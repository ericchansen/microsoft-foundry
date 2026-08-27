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
)
from contoso_travel_agent.runtime import TravelAgentRuntime


class OperationsError(RuntimeError):
    """Raised when a live operation cannot prove its exact candidate."""


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
    with span_context as span, _build_database(repo_root) as connection:
        if span is not None:
            span.set_attribute("contoso.synthetic", True)
            span.set_attribute("gen_ai.agent.name", spec.name)
            span.set_attribute("gen_ai.agent.version", str(manifest["created_version"]))
        runtime = TravelAgentRuntime(
            openai_client,
            spec,
            connection,
            contracts_dir=repo_root / "config" / "toolbox",
            tracer=tracer,
        )
        answer = runtime.run_turn(
            "Find the synthetic route from Contoso Seattle Headquarters to Contoso Chicago Distribution.",
            agent_version=str(manifest["created_version"]),
        )
    tool_names = [call.tool for call in runtime.audit]
    if "travel_search_routes" not in tool_names:
        raise OperationsError("the exact candidate did not call travel_search_routes")
    evidence = {
        "agent_name": spec.name,
        "agent_version": str(manifest["created_version"]),
        "definition_digest": spec.digest,
        "model_version": spec.model_version,
        "synthetic": True,
        "tool_names": tool_names,
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
    with _build_database(repo_root) as connection:

        def run_case(prompt: str, name: str, agent_version: str):
            if name != spec.name or agent_version != version:
                raise OperationsError("evaluation attempted to route away from the exact candidate")
            runtime = TravelAgentRuntime(
                openai_client,
                spec,
                connection,
                contracts_dir=repo_root / "config" / "toolbox",
            )
            answer = runtime.run_turn(prompt, agent_version=agent_version)
            return answer, runtime.executed_calls

        samples = collect_candidate_samples(
            golden,
            agent_name=spec.name,
            agent_version=version,
            run_case=run_case,
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


def _clients(endpoint: str):
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    return project, project.get_openai_client()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("deploy", "smoke", "evaluate"))
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
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
