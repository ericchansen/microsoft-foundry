"""Explicit, evidence-gated promotion and receipt-based rollback."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential

from contoso_foundry import azure_cli, boundary
from contoso_travel_agent.definition import build_agent_definition, load_agent_spec
from contoso_travel_agent.experiments import (
    CRITERIA,
    Evidence,
    digest,
    exact_version,
    load_dataset,
    owned_project,
    plain,
    resume_experiment,
)
from contoso_travel_agent.operations import governed_pin_agent_version


class ReleaseError(RuntimeError):
    """A release lacks evidence or no longer matches its expected state."""


def private_path(repo: Path, path: Path) -> Path:
    resolved = (repo / path).resolve()
    if (repo / "internal").resolve() not in resolved.parents:
        raise ReleaseError("Release inputs must remain under internal/")
    return resolved


def routed_version(endpoint: dict[str, Any]) -> str:
    rules = endpoint.get("version_selector", {}).get("version_selection_rules", [])
    if (
        len(rules) != 1 or rules[0].get("traffic_percentage") != 100
        or rules[0].get("type") != "FixedRatio"
    ):
        raise ReleaseError("The named endpoint must have one explicit accepted version at 100 percent")
    return exact_version(str(rules[0].get("agent_version", "")))


def require_candidate_pass(summary: dict[str, Any]) -> str:
    count = summary.get("paired_samples", 0)
    if summary.get("execution_status") != "completed" or type(count) is not int or count < 6:
        raise ReleaseError("Promotion requires a completed comparison with at least six paired cases")
    cases = summary.get("paired_cases", [])
    if len(cases) != count or len({case["case"].rsplit("-r", 1)[0] for case in cases}) < 6:
        raise ReleaseError("Repeating a smaller dataset cannot replace six distinct business cases")
    candidate = summary["sides"]["candidate"]
    for criterion in CRITERIA:
        grades = candidate["quality_counts"][criterion]
        if grades.get("passed") != count or grades.get("failed") != 0:
            raise ReleaseError(f"The candidate did not pass every {criterion} case")
    return exact_version(candidate["version"])


def tool_bundle(project: Any, repo: Path, name: str, version: str, account_id: str) -> dict[str, Any]:
    detail = plain(project.agents.get_version(agent_name=name, agent_version=version))
    if detail.get("name") != name or str(detail.get("version")) != version:
        raise ReleaseError("The dependency read returned a different agent version")
    definition = detail["definition"]
    tools = definition.get("tools", [])
    if definition.get("kind") != "prompt" or len(tools) != 1 or tools[0].get("type") != "openapi":
        raise ReleaseError("The release must reference one authenticated server-executed Toolbox")
    operation = tools[0]["openapi"]
    connection_id = operation["auth"]["security_scheme"]["project_connection_id"]
    if not connection_id.casefold().startswith((account_id + "/projects/travel/connections/").casefold()):
        raise ReleaseError("Tool connection is outside the owned Travel project")
    connection = project.connections.get(connection_id.rsplit("/", 1)[-1])
    servers = operation["spec"].get("servers", [])
    if (
        len(servers) != 1 or servers[0]["url"].rstrip("/") != connection.target.rstrip("/")
        or connection.id.casefold() != connection_id.casefold()
    ):
        raise ReleaseError("The immutable agent and its live tool connection disagree")
    plan = boundary.load_plan(repo / "config/boundary.yaml")
    group = plan["resource_group"]
    release = connection_id.rsplit("travel-openapi-", 1)[-1]
    if not release.startswith("v") or not release[1:].isdigit():
        raise ReleaseError("Tool connection does not identify a versioned release")
    app_name = "contoso-agents-travel-tool-" + release
    expected_scope = "providers/Microsoft.App/containerApps/" + app_name
    if not any(r.get("scope") == expected_scope for r in plan["resources"]):
        raise ReleaseError("The backend release is not declared in the ownership boundary")
    app = azure_cli.run(["containerapp", "show", "-g", group, "-n", app_name])
    properties = app["properties"]
    image = properties["template"]["containers"][0]["image"]
    image_digest = image.rpartition("@sha256:")[2]
    if (
        properties["provisioningState"] != "Succeeded" or len(image_digest) != 64
        or any(character not in "0123456789abcdef" for character in image_digest)
    ):
        raise ReleaseError("The retained backend is not provisioned with an immutable image")
    identities = app["identity"]["userAssignedIdentities"]
    if len(identities) != 1:
        raise ReleaseError("The tool backend must have exactly one owned identity")
    identity = azure_cli.run(["identity", "show", "-g", group, "-n", app_name])
    if set(key.casefold() for key in identities) != {identity["id"].casefold()}:
        raise ReleaseError("The backend is not using its own versioned identity")
    registries = azure_cli.run(["acr", "list", "-g", group])
    if len(registries) != 1 or not image.startswith(registries[0]["loginServer"] + "/"):
        raise ReleaseError("The backend image is not in the owned registry")
    manifest = azure_cli.run([
        "acr", "repository", "show", "--name", registries[0]["name"],
        "--image", image.removeprefix(registries[0]["loginServer"] + "/"),
    ])
    if manifest.get("digest") != "sha256:" + image_digest:
        raise ReleaseError("The retained image manifest is not available at the recorded digest")
    insights = azure_cli.run([
        "monitor", "app-insights", "component", "show", "-g", group, "-a", "contoso-agents-insights",
    ])
    for resource, role in ((registries[0], "AcrPull"), (insights, "Monitoring Metrics Publisher")):
        assignments = azure_cli.run(["role", "assignment", "list", "--scope", resource["id"]])
        if not any(
            entry.get("principalId", "").casefold() == identity["principalId"].casefold()
            and entry.get("roleDefinitionName") == role
            and entry.get("scope", "").casefold() == resource["id"].casefold()
            for entry in assignments
        ):
            raise ReleaseError(f"The retained backend is missing its scoped {role} permission")
    return {
        "version": version, "definition_digest": digest(definition),
        "connection_id": connection_id, "backend": app_name, "image": image,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["promote", "rollback"])
    parser.add_argument("--comparison", type=Path, help="Original experiment folder containing its cloud run IDs")
    parser.add_argument("--receipt", type=Path, help="Receipt from the promotion to reverse")
    parser.add_argument("--expect-version", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--enable-module", action="append", default=[])
    parser.add_argument("--apply", action="store_true", help="Explicitly authorize changing the named route")
    args = parser.parse_args(argv)
    if args.action == "promote" and (not args.comparison or args.receipt):
        parser.error("promote requires --comparison and does not accept --receipt")
    if args.action == "rollback" and (not args.receipt or args.comparison):
        parser.error("rollback requires --receipt and does not accept --comparison")
    expected = exact_version(args.expect_version)
    if not args.apply:
        print("No changes made. --apply authorizes an exact named-route change after live evidence gates.")
        return 0
    repo = args.repo_root.resolve()
    evidence = Evidence(repo, args.output_dir)
    spec = load_agent_spec(repo / "agents/travel/agent.yaml")
    original = None
    if args.action == "promote":
        source = private_path(repo, args.comparison)
        manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
        resume = argparse.Namespace(
            repo_root=repo, resume_from=source, enable_module=args.enable_module,
            baseline_version=manifest["baseline_version"], candidate_version=manifest["candidate_version"],
            judge_deployment=manifest["judge_deployment"], request_timeout=90, timeout_seconds=600,
        )
        summary = resume_experiment(resume, evidence)
        evidence.write("comparison-summary.json", summary)
        _, expected_dataset = load_dataset(repo / "agents/travel/golden/operator.jsonl")
        if summary["dataset_digest"] != expected_dataset:
            raise ReleaseError("The comparison does not use the checked-in operator dataset")
        target = require_candidate_pass(summary)
    else:
        original = json.loads(private_path(repo, args.receipt).read_text(encoding="utf-8"))
        if original.get("action") != "promote" or original.get("agent_name") != spec.name:
            raise ReleaseError("Rollback requires this agent's promotion receipt")
        if expected != original["target_version"]:
            raise ReleaseError("Rollback expectation does not match the receipt's promoted version")
        target = exact_version(original["previous_version"])
    if target == expected:
        raise ReleaseError("The target is already the expected accepted version")
    owned = owned_project(repo, spec, args.enable_module)
    with AzureCliCredential() as credential, AIProjectClient(
        endpoint=owned["endpoint"], credential=credential, allow_preview=True,
        retry_total=0, connection_timeout=30, read_timeout=90,
    ) as project:
        before = plain(project.agents.get(agent_name=spec.name))["agent_endpoint"]
        if routed_version(before) != expected:
            raise ReleaseError("The named route changed; refusing to overwrite another decision")
        bundles = {
            version: tool_bundle(project, repo, spec.name, version, owned["account_id"])
            for version in (expected, target)
        }
        if original and bundles[target] != original["bundles"][target]:
            raise ReleaseError("The rollback dependency bundle changed after promotion")
        if args.action == "promote":
            connection = project.connections.get(spec.tool_connection_name)
            declared = build_agent_definition(spec, server_url=connection.target, project_connection_id=connection.id)
            if bundles[target]["definition_digest"] != digest(declared.as_dict()):
                raise ReleaseError("The passing candidate no longer matches the checked-in definition")
        receipt = {
            "action": args.action, "agent_name": spec.name, "previous_version": expected,
            "target_version": target, "before": before, "bundles": bundles,
        }
        evidence.write("receipt.json", receipt)
        governed_pin_agent_version(
            project, {"agent_name": spec.name, "created_version": target}, repo,
            expected_endpoint=before, enabled_modules=args.enable_module,
        )
        after = plain(project.agents.get(agent_name=spec.name))["agent_endpoint"]
        evidence.write("routing-after.json", after)
        if routed_version(after) != target:
            raise ReleaseError("Named routing did not read back the selected version")
        if any(
            after.get(field) != before.get(field)
            for field in ("authorization_schemes", "protocols", "protocol_configuration")
        ):
            raise ReleaseError("The endpoint authentication or protocol contract changed")
        with project.get_openai_client(agent_name=spec.name, timeout=90, max_retries=0) as client:
            response = client.responses.create(input="Find the route from LOC-001 to LOC-002.")
            body = plain(response)
            evidence.write("named-response.json", body)
            if (
                body.get("status") != "completed"
                or body.get("agent_reference", {}).get("version") != target
                or "ROUTE-0001" not in response.output_text
            ):
                raise ReleaseError("The named endpoint did not execute the selected version correctly")
    result = {"action": args.action, "version": target, "named_response_verified": True, "dependencies_preserved": True}
    evidence.write("summary.json", result)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
