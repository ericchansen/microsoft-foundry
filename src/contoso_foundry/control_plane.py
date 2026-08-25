"""Machine-checkable platform coverage for Foundry Control Plane."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import azure_cli


@dataclass
class PlatformResult:
    platform_id: str
    display_name: str
    resource_name: str
    resource_type: str
    present: bool = False
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.present and not self.errors


@dataclass
class InventoryReport:
    generated_at: str
    resource_group: str
    live: bool
    platforms: list[PlatformResult]

    @property
    def ok(self) -> bool:
        return bool(self.platforms) and all(platform.ok for platform in self.platforms)


def load_config(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path} must be a mapping")
    return document


def _check_config(config: dict[str, Any]) -> list[PlatformResult]:
    platforms = config.get("platforms")
    if not isinstance(platforms, list) or not platforms:
        raise ValueError("control-plane platform config must declare at least one platform")

    results: list[PlatformResult] = []
    seen: set[tuple[str, str]] = set()
    for entry in platforms:
        required = ("id", "display_name", "resource_name", "resource_type", "api_version", "control_plane", "source")
        missing = [key for key in required if not entry.get(key)]
        if missing:
            raise ValueError(f"platform entry is missing required fields: {missing}")

        key = (str(entry["resource_type"]).lower(), str(entry["resource_name"]).lower())
        if key in seen:
            raise ValueError(f"duplicate platform resource: {entry['resource_type']}/{entry['resource_name']}")
        seen.add(key)

        control_plane = entry["control_plane"]
        if control_plane.get("discovery") != "automatic":
            raise ValueError(f"{entry['id']} must declare automatic Control Plane discovery")
        observability = control_plane.get("observability")
        if observability not in {"supported", "unsupported"}:
            raise ValueError(f"{entry['id']} has invalid observability status {observability!r}")
        if str(entry["resource_type"]).lower() == "microsoft.app/agents":
            if any(
                not entry.get(key)
                for key in (
                    "application_insights_name",
                    "user_assigned_identity_name",
                    "operator_group_principal_environment",
                    "rbac",
                )
            ):
                raise ValueError(f"{entry['id']} must declare its identity, telemetry, operator, and RBAC contract")
            rbac = entry["rbac"]
            required_rbac = {
                "resource_group_role_definition_ids",
                "application_insights_role_definition_ids",
                "operator_role_definition_id",
                "forbidden_operator_role_definition_ids",
            }
            if not isinstance(rbac, dict) or any(not rbac.get(key) for key in required_rbac):
                raise ValueError(f"{entry['id']} must declare every required RBAC role definition")

        results.append(
            PlatformResult(
                platform_id=str(entry["id"]),
                display_name=str(entry["display_name"]),
                resource_name=str(entry["resource_name"]),
                resource_type=str(entry["resource_type"]),
                checks=["config:complete", "config:control-plane-coverage"],
            )
        )
    return results


def _identity_type(resource: dict[str, Any]) -> str:
    return str((resource.get("identity") or {}).get("type", "")).replace(" ", "")


def _normalize_resource_id(value: Any) -> str:
    return str(value or "").rstrip("/").lower()


def _role_definition_id(assignment: dict[str, Any]) -> str:
    return str(assignment.get("roleDefinitionId") or "").rstrip("/").rsplit("/", maxsplit=1)[-1].lower()


def _scope_applies_to_resource(scope: Any, resource_id: Any) -> bool:
    normalized_scope = _normalize_resource_id(scope)
    normalized_resource = _normalize_resource_id(resource_id)
    return bool(
        normalized_scope
        and normalized_resource
        and (
            normalized_scope == normalized_resource
            or normalized_resource.startswith(f"{normalized_scope}/")
            or normalized_scope.startswith(f"{normalized_resource}/")
            or normalized_scope.startswith("/providers/microsoft.management/managementgroups/")
        )
    )


def _workflow_actions(resource: dict[str, Any]) -> Any:
    definition = (resource.get("properties") or {}).get("definition") or {}
    return definition.get("actions") or {}


_ACTION_KEYS = {
    "Agent": frozenset({"type", "inputs", "tools", "runAfter", "limit"}),
    "Compose": frozenset({"type", "inputs", "runAfter"}),
    "Response": frozenset({"type", "kind", "inputs", "runAfter"}),
}
_TOOL_KEYS = frozenset({"description", "agentParameterSchema", "actions"})


def _validate_action_map(
    actions: Any,
    *,
    path: str,
    allowed_types: frozenset[str],
) -> list[str]:
    if not isinstance(actions, dict) or not actions:
        return [f"{path} must contain at least one allowlisted action"]

    errors: list[str] = []
    for name, action in actions.items():
        action_path = f"{path}.{name}"
        if not isinstance(action, dict):
            errors.append(f"{action_path} is not an action object")
            continue

        action_type = action.get("type")
        if not isinstance(action_type, str) or action_type not in allowed_types:
            errors.append(f"{action_path} uses non-allowlisted action type {action_type!r}")
            continue

        unexpected_keys = sorted(set(action) - _ACTION_KEYS[action_type])
        if unexpected_keys:
            errors.append(f"{action_path} has non-allowlisted fields {unexpected_keys}")

        if action_type != "Agent":
            continue

        tools = action.get("tools")
        if not isinstance(tools, dict) or not tools:
            errors.append(f"{action_path} must expose at least one bounded tool")
            continue
        for tool_name, tool in tools.items():
            tool_path = f"{action_path}.tools.{tool_name}"
            if not isinstance(tool, dict):
                errors.append(f"{tool_path} is not a tool object")
                continue
            unexpected_tool_keys = sorted(set(tool) - _TOOL_KEYS)
            if unexpected_tool_keys:
                errors.append(f"{tool_path} has non-allowlisted fields {unexpected_tool_keys}")
            errors.extend(
                _validate_action_map(
                    tool.get("actions"),
                    path=f"{tool_path}.actions",
                    allowed_types=frozenset({"Compose"}),
                )
            )
    return errors


def _validate_logic_workflow(resource: dict[str, Any]) -> list[str]:
    definition = (resource.get("properties") or {}).get("definition") or {}
    errors: list[str] = []

    triggers = definition.get("triggers")
    if not isinstance(triggers, dict) or set(triggers) != {"Receive_synthetic_approval_scenario"}:
        errors.append("Logic Apps workflow must expose only the synthetic request trigger")
        return errors

    trigger = triggers["Receive_synthetic_approval_scenario"]
    if not isinstance(trigger, dict):
        errors.append("Logic Apps synthetic request trigger is not an object")
        return errors
    if set(trigger) - {"type", "kind", "inputs", "conditions"}:
        errors.append("Logic Apps synthetic request trigger has non-allowlisted fields")
    if trigger.get("type") != "Request" or trigger.get("kind") != "Http":
        errors.append("Logic Apps workflow must use the allowlisted HTTP Request trigger")

    inputs = trigger.get("inputs") or {}
    schema = inputs.get("schema") if isinstance(inputs, dict) else {}
    schema = schema if isinstance(schema, dict) else {}
    schema_properties = schema.get("properties") or {}
    schema_properties = schema_properties if isinstance(schema_properties, dict) else {}
    synthetic_schema = schema_properties.get("synthetic") or {}
    synthetic_schema = synthetic_schema if isinstance(synthetic_schema, dict) else {}
    if (
        schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
        or set(schema.get("required") or []) != {"scenario", "synthetic"}
        or synthetic_schema.get("type") != "boolean"
        or synthetic_schema.get("enum") != [True]
    ):
        errors.append("Logic Apps request schema must accept only explicit synthetic=true scenarios")

    expected_condition = "@equals(triggerBody()?['synthetic'], true)"
    if trigger.get("conditions") != [{"expression": expected_condition}]:
        errors.append("Logic Apps request trigger must reject synthetic values other than true before actions run")

    actions = _workflow_actions(resource)
    if not isinstance(actions, dict):
        errors.append("Logic Apps workflow actions are not an action mapping")
        return errors
    expected_actions = {
        "Approval_triage_agent",
        "Create_synthetic_review_envelope",
        "Return_recommendation",
    }
    if set(actions) != expected_actions:
        errors.append("Logic Apps workflow action set differs from the exact synthetic contract")
    errors.extend(
        _validate_action_map(
            actions,
            path="actions",
            allowed_types=frozenset({"Agent", "Compose", "Response"}),
        )
    )

    agent = actions.get("Approval_triage_agent") or {}
    tools = agent.get("tools") or {}
    if set(tools) != {"Create_approval_recommendation"}:
        errors.append("Logic Apps Agent must expose only the synthetic recommendation tool")
    tool_actions = (tools.get("Create_approval_recommendation") or {}).get("actions") or {}
    if set(tool_actions) != {"Build_recommendation"}:
        errors.append("Logic Apps recommendation tool action set differs from the exact synthetic contract")
    tool_inputs = (tool_actions.get("Build_recommendation") or {}).get("inputs") or {}
    if (
        tool_inputs.get("requiresHumanApproval") is not True
        or tool_inputs.get("synthetic") != "@triggerBody()?['synthetic']"
    ):
        errors.append("Logic Apps recommendation tool must preserve the validated synthetic marker")

    envelope = actions.get("Create_synthetic_review_envelope") or {}
    envelope_inputs = envelope.get("inputs") or {}
    if (
        envelope.get("type") != "Compose"
        or envelope_inputs.get("requiresHumanApproval") is not True
        or envelope_inputs.get("synthetic") != "@triggerBody()?['synthetic']"
    ):
        errors.append("Logic Apps workflow lacks the mandatory synthetic human-review envelope")

    response = actions.get("Return_recommendation") or {}
    response_body = (response.get("inputs") or {}).get("body") or {}
    if response_body.get("result") != "@outputs('Create_synthetic_review_envelope')":
        errors.append("Logic Apps response must return the validated synthetic review envelope")
    return errors


def _assignment_contract(assignments: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (
            _role_definition_id(assignment),
            _normalize_resource_id(assignment.get("scope")),
            str(assignment.get("principalType") or "").lower(),
        )
        for assignment in assignments
    }


def _check_sre_platform(
    entry: dict[str, Any],
    result: PlatformResult,
    resource: dict[str, Any],
    context: dict[str, Any],
) -> None:
    properties = resource.get("properties") or {}
    application_insights = context.get("application_insights") or {}
    app_insights = (properties.get("logConfiguration") or {}).get("applicationInsightsConfiguration") or {}
    expected_app_id = (application_insights.get("properties") or {}).get("AppId")
    if not app_insights.get("appId") or app_insights.get("appId") != expected_app_id:
        result.errors.append("SRE Agent is not connected to the exact shared Application Insights resource")
    else:
        result.checks.append("live:application-insights")

    user_identity = context.get("user_identity") or {}
    expected_uami_id = _normalize_resource_id(user_identity.get("id"))
    configured_uamis = {
        _normalize_resource_id(identity_id)
        for identity_id in ((resource.get("identity") or {}).get("userAssignedIdentities") or {})
    }
    action = properties.get("actionConfiguration") or {}
    knowledge = properties.get("knowledgeGraphConfiguration") or {}
    if (
        not expected_uami_id
        or configured_uamis != {expected_uami_id}
        or _normalize_resource_id(action.get("identity")) != expected_uami_id
        or _normalize_resource_id(knowledge.get("identity")) != expected_uami_id
    ):
        result.errors.append("SRE Agent action and knowledge graph must use the exact sole dedicated UAMI")
    else:
        result.checks.append("live:exact-uami")

    expected_resource_group_id = _normalize_resource_id(context.get("resource_group_id"))
    managed_resources = [_normalize_resource_id(item) for item in knowledge.get("managedResources") or []]
    if not expected_resource_group_id or managed_resources != [expected_resource_group_id]:
        result.errors.append("SRE Agent must manage only the exact project-owned resource group")
    else:
        result.checks.append("live:owned-scope-only")

    if action.get("accessLevel") != "Low" or action.get("mode") != "Review":
        result.errors.append("SRE Agent must use Low access and Review mode")
    else:
        result.checks.append("live:review-only")

    if properties.get("powerState") not in {"Running", "Stopped"}:
        result.errors.append("SRE Agent has no recognized lifecycle power state")
    else:
        result.checks.append("live:lifecycle-state")

    rbac = entry["rbac"]
    app_insights_id = _normalize_resource_id(application_insights.get("id"))
    expected_identity_assignments = {
        (str(role_id).lower(), expected_resource_group_id, "serviceprincipal")
        for role_id in rbac["resource_group_role_definition_ids"]
    } | {
        (str(role_id).lower(), app_insights_id, "serviceprincipal")
        for role_id in rbac["application_insights_role_definition_ids"]
    }

    assignments = context.get("role_assignments") or {}
    identity_principals = {
        "user-assigned": (user_identity.get("properties") or {}).get("principalId"),
        "system-assigned": (resource.get("identity") or {}).get("principalId"),
    }
    identity_rbac_ok = True
    for label, principal_id in identity_principals.items():
        principal_assignments = assignments.get(str(principal_id)) if principal_id else None
        has_exact_assignments = (
            principal_assignments is not None
            and _assignment_contract(principal_assignments) == expected_identity_assignments
        )
        if not has_exact_assignments:
            result.errors.append(f"SRE Agent {label} identity RBAC differs from the exact scoped contract")
            identity_rbac_ok = False
    if identity_rbac_ok:
        result.checks.append("live:exact-identity-rbac")

    operator_principal_id = context.get("operator_principal_id")
    operator_assignments = assignments.get(str(operator_principal_id)) if operator_principal_id else None
    expected_operator_role = str(rbac["operator_role_definition_id"]).lower()
    if operator_assignments is None:
        result.errors.append("SRE operator group is missing or its role assignments could not be read")
    else:
        forbidden_operator_roles = {
            str(role_id).lower() for role_id in rbac["forbidden_operator_role_definition_ids"]
        }
        relevant_operator_assignments = [
            assignment
            for assignment in operator_assignments
            if _scope_applies_to_resource(assignment.get("scope"), resource.get("id"))
            or _role_definition_id(assignment) in forbidden_operator_roles
        ]
        expected_operator_assignment = {
            (
                expected_operator_role,
                _normalize_resource_id(resource.get("id")),
                "group",
            )
        }
        if _assignment_contract(relevant_operator_assignments) != expected_operator_assignment:
            result.errors.append("SRE operator group must have only Standard User at the exact agent scope")
        else:
            result.checks.append("live:operator-standard-user")


def _check_live_platform(
    entry: dict[str, Any],
    result: PlatformResult,
    resource: dict[str, Any],
    ownership_tags: dict[str, str],
    context: dict[str, Any] | None = None,
) -> None:
    result.present = True
    result.checks.append("live:resource-present")

    actual_tags = resource.get("tags") or {}
    mismatched = {key: value for key, value in ownership_tags.items() if actual_tags.get(key) != value}
    if mismatched:
        result.errors.append(f"ownership tags differ for: {sorted(mismatched)}")
    else:
        result.checks.append("live:ownership-tags")

    expected_identity = str(entry.get("identity_type", "")).replace(" ", "")
    if expected_identity and _identity_type(resource) != expected_identity:
        result.errors.append(f"identity type is {_identity_type(resource)!r}; expected {expected_identity!r}")
    else:
        result.checks.append("live:dedicated-identity")

    if entry["resource_type"].lower() == "microsoft.app/agents":
        _check_sre_platform(entry, result, resource, context or {})

    if entry["resource_type"].lower() == "microsoft.logic/workflows":
        properties = resource.get("properties") or {}
        workflow_errors = _validate_logic_workflow(resource)
        if workflow_errors:
            result.errors.extend(workflow_errors)
        else:
            result.checks.extend(
                [
                    "live:agent-loop",
                    "live:recursive-action-allowlist",
                    "live:synthetic-only-gate",
                    "live:human-review-envelope",
                ]
            )

        if properties.get("state") not in {"Enabled", "Disabled"}:
            result.errors.append("Logic Apps workflow has no recognized lifecycle state")
        else:
            result.checks.append("live:lifecycle-state")


def verify(config: dict[str, Any], *, live: bool = True) -> InventoryReport:
    results = _check_config(config)
    report = InventoryReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        resource_group=str(config.get("resource_group", "")),
        live=live,
        platforms=results,
    )
    if not live:
        for result in report.platforms:
            result.present = True
            result.checks.append("static:live-check-skipped")
        return report

    ownership_tags = {str(key): str(value) for key, value in (config.get("ownership_tags") or {}).items()}
    entries = {str(entry["id"]): entry for entry in config["platforms"]}
    for result in report.platforms:
        entry = entries[result.platform_id]
        resource = azure_cli.try_run(
            [
                "resource",
                "show",
                "--resource-group",
                report.resource_group,
                "--name",
                result.resource_name,
                "--resource-type",
                result.resource_type,
                "--api-version",
                str(entry["api_version"]),
            ],
            default=None,
        )
        if resource is None:
            result.errors.append("resource was not returned by Azure Resource Manager")
            continue

        context: dict[str, Any] = {}
        if result.resource_type.lower() == "microsoft.app/agents":
            application_insights = azure_cli.try_run(
                [
                    "resource",
                    "show",
                    "--resource-group",
                    report.resource_group,
                    "--name",
                    str(entry["application_insights_name"]),
                    "--resource-type",
                    "Microsoft.Insights/components",
                    "--api-version",
                    "2020-02-02",
                ],
                default=None,
            )
            user_identity = azure_cli.try_run(
                [
                    "resource",
                    "show",
                    "--resource-group",
                    report.resource_group,
                    "--name",
                    str(entry["user_assigned_identity_name"]),
                    "--resource-type",
                    "Microsoft.ManagedIdentity/userAssignedIdentities",
                    "--api-version",
                    "2024-11-30",
                ],
                default=None,
            )
            resource_group = azure_cli.try_run(
                ["group", "show", "--name", report.resource_group],
                default=None,
            )
            operator_principal_id = os.environ.get(str(entry["operator_group_principal_environment"]))

            principals = {
                ((user_identity or {}).get("properties") or {}).get("principalId"),
                (resource.get("identity") or {}).get("principalId"),
                operator_principal_id,
            }
            role_assignments = {
                str(principal_id): azure_cli.try_run(
                    [
                        "role",
                        "assignment",
                        "list",
                        "--assignee-object-id",
                        str(principal_id),
                        "--all",
                        "--include-inherited",
                    ],
                    default=None,
                )
                for principal_id in principals
                if principal_id
            }
            context = {
                "application_insights": application_insights,
                "user_identity": user_identity,
                "resource_group_id": (resource_group or {}).get("id"),
                "operator_principal_id": operator_principal_id,
                "role_assignments": role_assignments,
            }

        _check_live_platform(
            entry,
            result,
            resource,
            ownership_tags,
            context,
        )
    return report


def render_markdown(report: InventoryReport) -> str:
    verdict = "PASS" if report.ok else "FAIL"
    lines = [
        "# Foundry Control Plane platform inventory",
        "",
        f"Generated: `{report.generated_at}`  ",
        f"Boundary: **`{report.resource_group}`**  ",
        f"Mode: **{'live' if report.live else 'static'}**  ",
        f"Result: **{verdict}**",
        "",
        "| Platform | Resource type | Present | Result |",
        "| --- | --- | :---: | :---: |",
    ]
    for platform in report.platforms:
        lines.append(
            f"| {platform.display_name} | `{platform.resource_type}` | "
            f"{'yes' if platform.present else 'no'} | {'PASS' if platform.ok else 'FAIL'} |"
        )
    lines += ["", "## Checks", ""]
    for platform in report.platforms:
        lines.append(f"### {platform.display_name}")
        lines.append("")
        lines += [f"- `{check}`" for check in platform.checks]
        lines += [f"- **Error:** {error}" for error in platform.errors]
        if not platform.errors:
            lines.append("- No errors.")
        lines.append("")
    return "\n".join(lines)


def to_json(report: InventoryReport) -> str:
    return json.dumps(
        {
            "generated_at": report.generated_at,
            "resource_group": report.resource_group,
            "live": report.live,
            "ok": report.ok,
            "platforms": [
                {
                    "id": platform.platform_id,
                    "display_name": platform.display_name,
                    "resource_name": platform.resource_name,
                    "resource_type": platform.resource_type,
                    "present": platform.present,
                    "ok": platform.ok,
                    "checks": platform.checks,
                    "errors": platform.errors,
                }
                for platform in report.platforms
            ],
        },
        indent=2,
    )
