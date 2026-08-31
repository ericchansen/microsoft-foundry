"""Live deployment and smoke operations for one exact Travel agent version."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from contoso_foundry.data import build as build_mod
from contoso_travel_agent.definition import create_agent_version, load_agent_spec
from contoso_travel_agent.evaluation import (
    EvaluationError,
    collect_candidate_samples,
    load_golden,
    run_openai_eval,
    tool_calls_match,
)
from contoso_travel_agent.runtime import ExecutedToolCall, ServerExecutedTravelRuntime


class OperationsError(RuntimeError):
    """Raised when a live operation cannot prove its exact candidate."""


CONTINUOUS_EVALUATION_NAME = "Contoso Travel continuous safety"


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def verify_continuous_evaluation(openai_client: Any) -> dict[str, Any]:
    """Read back the newest recurring safety result without exposing live IDs."""

    evaluations = [
        item
        for item in openai_client.evals.list(limit=100)
        if _value(item, "name") == CONTINUOUS_EVALUATION_NAME
    ]
    if len(evaluations) != 1:
        raise OperationsError(
            f"expected one {CONTINUOUS_EVALUATION_NAME!r} evaluation; found {len(evaluations)}"
        )
    evaluation_id = str(_value(evaluations[0], "id", "")).strip()
    if not evaluation_id:
        raise OperationsError("the recurring evaluation has no ID")
    runs = list(openai_client.evals.runs.list(eval_id=evaluation_id, limit=20))
    if not runs:
        raise OperationsError("the recurring evaluation has no run results")
    newest = max(runs, key=lambda item: int(_value(item, "created_at", 0) or 0))
    status = str(_value(newest, "status", "")).lower()
    counts_value = _value(newest, "result_counts", {})
    counts = counts_value.model_dump() if hasattr(counts_value, "model_dump") else dict(counts_value)
    normalized_counts = {
        key: int(counts.get(key, 0) or 0)
        for key in ("errored", "failed", "passed", "skipped", "total")
    }
    if (
        status != "completed"
        or normalized_counts["total"] <= 0
        or normalized_counts["passed"] != normalized_counts["total"]
        or normalized_counts["failed"] != 0
        or normalized_counts["errored"] != 0
        or not str(_value(newest, "report_url", "")).strip()
    ):
        raise OperationsError(
            f"the newest recurring evaluation result is not passing: "
            f"status={status or 'missing'}, counts={normalized_counts}"
        )
    return {
        "evaluation_name": CONTINUOUS_EVALUATION_NAME,
        "status": status,
        "result_counts": normalized_counts,
        "report_url_present": True,
    }


def _write_internal(repo_root: Path, relative_path: Path, payload: dict[str, Any]) -> Path:
    destination = (repo_root / relative_path).resolve()
    internal = (repo_root / "internal").resolve()
    if internal not in destination.parents:
        raise OperationsError("live manifests and evidence must be written under internal/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _build_database(repo_root: Path) -> sqlite3.Connection:
    result = build_mod.build(
        config_path=repo_root / "config" / "data-spine.yaml",
        seed_dir=repo_root / "data" / "seed",
        out_dir=Path(tempfile.mkdtemp(prefix="contoso-travel-smoke-")),
        fixtures_dir=repo_root / "data" / "fixtures",
    )
    return sqlite3.connect(result.root / "contoso.db")


def deploy(repo_root: Path, project_client: Any) -> dict[str, Any]:
    spec = load_agent_spec(repo_root / "agents" / "travel" / "agent.yaml")
    manifest = create_agent_version(project_client, spec)
    _write_internal(repo_root, Path("internal/travel/agent-version.json"), manifest)
    return manifest


def smoke(
    repo_root: Path,
    openai_client: Any,
    manifest: dict[str, Any],
    *,
    tracer: Any | None = None,
) -> dict[str, Any]:
    required = ("agent_name", "created_version", "definition_digest", "model_version")
    missing = [name for name in required if not str(manifest.get(name, "")).strip()]
    if missing:
        raise OperationsError(f"candidate manifest is incomplete: {', '.join(missing)}")
    spec = load_agent_spec(repo_root / "agents" / "travel" / "agent.yaml")
    if manifest["agent_name"] != spec.name or manifest["definition_digest"] != spec.digest:
        raise OperationsError("candidate manifest does not match the checked-in definition")

    span_context = (
        tracer.start_as_current_span("contoso.travel.smoke") if tracer is not None else nullcontext(None)
    )
    with span_context as span:
        if span is not None:
            span.set_attribute("contoso.synthetic", True)
            span.set_attribute("gen_ai.agent.name", spec.name)
            span.set_attribute("gen_ai.agent.version", str(manifest["created_version"]))
        runtime = ServerExecutedTravelRuntime(openai_client, spec)
        answer = runtime.run_turn(
            "Find the synthetic route from LOC-001 (Contoso Seattle Headquarters) "
            "to LOC-002 (Contoso Chicago Distribution).",
            agent_version=str(manifest["created_version"]),
        )
    expected_route_call = ExecutedToolCall.from_arguments(
        "travel_search_routes",
        {
            "origin_location_id": "LOC-001",
            "destination_location_id": "LOC-002",
        },
    )
    if "ROUTE-0001" not in answer or "synthetic" not in answer.casefold():
        raise OperationsError("the exact candidate did not return the required synthetic route evidence")
    if not tool_calls_match(runtime.executed_calls, [expected_route_call]):
        raise OperationsError("the exact candidate did not execute the expected server-side route call")
    evidence = {
        "agent_name": spec.name,
        "agent_version": str(manifest["created_version"]),
        "definition_digest": spec.digest,
        "model_version": spec.model_version,
        "synthetic": True,
        "client_function_callbacks": 0,
        "tool_execution": "server",
        "answer_length": len(answer),
        "status": "passed",
    }
    _write_internal(repo_root, Path("internal/travel/smoke-result.json"), evidence)
    return evidence


def evaluate(
    repo_root: Path,
    openai_client: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    spec = load_agent_spec(repo_root / "agents" / "travel" / "agent.yaml")
    if (
        manifest.get("agent_name") != spec.name
        or manifest.get("definition_digest") != spec.digest
        or not str(manifest.get("created_version", "")).strip()
    ):
        raise OperationsError("evaluation candidate does not match the exact checked-in definition")
    version = str(manifest["created_version"])
    golden = load_golden(repo_root / "agents" / "travel" / "golden" / "travel.jsonl")
    def run_case(prompt: str, name: str, agent_version: str):
        if name != spec.name or agent_version != version:
            raise OperationsError("evaluation attempted to route away from the exact candidate")
        runtime = ServerExecutedTravelRuntime(openai_client, spec)
        answer = runtime.run_turn(prompt, agent_version=agent_version)
        return answer, runtime.executed_calls

    samples = collect_candidate_samples(
        golden,
        agent_name=spec.name,
        agent_version=version,
        run_case=run_case,
        server_executed=True,
    )
    try:
        result = run_openai_eval(
            openai_client,
            samples,
            judge_model=spec.deployment_name,
        )
    except EvaluationError as error:
        evidence = error.evidence
        if isinstance(evidence, dict):
            result = evidence
            output_items = result.pop("output_items", [])
            _write_internal(
                repo_root,
                Path("internal/travel/eval-output-items.json"),
                {
                    "eval_id": result["eval_id"],
                    "run_id": result["run_id"],
                    "items": output_items,
                    "synthetic": True,
                },
            )
            result.update(
                {
                    "agent_name": spec.name,
                    "agent_version": version,
                    "definition_digest": spec.digest,
                    "sample_count": len(samples),
                    "synthetic": True,
                }
            )
            _write_internal(repo_root, Path("internal/travel/eval-result.json"), result)
        raise
    output_items = result.pop("output_items")
    _write_internal(
        repo_root,
        Path("internal/travel/eval-output-items.json"),
        {
            "eval_id": result["eval_id"],
            "run_id": result["run_id"],
            "items": output_items,
            "synthetic": True,
        },
    )
    result.update(
        {
            "agent_name": spec.name,
            "agent_version": version,
            "definition_digest": spec.digest,
            "sample_count": len(samples),
            "synthetic": True,
        }
    )
    _write_internal(repo_root, Path("internal/travel/eval-result.json"), result)
    return result


def pin_agent_version(project_client: Any, manifest: dict[str, Any]) -> None:
    from azure.ai.projects.models import (
        FixedRatioVersionSelectionRule,
        VersionSelector,
    )

    name = str(manifest.get("agent_name", ""))
    version = str(manifest.get("created_version", ""))
    if not name or not version:
        raise OperationsError("promotion requires an exact agent name and version")
    agent = project_client.agents.get(agent_name=name)
    endpoint = getattr(agent, "agent_endpoint", None)
    if endpoint is None:
        raise OperationsError("the agent has no endpoint configuration to promote")
    endpoint.version_selector = VersionSelector(
        version_selection_rules=[
            FixedRatioVersionSelectionRule(
                agent_version=version,
                traffic_percentage=100,
            )
        ]
    )
    updated = project_client.agents.update_details(
        agent_name=name,
        agent_endpoint=endpoint,
    )
    rules = updated.agent_endpoint.version_selector.version_selection_rules
    if len(rules) != 1 or str(rules[0].agent_version) != version or rules[0].traffic_percentage != 100:
        raise OperationsError("the agent endpoint did not pin the accepted version")


def _clients(endpoint: str):
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    return project, project.get_openai_client()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("deploy", "smoke", "evaluate", "verify-continuous"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=Path("internal/travel/agent-version.json"))
    args = parser.parse_args()
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
    if not endpoint:
        raise OperationsError("FOUNDRY_PROJECT_ENDPOINT is required")
    if args.operation == "smoke":
        os.environ.setdefault("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING", "true")

    project, openai_client = _clients(endpoint)
    if args.operation == "deploy":
        result = deploy(args.repo_root, project)
    elif args.operation == "verify-continuous":
        result = verify_continuous_evaluation(openai_client)
        _write_internal(
            args.repo_root,
            Path("internal/travel/continuous-eval-result.json"),
            result,
        )
    elif args.operation == "smoke":
        from azure.ai.projects.telemetry import AIProjectInstrumentor
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry import trace

        configure_azure_monitor(
            connection_string=project.telemetry.get_application_insights_connection_string()
        )
        AIProjectInstrumentor().instrument()
        manifest = json.loads((args.repo_root / args.manifest).read_text(encoding="utf-8"))
        result = smoke(
            args.repo_root,
            openai_client,
            manifest,
            tracer=trace.get_tracer("contoso.travel.smoke"),
        )
    else:
        manifest = json.loads((args.repo_root / args.manifest).read_text(encoding="utf-8"))
        result = evaluate(args.repo_root, openai_client, manifest)
        pin_agent_version(project, manifest)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
