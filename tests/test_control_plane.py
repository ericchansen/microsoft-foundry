from __future__ import annotations

from copy import deepcopy

import pytest

from contoso_foundry import control_plane


@pytest.fixture
def config(config_dir):
    return control_plane.load_config(config_dir / "control-plane-platforms.yaml")


def sre_resource(config):
    entry = config["platforms"][0]
    return {
        "name": entry["resource_name"],
        "type": entry["resource_type"],
        "tags": config["ownership_tags"],
        "identity": {"type": entry["identity_type"]},
        "properties": {
            "actionConfiguration": {"accessLevel": "Low", "mode": "Review"},
            "powerState": "Running",
            "knowledgeGraphConfiguration": {
                "managedResources": [f"/subscriptions/redacted/resourceGroups/{config['resource_group']}"]
            },
            "logConfiguration": {
                "applicationInsightsConfiguration": {
                    "appId": "redacted",
                    "connectionString": "redacted",
                }
            },
        },
    }


def logic_resource(config):
    entry = config["platforms"][1]
    return {
        "name": entry["resource_name"],
        "type": entry["resource_type"],
        "tags": config["ownership_tags"],
        "identity": {"type": "UserAssigned"},
        "properties": {
            "state": "Enabled",
            "definition": {
                "actions": {
                    "Approval_triage_agent": {
                        "type": "Agent",
                        "tools": {"Create_approval_recommendation": {"actions": {}}},
                    },
                    "Create_synthetic_review_envelope": {
                        "type": "Compose",
                        "inputs": {"requiresHumanApproval": True, "synthetic": True},
                    },
                }
            }
        },
    }


def application_insights_resource():
    return {"properties": {"AppId": "redacted"}}


def test_static_inventory_is_machine_checkable(config):
    report = control_plane.verify(config, live=False)
    assert report.ok
    assert {platform.platform_id for platform in report.platforms} == {
        "contoso-sre",
        "contoso-approvals",
    }


def test_live_inventory_verifies_both_platforms(monkeypatch, config):
    resources = {
        "contoso-agents-sre-control-plane": sre_resource(config),
        "contoso-agents-approvals-loop": logic_resource(config),
        "contoso-agents-insights": application_insights_resource(),
    }
    monkeypatch.setattr(
        control_plane.azure_cli,
        "try_run",
        lambda args, **_kwargs: resources.get(args[args.index("--name") + 1]),
    )
    report = control_plane.verify(config)
    assert report.ok
    assert all(platform.present for platform in report.platforms)


def test_missing_platform_fails_instead_of_becoming_unknown(monkeypatch, config):
    monkeypatch.setattr(control_plane.azure_cli, "try_run", lambda *_args, **_kwargs: None)
    report = control_plane.verify(config)
    assert not report.ok
    assert all("not returned" in platform.errors[0] for platform in report.platforms)


def test_logic_apps_without_agent_action_is_not_coverage(monkeypatch, config):
    resources = {
        "contoso-agents-sre-control-plane": sre_resource(config),
        "contoso-agents-approvals-loop": logic_resource(config),
        "contoso-agents-insights": application_insights_resource(),
    }
    broken = deepcopy(resources["contoso-agents-approvals-loop"])
    broken["properties"]["definition"]["actions"] = {}
    resources["contoso-agents-approvals-loop"] = broken
    monkeypatch.setattr(
        control_plane.azure_cli,
        "try_run",
        lambda args, **_kwargs: resources.get(args[args.index("--name") + 1]),
    )
    report = control_plane.verify(config)
    approvals = next(platform for platform in report.platforms if platform.platform_id == "contoso-approvals")
    assert not approvals.ok
    assert "Logic Apps workflow contains no Agent action" in approvals.errors


def test_logic_apps_requires_a_synthetic_human_review_envelope(monkeypatch, config):
    resources = {
        "contoso-agents-sre-control-plane": sre_resource(config),
        "contoso-agents-approvals-loop": logic_resource(config),
        "contoso-agents-insights": application_insights_resource(),
    }
    del resources["contoso-agents-approvals-loop"]["properties"]["definition"]["actions"][
        "Create_synthetic_review_envelope"
    ]
    monkeypatch.setattr(
        control_plane.azure_cli,
        "try_run",
        lambda args, **_kwargs: resources.get(args[args.index("--name") + 1]),
    )
    report = control_plane.verify(config)
    approvals = next(platform for platform in report.platforms if platform.platform_id == "contoso-approvals")
    assert not approvals.ok
    assert "Logic Apps workflow lacks the mandatory synthetic human-review envelope" in approvals.errors


def test_sre_must_use_the_shared_application_insights(monkeypatch, config):
    resources = {
        "contoso-agents-sre-control-plane": sre_resource(config),
        "contoso-agents-approvals-loop": logic_resource(config),
        "contoso-agents-insights": {"properties": {"AppId": "different"}},
    }
    monkeypatch.setattr(
        control_plane.azure_cli,
        "try_run",
        lambda args, **_kwargs: resources.get(args[args.index("--name") + 1]),
    )
    report = control_plane.verify(config)
    sre = next(platform for platform in report.platforms if platform.platform_id == "contoso-sre")
    assert not sre.ok
    assert "SRE Agent is connected to a different Application Insights resource" in sre.errors


def test_logic_apps_observability_is_explicitly_unsupported(config):
    approvals = next(entry for entry in config["platforms"] if entry["id"] == "contoso-approvals")
    assert approvals["control_plane"]["observability"] == "unsupported"
