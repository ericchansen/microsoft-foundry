import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents" / "travel" / "src"))

from contoso_travel_agent import operations, release  # noqa: E402


def test_route_requires_one_exact_accepted_version():
    assert release.routed_version({"version_selector": {
        "version_selection_rules": [{"type": "FixedRatio", "agent_version": "7", "traffic_percentage": 100}],
    }}) == "7"
    for rules in ([], [{"agent_version": "@latest", "traffic_percentage": 100}],
                  [{"agent_version": "7", "traffic_percentage": 50}]):
        with pytest.raises((release.ReleaseError, ValueError, RuntimeError)):
            release.routed_version({"version_selector": {"version_selection_rules": rules}})


def test_candidate_gate_requires_all_business_cases():
    summary = {
        "execution_status": "completed", "paired_samples": 6,
        "paired_cases": [{"case": f"case-{index}-r1"} for index in range(6)],
        "sides": {"candidate": {"version": "7", "quality_counts": {
            criterion: {"passed": 6, "failed": 0} for criterion in release.CRITERIA
        }}},
    }
    assert release.require_candidate_pass(summary) == "7"
    summary["sides"]["candidate"]["quality_counts"]["task_correctness"] = {"passed": 5, "failed": 1}
    with pytest.raises(release.ReleaseError, match="task_correctness"):
        release.require_candidate_pass(summary)
    summary["sides"]["candidate"]["quality_counts"]["task_correctness"] = {"passed": 6, "failed": 0}
    summary["paired_cases"] = [{"case": f"repeated-r{index}"} for index in range(6)]
    with pytest.raises(release.ReleaseError, match="distinct"):
        release.require_candidate_pass(summary)


def test_release_inputs_remain_private(tmp_path):
    assert release.private_path(tmp_path, Path("internal/receipt.json")) == tmp_path / "internal/receipt.json"
    with pytest.raises(release.ReleaseError):
        release.private_path(tmp_path, Path("reports/receipt.json"))


def test_default_release_does_not_read_or_write_cloud(tmp_path):
    output = tmp_path / "must-not-exist"
    assert release.main([
        "promote", "--comparison", "internal/comparison", "--expect-version", "5",
        "--repo-root", str(tmp_path), "--output-dir", str(output),
    ]) == 0
    assert not output.exists()


@pytest.mark.parametrize("failure", ["boundary", "drift", "unhealthy", None])
def test_live_governance_gate_runs_before_any_route_change(monkeypatch, repo_root, failure):
    pin = Mock()
    collect = Mock(return_value=SimpleNamespace(ok=failure != "unhealthy"))
    guard = Mock(return_value=SimpleNamespace(resource_group="rg-contoso-agents"))
    if failure == "boundary":
        guard.side_effect = PermissionError("live ownership boundary failed")
    if failure == "drift":
        collect.side_effect = operations.gateway.GatewayConfigError("responsible AI policy mode must be Blocking")
    monkeypatch.setattr(operations, "pin_agent_version", pin)
    monkeypatch.setattr(operations.boundary, "require_clean_live", guard)
    monkeypatch.setenv("FOUNDRY_ENABLED_MODULES", "optional-control-plane")
    monkeypatch.setattr(operations.gateway, "collect_status", collect)
    project, manifest = object(), {"agent_name": "contoso-travel", "created_version": "7"}
    if failure:
        with pytest.raises((PermissionError, operations.OperationsError, operations.gateway.GatewayConfigError)):
            operations.governed_pin_agent_version(project, manifest, repo_root, expected_endpoint={"before": True})
        pin.assert_not_called()
    else:
        operations.governed_pin_agent_version(project, manifest, repo_root, expected_endpoint={"before": True})
        pin.assert_called_once_with(project, manifest, expected_endpoint={"before": True})
    guard.assert_called_once_with(
        repo_root / "config" / "boundary.yaml", enabled_modules={"optional-control-plane"},
    )
    if failure == "boundary":
        collect.assert_not_called()
    else:
        assert collect.call_args.args[0] == "rg-contoso-agents"
        assert collect.call_args.kwargs["expected_location"]


def test_governed_route_preserves_explicit_optional_modules(monkeypatch, repo_root):
    monkeypatch.delenv("FOUNDRY_ENABLED_MODULES", raising=False)
    guard = Mock(return_value=SimpleNamespace(resource_group="rg-contoso-agents"))
    monkeypatch.setattr(operations.boundary, "require_clean_live", guard)
    monkeypatch.setattr(operations.gateway, "collect_status", Mock(return_value=SimpleNamespace(ok=True)))
    monkeypatch.setattr(operations, "pin_agent_version", Mock())
    operations.governed_pin_agent_version(
        object(), {"agent_name": "contoso-travel", "created_version": "7"}, repo_root,
        expected_endpoint={"before": True}, enabled_modules=["optional-control-plane"],
    )
    assert guard.call_args.kwargs["enabled_modules"] == {"optional-control-plane"}


