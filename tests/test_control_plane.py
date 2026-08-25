from __future__ import annotations

from copy import deepcopy

import pytest

from contoso_foundry import control_plane


@pytest.fixture
def config(config_dir):
    return control_plane.load_config(config_dir / "control-plane-platforms.yaml")


@pytest.fixture(autouse=True)
def operator_group(monkeypatch):
    monkeypatch.setenv("SRE_OPERATOR_GROUP_OBJECT_ID", "operator-group-principal")


def resource_ids(config):
    resource_group_id = f"/subscriptions/redacted/resourceGroups/{config['resource_group']}"
    return {
        "resource_group": resource_group_id,
        "sre": f"{resource_group_id}/providers/Microsoft.App/agents/contoso-agents-sre-control-plane",
        "sre_uami": (
            f"{resource_group_id}/providers/Microsoft.ManagedIdentity/"
            "userAssignedIdentities/contoso-agents-sre"
        ),
        "app_insights": (
            f"{resource_group_id}/providers/Microsoft.Insights/components/contoso-agents-insights"
        ),
    }


def sre_resource(config):
    ids = resource_ids(config)
    return {
        "id": ids["sre"],
        "name": config["platforms"][0]["resource_name"],
        "type": "Microsoft.App/agents",
        "tags": config["ownership_tags"],
        "identity": {
            "type": "SystemAssigned,UserAssigned",
            "principalId": "sre-system-principal",
            "userAssignedIdentities": {ids["sre_uami"]: {}},
        },
        "properties": {
            "actionConfiguration": {
                "accessLevel": "Low",
                "identity": ids["sre_uami"],
                "mode": "Review",
            },
            "powerState": "Running",
            "knowledgeGraphConfiguration": {
                "identity": ids["sre_uami"],
                "managedResources": [ids["resource_group"]],
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
    synthetic_expression = "@triggerBody()?['synthetic']"
    return {
        "name": entry["resource_name"],
        "type": entry["resource_type"],
        "tags": config["ownership_tags"],
        "identity": {"type": "UserAssigned"},
        "properties": {
            "state": "Enabled",
            "definition": {
                "triggers": {
                    "Receive_synthetic_approval_scenario": {
                        "type": "Request",
                        "kind": "Http",
                        "inputs": {
                            "schema": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "scenario": {"type": "string"},
                                    "synthetic": {"type": "boolean", "enum": [True]},
                                },
                                "required": ["scenario", "synthetic"],
                            }
                        },
                        "conditions": [{"expression": "@equals(triggerBody()?['synthetic'], true)"}],
                    }
                },
                "actions": {
                    "Approval_triage_agent": {
                        "type": "Agent",
                        "inputs": {},
                        "tools": {
                            "Create_approval_recommendation": {
                                "description": "Synthetic recommendation",
                                "agentParameterSchema": {},
                                "actions": {
                                    "Build_recommendation": {
                                        "type": "Compose",
                                        "inputs": {
                                            "requiresHumanApproval": True,
                                            "synthetic": synthetic_expression,
                                        },
                                    }
                                },
                            }
                        },
                        "runAfter": {},
                        "limit": {"count": 5, "timeout": "PT5M"},
                    },
                    "Create_synthetic_review_envelope": {
                        "type": "Compose",
                        "inputs": {
                            "requiresHumanApproval": True,
                            "synthetic": synthetic_expression,
                        },
                        "runAfter": {"Approval_triage_agent": ["Succeeded"]},
                    },
                    "Return_recommendation": {
                        "type": "Response",
                        "kind": "Http",
                        "inputs": {
                            "statusCode": 202,
                            "body": {
                                "result": "@outputs('Create_synthetic_review_envelope')",
                            },
                        },
                        "runAfter": {"Create_synthetic_review_envelope": ["Succeeded"]},
                    },
                },
            },
        },
    }


def application_insights_resource(config):
    return {
        "id": resource_ids(config)["app_insights"],
        "properties": {"AppId": "redacted"},
    }


def sre_identity_resource(config):
    return {
        "id": resource_ids(config)["sre_uami"],
        "properties": {"principalId": "sre-uami-principal"},
    }


def role_assignment(principal_id, role_definition_id, scope, principal_type):
    return {
        "principalId": principal_id,
        "principalType": principal_type,
        "roleDefinitionId": role_definition_id,
        "scope": scope,
    }


def expected_role_assignments(config):
    ids = resource_ids(config)
    rbac = config["platforms"][0]["rbac"]
    identity_roles = [
        *[
            role_assignment("sre-uami-principal", role_id, ids["resource_group"], "ServicePrincipal")
            for role_id in rbac["resource_group_role_definition_ids"]
        ],
        *[
            role_assignment("sre-uami-principal", role_id, ids["app_insights"], "ServicePrincipal")
            for role_id in rbac["application_insights_role_definition_ids"]
        ],
    ]
    system_roles = [
        {**assignment, "principalId": "sre-system-principal"}
        for assignment in identity_roles
    ]
    operator_roles = [
        role_assignment(
            "operator-group-principal",
            rbac["operator_role_definition_id"],
            ids["sre"],
            "Group",
        )
    ]
    return {
        "sre-uami-principal": identity_roles,
        "sre-system-principal": system_roles,
        "operator-group-principal": operator_roles,
    }


def live_resources(config):
    return {
        "contoso-agents-sre-control-plane": sre_resource(config),
        "contoso-agents-approvals-loop": logic_resource(config),
        "contoso-agents-insights": application_insights_resource(config),
        "contoso-agents-sre": sre_identity_resource(config),
    }


def install_live_azure(monkeypatch, config, *, resources=None, assignments=None):
    resources = resources or live_resources(config)
    assignments = assignments or expected_role_assignments(config)

    def try_run(args, **_kwargs):
        if args[:2] == ["group", "show"]:
            return {"id": resource_ids(config)["resource_group"]}
        if args[:3] == ["role", "assignment", "list"]:
            return assignments.get(args[args.index("--assignee-object-id") + 1])
        if args[:2] == ["resource", "show"]:
            return resources.get(args[args.index("--name") + 1])
        return None

    monkeypatch.setattr(control_plane.azure_cli, "try_run", try_run)


def platform_result(report, platform_id):
    return next(platform for platform in report.platforms if platform.platform_id == platform_id)


def test_static_inventory_is_machine_checkable(config):
    report = control_plane.verify(config, live=False)
    assert report.ok
    assert {platform.platform_id for platform in report.platforms} == {
        "contoso-sre",
        "contoso-approvals",
    }


def test_live_inventory_verifies_both_platforms(monkeypatch, config):
    install_live_azure(monkeypatch, config)
    report = control_plane.verify(config)
    assert report.ok
    assert all(platform.present for platform in report.platforms)


def test_missing_platform_fails_instead_of_becoming_unknown(monkeypatch, config):
    monkeypatch.setattr(control_plane.azure_cli, "try_run", lambda *_args, **_kwargs: None)
    report = control_plane.verify(config)
    assert not report.ok
    assert all("not returned" in platform.errors[0] for platform in report.platforms)


@pytest.mark.parametrize(
    "mutating_action",
    [
        {"type": "Http", "inputs": {"method": "DELETE", "uri": "https://example.com"}},
        {"type": "ApiConnection", "inputs": {"method": "post"}},
        {"type": "Scope", "actions": {"Nested": {"type": "Compose", "inputs": {}}}},
    ],
)
def test_logic_apps_rejects_nested_mutating_or_unknown_actions(
    monkeypatch,
    config,
    mutating_action,
):
    resources = live_resources(config)
    resources["contoso-agents-approvals-loop"]["properties"]["definition"]["actions"][
        "Approval_triage_agent"
    ]["tools"]["Create_approval_recommendation"]["actions"]["Unsafe"] = mutating_action
    install_live_azure(monkeypatch, config, resources=resources)
    report = control_plane.verify(config)
    approvals = platform_result(report, "contoso-approvals")
    assert not approvals.ok
    assert any("non-allowlisted action type" in error for error in approvals.errors)


def test_logic_apps_rejects_extra_top_level_action(monkeypatch, config):
    resources = live_resources(config)
    resources["contoso-agents-approvals-loop"]["properties"]["definition"]["actions"]["Delete_external"] = {
        "type": "Http",
        "inputs": {"method": "DELETE", "uri": "https://example.com"},
    }
    install_live_azure(monkeypatch, config, resources=resources)
    approvals = platform_result(control_plane.verify(config), "contoso-approvals")
    assert not approvals.ok
    assert any("action set differs" in error for error in approvals.errors)
    assert any("non-allowlisted action type" in error for error in approvals.errors)


def test_logic_apps_rejects_synthetic_false_schema(monkeypatch, config):
    resources = live_resources(config)
    trigger = resources["contoso-agents-approvals-loop"]["properties"]["definition"]["triggers"][
        "Receive_synthetic_approval_scenario"
    ]
    trigger["inputs"]["schema"]["properties"]["synthetic"]["enum"] = [True, False]
    install_live_azure(monkeypatch, config, resources=resources)
    approvals = platform_result(control_plane.verify(config), "contoso-approvals")
    assert not approvals.ok
    assert "Logic Apps request schema must accept only explicit synthetic=true scenarios" in approvals.errors


def test_logic_apps_rejects_missing_pre_action_synthetic_condition(monkeypatch, config):
    resources = live_resources(config)
    trigger = resources["contoso-agents-approvals-loop"]["properties"]["definition"]["triggers"][
        "Receive_synthetic_approval_scenario"
    ]
    trigger["conditions"] = []
    install_live_azure(monkeypatch, config, resources=resources)
    approvals = platform_result(control_plane.verify(config), "contoso-approvals")
    assert not approvals.ok
    assert (
        "Logic Apps request trigger must reject synthetic values other than true before actions run"
        in approvals.errors
    )


@pytest.mark.parametrize(
    "action_name",
    ["Build_recommendation", "Create_synthetic_review_envelope"],
)
def test_logic_apps_preserves_validated_synthetic_value(monkeypatch, config, action_name):
    resources = live_resources(config)
    actions = resources["contoso-agents-approvals-loop"]["properties"]["definition"]["actions"]
    if action_name == "Build_recommendation":
        action = actions["Approval_triage_agent"]["tools"]["Create_approval_recommendation"]["actions"][action_name]
    else:
        action = actions[action_name]
    action["inputs"]["synthetic"] = True
    install_live_azure(monkeypatch, config, resources=resources)
    approvals = platform_result(control_plane.verify(config), "contoso-approvals")
    assert not approvals.ok
    assert any(
        "preserve the validated synthetic marker" in error or "human-review envelope" in error
        for error in approvals.errors
    )


def test_sre_rejects_additional_managed_resource(monkeypatch, config):
    resources = live_resources(config)
    resources["contoso-agents-sre-control-plane"]["properties"]["knowledgeGraphConfiguration"][
        "managedResources"
    ].append("/subscriptions/redacted/resourceGroups/foreign")
    install_live_azure(monkeypatch, config, resources=resources)
    sre = platform_result(control_plane.verify(config), "contoso-sre")
    assert not sre.ok
    assert "SRE Agent must manage only the exact project-owned resource group" in sre.errors


def test_sre_rejects_mismatched_action_identity(monkeypatch, config):
    resources = live_resources(config)
    resources["contoso-agents-sre-control-plane"]["properties"]["actionConfiguration"][
        "identity"
    ] = "different"
    install_live_azure(monkeypatch, config, resources=resources)
    sre = platform_result(control_plane.verify(config), "contoso-sre")
    assert not sre.ok
    assert "SRE Agent action and knowledge graph must use the exact sole dedicated UAMI" in sre.errors


def test_sre_rejects_multiple_user_assigned_identities(monkeypatch, config):
    resources = live_resources(config)
    resources["contoso-agents-sre-control-plane"]["identity"]["userAssignedIdentities"]["extra-uami"] = {}
    install_live_azure(monkeypatch, config, resources=resources)
    sre = platform_result(control_plane.verify(config), "contoso-sre")
    assert not sre.ok
    assert "SRE Agent action and knowledge graph must use the exact sole dedicated UAMI" in sre.errors


def test_sre_rejects_wider_managed_identity_rbac(monkeypatch, config):
    assignments = expected_role_assignments(config)
    assignments["sre-uami-principal"].append(
        role_assignment(
            "sre-uami-principal",
            config["platforms"][0]["rbac"]["resource_group_role_definition_ids"][0],
            "/subscriptions/redacted",
            "ServicePrincipal",
        )
    )
    install_live_azure(monkeypatch, config, assignments=assignments)
    sre = platform_result(control_plane.verify(config), "contoso-sre")
    assert not sre.ok
    assert "SRE Agent user-assigned identity RBAC differs from the exact scoped contract" in sre.errors


def test_sre_rejects_system_identity_rbac_drift(monkeypatch, config):
    assignments = expected_role_assignments(config)
    assignments["sre-system-principal"].pop()
    install_live_azure(monkeypatch, config, assignments=assignments)
    sre = platform_result(control_plane.verify(config), "contoso-sre")
    assert not sre.ok
    assert "SRE Agent system-assigned identity RBAC differs from the exact scoped contract" in sre.errors


def test_sre_rejects_operator_administrator_role(monkeypatch, config):
    assignments = expected_role_assignments(config)
    assignments["operator-group-principal"].append(
        role_assignment(
            "operator-group-principal",
            config["platforms"][0]["rbac"]["forbidden_operator_role_definition_ids"][0],
            resource_ids(config)["sre"],
            "Group",
        )
    )
    install_live_azure(monkeypatch, config, assignments=assignments)
    sre = platform_result(control_plane.verify(config), "contoso-sre")
    assert not sre.ok
    assert "SRE operator group must have only Standard User at the exact agent scope" in sre.errors


@pytest.mark.parametrize("scope_name", ["sre", "resource_group"])
def test_sre_rejects_any_other_operator_role_at_agent_or_wider_scope(monkeypatch, config, scope_name):
    assignments = expected_role_assignments(config)
    assignments["operator-group-principal"].append(
        role_assignment(
            "operator-group-principal",
            "unapproved-role",
            resource_ids(config)[scope_name],
            "Group",
        )
    )
    install_live_azure(monkeypatch, config, assignments=assignments)
    sre = platform_result(control_plane.verify(config), "contoso-sre")
    assert not sre.ok
    assert "SRE operator group must have only Standard User at the exact agent scope" in sre.errors


def test_sre_requires_operator_group(monkeypatch, config):
    monkeypatch.delenv("SRE_OPERATOR_GROUP_OBJECT_ID")
    install_live_azure(monkeypatch, config)
    sre = platform_result(control_plane.verify(config), "contoso-sre")
    assert not sre.ok
    assert "SRE operator group is missing or its role assignments could not be read" in sre.errors


def test_sre_must_use_the_shared_application_insights(monkeypatch, config):
    resources = live_resources(config)
    resources["contoso-agents-insights"]["properties"]["AppId"] = "different"
    install_live_azure(monkeypatch, config, resources=resources)
    sre = platform_result(control_plane.verify(config), "contoso-sre")
    assert not sre.ok
    assert "SRE Agent is not connected to the exact shared Application Insights resource" in sre.errors


def test_logic_apps_observability_is_explicitly_unsupported(config):
    approvals = next(entry for entry in config["platforms"] if entry["id"] == "contoso-approvals")
    assert approvals["control_plane"]["observability"] == "unsupported"


def test_action_graph_copy_isolation(config):
    original = logic_resource(config)
    copied = deepcopy(original)
    copied["properties"]["definition"]["actions"]["Approval_triage_agent"]["tools"].clear()
    assert logic_resource(config) == original