def test_legacy_evaluate_cli_cannot_promote_with_dirty_live_inventory(monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"agent_name": "contoso-travel", "created_version": "7"}))
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://project.example.invalid")
    monkeypatch.delenv("FOUNDRY_ENABLED_MODULES", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "operations", "evaluate", "--repo-root", str(tmp_path), "--manifest", "manifest.json",
        "--enable-module", "optional-control-plane",
    ])
    monkeypatch.setattr(operations, "_clients", lambda _: (object(), object()))
    monkeypatch.setattr(operations, "endpoint_snapshot", Mock(return_value={"before": True}))
    monkeypatch.setattr(operations, "evaluate", Mock(return_value={"status": "passed"}))
    guard = Mock(side_effect=PermissionError("live ownership boundary failed"))
    collect, pin = Mock(), Mock()
    monkeypatch.setattr(operations.boundary, "require_clean_live", guard)
    monkeypatch.setattr(operations.gateway, "collect_status", collect)
    monkeypatch.setattr(operations, "pin_agent_version", pin)
    with pytest.raises(PermissionError, match="live ownership boundary"):
        operations.main()
    assert guard.call_args.kwargs["enabled_modules"] == {"optional-control-plane"}
    collect.assert_not_called()
    pin.assert_not_called()


@pytest.mark.parametrize("changed_field", ["version_selector", "authorization_schemes", "protocols"])
def test_routing_rechecks_endpoint_after_slow_preflight(monkeypatch, repo_root, changed_field):
    before = {
        "version_selector": {"version_selection_rules": [{
            "type": "FixedRatio", "agent_version": "5", "traffic_percentage": 100,
        }]},
        "authorization_schemes": ["original-auth"],
        "protocols": ["original-protocol"],
    }
    current = deepcopy(before)
    update = Mock()
    project = SimpleNamespace(agents=SimpleNamespace(
        get=Mock(return_value=SimpleNamespace(agent_endpoint=SimpleNamespace(as_dict=lambda: current))),
        update_details=update,
    ))
    monkeypatch.setattr(
        operations.boundary, "require_clean_live",
        Mock(return_value=SimpleNamespace(resource_group="rg-contoso-agents")),
    )

    def slow_preflight(*args, **kwargs):
        current[changed_field] = {"changed-by-another-operator": True}
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(operations.gateway, "collect_status", slow_preflight)
    with pytest.raises(operations.OperationsError, match="endpoint changed"):
        operations.governed_pin_agent_version(
            project, {"agent_name": "contoso-travel", "created_version": "7"}, repo_root,
            expected_endpoint=before,
        )
    update.assert_not_called()


@pytest.mark.parametrize("missing_role,missing_image", [(False, False), (True, False), (False, True)])
def test_bundle_reads_scoped_permissions_without_all(monkeypatch, tmp_path, missing_role, missing_image):
    name = "contoso-agents-travel-tool-v4"
    connection_id = "owned-account/projects/travel/connections/travel-openapi-v4"
    connection = SimpleNamespace(id=connection_id, target="https://tool.example.invalid")
    definition = {"kind": "prompt", "tools": [{"type": "openapi", "openapi": {
        "auth": {"security_scheme": {"project_connection_id": connection_id}},
        "spec": {"servers": [{"url": connection.target}]},
    }}]}
    project = SimpleNamespace(
        agents=SimpleNamespace(get_version=lambda **_: {
            "name": "contoso-travel", "version": "7", "definition": definition,
        }),
        connections=SimpleNamespace(get=lambda _: connection),
    )
    monkeypatch.setattr(release.boundary, "load_plan", lambda _: {
        "resource_group": "owned", "resources": [{"scope": "providers/Microsoft.App/containerApps/" + name}],
    })

    def run(args):
        if args[:2] == ["containerapp", "show"]:
            return {
                "properties": {"provisioningState": "Succeeded", "template": {
                    "containers": [{"image": "registry.example.invalid/tool@sha256:" + "a" * 64}],
                }},
                "identity": {"userAssignedIdentities": {"owned-identity": {}}},
            }
        if args[:2] == ["identity", "show"]:
            return {"id": "owned-identity", "principalId": "principal"}
        if args[:2] == ["acr", "list"]:
            return [{"id": "registry-resource", "name": "owned-registry", "loginServer": "registry.example.invalid"}]
        if args[:2] == ["acr", "repository"]:
            return {"digest": "sha256:" + ("b" if missing_image else "a") * 64}
        if args[:2] == ["monitor", "app-insights"]:
            return {"id": "insights-resource"}
        assert args[:4] == ["role", "assignment", "list", "--scope"]
        assert "--all" not in args
        if missing_role:
            return []
        return [{
            "scope": args[4], "principalId": "principal",
            "roleDefinitionName": "AcrPull" if args[4] == "registry-resource" else "Monitoring Metrics Publisher",
        }]

    monkeypatch.setattr(release.azure_cli, "run", run)
    if missing_role or missing_image:
        with pytest.raises(release.ReleaseError, match="permission" if missing_role else "image manifest"):
            release.tool_bundle(project, tmp_path, "contoso-travel", "7", "owned-account")
    else:
        assert release.tool_bundle(project, tmp_path, "contoso-travel", "7", "owned-account")["backend"] == name
