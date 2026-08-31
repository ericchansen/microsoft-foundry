"""Ownership-boundary enforcement.

One rule: **everything this project creates, assigns, or deletes lives inside a
single resource group.** Everything else in the subscription is somebody else's
and is read-only.

This module is the gate that runs *before* the first provisioning step. It
checks the declared plan statically (no network needed) and then, if the CLI is
available, confirms the live subscription still matches what the plan assumes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml

from . import azure_cli

RESOURCE_GROUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._\-()]{0,88}[a-z0-9_()]$")

#: Role-assignment scopes that are never acceptable for this project.
# Scopes that would place a role assignment above the resource group. Two shapes
# have to be covered: ARM paths, and the human-readable words a plan author is
# most likely to reach for. `scope: subscription` is relative and contains no
# `subscriptions/` segment, so nothing else in this module would catch it.
_FORBIDDEN_SCOPE_PREFIXES = ("/", "/subscriptions", "/providers/Microsoft.Management")
_FORBIDDEN_SCOPE_WORDS = frozenset(
    {"subscription", "tenant", "management-group", "management group", "root", "/"}
)


@dataclass
class Violation:
    check: str
    subject: str
    message: str

    def __str__(self) -> str:
        return f"[{self.check}] {self.subject}: {self.message}"


@dataclass
class BoundaryReport:
    generated_at: str
    resource_group: str
    violations: list[Violation] = field(default_factory=list)
    protected_resource_groups: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)
    live: bool = False
    target_exists: bool | None = None

    @property
    def ok(self) -> bool:
        return not self.violations

    def fail(self, check: str, subject: str, message: str) -> None:
        self.violations.append(Violation(check, subject, message))


def load_plan(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a mapping")
    return data


def _iter_scoped_entries(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Flatten every plan section that declares something with a scope."""
    entries: list[tuple[str, dict[str, Any]]] = []
    for section in (
        "resources",
        "identities",
        "role_assignments",
        "diagnostic_settings",
        "policy_assignments",
        "teardown",
    ):
        for entry in plan.get(section, []) or []:
            entries.append((section, entry))
    return entries


def _enabled_modules(plan: dict[str, Any], requested: Iterable[str] | None = None) -> set[str]:
    modules = plan.get("modules") or {"core": {"enabled_by_default": True}}
    if not isinstance(modules, dict) or not modules:
        raise ValueError("boundary modules must be a non-empty mapping")
    enabled = {
        str(name)
        for name, definition in modules.items()
        if isinstance(definition, dict) and definition.get("enabled_by_default") is True
    }
    requested_set = {str(name) for name in requested or []}
    unknown = requested_set - set(modules)
    if unknown:
        raise ValueError(f"unknown boundary module(s): {sorted(unknown)}")
    return enabled | requested_set


def enabled_modules_from_environment() -> set[str]:
    return {
        module.strip()
        for module in os.environ.get("FOUNDRY_ENABLED_MODULES", "").split(",")
        if module.strip()
    }


def _active_plan(plan: dict[str, Any], requested: Iterable[str] | None = None) -> dict[str, Any]:
    enabled = _enabled_modules(plan, requested)
    active = dict(plan)
    for section in (
        "resources",
        "identities",
        "role_assignments",
        "diagnostic_settings",
        "policy_assignments",
        "teardown",
        "external_principals",
    ):
        active[section] = [
            entry
            for entry in plan.get(section, []) or []
            if str(entry.get("module", "core")) in enabled
        ]
    return active


def _relative_live_scope(resource_id: str, resource_group: str) -> str:
    normalized = resource_id.strip().lower().rstrip("/")
    marker = f"/resourcegroups/{resource_group.lower()}"
    marker_index = normalized.find(marker)
    if marker_index < 0:
        return normalized
    suffix = normalized[marker_index + len(marker) :]
    if suffix and not suffix.startswith("/"):
        return normalized
    relative = suffix.lstrip("/")
    return relative or "."


def _scope_matches(scope: str, pattern: str) -> bool:
    """Match resource scopes without allowing wildcards to cross path segments."""
    expression = re.escape(pattern)
    expression = expression.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(expression, scope) is not None


def _arm_url(subscription_id: str, resource_group: str, relative_scope: str, api_version: str) -> str:
    return (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}/{relative_scope}"
        f"?api-version={api_version}"
    )


def _expand_inventory_scope(
    pattern: str,
    live_resources: dict[str, dict[str, Any]],
) -> list[str]:
    if "*" not in pattern and "?" not in pattern:
        return [pattern]
    segments = pattern.split("/")
    wildcard_index = next(
        index
        for index, segment in enumerate(segments)
        if "*" in segment or "?" in segment
    )
    parent_pattern = "/".join(segments[: wildcard_index + 1])
    suffix = segments[wildcard_index + 1 :]
    return [
        "/".join([scope, *suffix])
        for scope in live_resources
        if len(scope.split("/")) == wildcard_index + 1
        and _scope_matches(scope, parent_pattern)
    ]


def _augment_declared_inventory(
    report: BoundaryReport,
    plan: dict[str, Any],
    live_resources: dict[str, dict[str, Any]],
) -> None:
    """Read ARM children omitted by ``az resource list`` and data-plane agents."""
    arm_entries = [
        entry
        for section in ("resources", "identities")
        for entry in plan.get(section, []) or []
        if entry.get("inventory_api_version")
    ]
    if arm_entries:
        account = azure_cli.run(["account", "show"]) or {}
        subscription_id = str(account.get("id", "")).strip()
        if not subscription_id:
            raise azure_cli.AzureCliError("Azure CLI returned no subscription ID")
        queried_collections: set[str] = set()
        for entry in arm_entries:
            api_version = str(entry["inventory_api_version"])
            collection = str(entry.get("inventory_collection", "")).strip().lower()
            scope = str(entry.get("scope", "")).strip().lower()
            query_scopes = _expand_inventory_scope(collection or scope, live_resources)
            for query_scope in query_scopes:
                if collection and query_scope in queried_collections:
                    continue
                queried_collections.add(query_scope)
                next_url = _arm_url(
                    subscription_id,
                    report.resource_group,
                    query_scope,
                    api_version,
                )
                visited_urls: set[str] = set()
                while next_url:
                    if next_url in visited_urls:
                        raise ValueError(f"{query_scope}: ARM inventory pagination loop")
                    visited_urls.add(next_url)
                    parsed = urlparse(next_url)
                    expected_prefix = (
                        f"/subscriptions/{subscription_id}/resourcegroups/"
                        f"{report.resource_group}/"
                    ).lower()
                    if (
                        parsed.scheme != "https"
                        or parsed.netloc.lower() != "management.azure.com"
                        or not parsed.path.lower().startswith(expected_prefix)
                    ):
                        raise ValueError(
                            f"{query_scope}: ARM inventory continuation left the owned resource group"
                        )
                    payload = azure_cli.run(
                        [
                            "rest",
                            "--method",
                            "get",
                            "--url",
                            next_url,
                        ]
                    )
                    resources = (
                        payload.get("value", [])
                        if isinstance(payload, dict) and "value" in payload
                        else [payload]
                    )
                    for resource in resources:
                        if isinstance(resource, dict) and resource.get("id"):
                            live_resources[
                                _relative_live_scope(
                                    str(resource["id"]),
                                    report.resource_group,
                                )
                            ] = resource
                    next_url = (
                        str(payload.get("nextLink", "")).strip()
                        if isinstance(payload, dict)
                        else ""
                    )

    data_plane_agents = [
        entry
        for entry in plan.get("resources", []) or []
        if entry.get("inventory_data_plane") == "foundry_agent"
    ]
    if not data_plane_agents:
        return
    token_payload = azure_cli.run(
        ["account", "get-access-token", "--scope", "https://ai.azure.com/.default"]
    ) or {}
    token = str(token_payload.get("accessToken", ""))
    if not token:
        raise azure_cli.AzureCliError("Azure CLI returned no Foundry access token")
    queried_projects: set[tuple[str, str]] = set()
    for entry in data_plane_agents:
        scope = str(entry.get("scope", "")).strip().lower()
        match = re.fullmatch(
            r"providers/microsoft\.cognitiveservices/accounts/([^/]+)/projects/([^/]+)/agents/([^/]+)",
            scope,
        )
        if not match:
            raise ValueError(f"{entry.get('name')}: invalid Foundry agent inventory scope")
        account_name, project_name, _agent_name = match.groups()
        project_key = (account_name, project_name)
        if project_key in queried_projects:
            continue
        queried_projects.add(project_key)
        after = ""
        seen_continuations: set[str] = set()
        while True:
            params = {"api-version": "v1", "limit": 100}
            if after:
                params["after"] = after
            response = requests.get(
                (
                    f"https://{account_name}.services.ai.azure.com/api/projects/"
                    f"{project_name}/agents"
                ),
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            agents = payload.get("data", [])
            if not isinstance(agents, list):
                raise ValueError("Foundry returned an invalid agent collection")
            for agent in agents:
                agent_name = str(agent.get("name", "")).strip().lower()
                if not agent_name:
                    raise ValueError("Foundry returned an agent without a name")
                agent_scope = (
                    "providers/microsoft.cognitiveservices/accounts/"
                    f"{account_name}/projects/{project_name}/agents/{agent_name}"
                )
                live_resources[agent_scope] = {
                    "id": f"/resourceGroups/{report.resource_group}/{agent_scope}",
                    "type": "Microsoft.CognitiveServices/accounts/projects/agents",
                    "name": agent_name,
                }
            if not payload.get("has_more"):
                break
            after = str(payload.get("last_id", "")).strip()
            if not after:
                raise ValueError("Foundry agent collection omitted its continuation ID")
            if after in seen_continuations:
                raise ValueError("Foundry agent collection repeated its continuation ID")
            seen_continuations.add(after)


def _principal_id(resource: dict[str, Any]) -> str | None:
    identity = resource.get("identity") or {}
    properties = resource.get("properties") or {}
    for value in (
        resource.get("principalId"),
        identity.get("principalId"),
        properties.get("principalId"),
    ):
        if value:
            return str(value).lower()
    return None


def _check_live_role_assignments(
    report: BoundaryReport,
    plan: dict[str, Any],
    live_resources: dict[str, dict[str, Any]],
    assignments: list[dict[str, Any]],
) -> None:
    declared_entries = {
        str(entry.get("name")): str(entry.get("scope", "")).lower()
        for section, entry in _iter_scoped_entries(plan)
        if section in {"resources", "identities"}
    }
    expected: dict[tuple[str, str, str], str] = {}
    required_expected: set[tuple[str, str, str]] = set()
    principal_aliases: dict[str, str] = {}
    external_principals: dict[str, str] = {}
    unresolved_external_principals: set[str] = set()
    for entry in plan.get("external_principals", []) or []:
        alias = str(entry.get("name", ""))
        environment_name = str(entry.get("object_id_environment", ""))
        principal_id = os.environ.get(environment_name, "").strip().lower()
        if not alias:
            report.fail(
                "live:declared-role-assignments",
                "<unnamed-external-principal>",
                "external principal must declare a name",
            )
            continue
        if not principal_id:
            if entry.get("required_live") is False:
                unresolved_external_principals.add(alias)
                continue
            report.fail(
                "live:declared-role-assignments",
                alias,
                "external principal object ID was not supplied by its declared environment variable",
            )
            continue
        external_principals[alias] = principal_id

    for assignment in plan.get("role_assignments", []) or []:
        assignment_name = str(assignment.get("name", "<unnamed>"))
        principal_alias = str(assignment.get("principal", ""))
        external_principal_id = external_principals.get(principal_alias)
        principal_pattern = declared_entries.get(principal_alias)
        if (
            principal_alias in unresolved_external_principals
            and assignment.get("required_live") is False
        ):
            continue
        if external_principal_id:
            principal_id = external_principal_id
            principal_aliases[principal_id] = principal_alias
        elif not principal_pattern:
            report.fail(
                "live:declared-role-assignments",
                assignment_name,
                f"principal {principal_alias!r} is not a declared resource or identity",
            )
            continue
        else:
            matching_principals = {
                principal
                for scope, resource in live_resources.items()
                if _scope_matches(scope, principal_pattern) and (principal := _principal_id(resource))
            }
            if not matching_principals and assignment.get("required_live") is False:
                continue
            if len(matching_principals) != 1:
                report.fail(
                    "live:declared-role-assignments",
                    assignment_name,
                    f"principal {principal_alias!r} resolved to {len(matching_principals)} live principal IDs",
                )
                continue
            principal_id = next(iter(matching_principals))
            principal_aliases[principal_id] = principal_alias

        scope_pattern = str(assignment.get("scope", "")).lower()
        matching_scopes = (
            {"."}
            if scope_pattern == "."
            else {scope for scope in live_resources if _scope_matches(scope, scope_pattern)}
        )
        if not matching_scopes and assignment.get("required_live") is False:
            continue
        if len(matching_scopes) != 1:
            report.fail(
                "live:declared-role-assignments",
                assignment_name,
                f"scope {scope_pattern!r} resolved to {len(matching_scopes)} live resources",
            )
            continue
        resolved_scope = next(iter(matching_scopes))
        role = str(assignment.get("role", "")).lower()
        expected_key = (principal_id, role, resolved_scope)
        expected[expected_key] = (
            f"{principal_alias}:{assignment.get('role')}@{resolved_scope}"
        )
        if assignment.get("required_live") is not False:
            required_expected.add(expected_key)

    actual: dict[tuple[str, str, str], str] = {}
    for assignment in assignments:
        principal_id = str(assignment.get("principalId", "")).lower()
        role = str(assignment.get("roleDefinitionName", "")).lower()
        scope = _relative_live_scope(str(assignment.get("scope", "")), report.resource_group)
        if not principal_id or not role or not scope:
            report.fail(
                "live:declared-role-assignments",
                "<malformed>",
                "Azure returned a role assignment without principalId, roleDefinitionName, or scope",
            )
            continue
        if scope.startswith("/"):
            if principal_id in principal_aliases:
                report.fail(
                    "live:declared-role-assignments",
                    principal_aliases[principal_id],
                    f"has an out-of-bound live {assignment.get('roleDefinitionName')} assignment",
                )
            continue
        principal = principal_aliases.get(principal_id, "<undeclared-principal>")
        actual[(principal_id, role, scope)] = f"{principal}:{assignment.get('roleDefinitionName')}@{scope}"

    missing = sorted(expected[key] for key in required_expected - actual.keys())
    unexpected = sorted(actual[key] for key in actual.keys() - expected.keys())
    if missing or unexpected:
        report.fail(
            "live:declared-role-assignments",
            report.resource_group,
            f"role assignment inventory differs from the plan; missing={missing}, unexpected={unexpected}",
        )


def check_plan(plan: dict[str, Any], *, expected_resource_group: str | None = None) -> BoundaryReport:
    """Static validation. Requires no credentials, so CI always runs it."""
    declared_rg = str(plan.get("resource_group", ""))
    report = BoundaryReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        resource_group=declared_rg,
    )

    report.checks_run.append("plan:modules")
    modules = plan.get("modules") or {"core": {"enabled_by_default": True}}
    if not isinstance(modules, dict) or "core" not in modules:
        report.fail("plan:modules", "modules", "must declare a core module")
    else:
        for name, definition in modules.items():
            if not isinstance(definition, dict) or not isinstance(definition.get("enabled_by_default"), bool):
                report.fail(
                    "plan:modules",
                    str(name),
                    "must declare enabled_by_default as a boolean",
                )
        known_modules = set(modules)
        for section, entry in _iter_scoped_entries(plan):
            module = str(entry.get("module", "core"))
            if module not in known_modules:
                report.fail(
                    "plan:modules",
                    f"{section}/{entry.get('name', '<unnamed>')}",
                    f"references unknown module {module!r}",
                )
        for entry in plan.get("external_principals", []) or []:
            name = str(entry.get("name", "<unnamed>"))
            module = str(entry.get("module", "core"))
            if module not in known_modules:
                report.fail("plan:modules", f"external_principals/{name}", f"references unknown module {module!r}")
            if not entry.get("object_id_environment"):
                report.fail(
                    "plan:modules",
                    f"external_principals/{name}",
                    "must declare object_id_environment",
                )
    report.checks_run.append("plan:resource-group-name")

    if not declared_rg or not RESOURCE_GROUP_NAME_RE.match(declared_rg):
        report.fail("plan:resource-group-name", declared_rg or "<empty>", "not a valid resource group name")

    expected = expected_resource_group or os.environ.get("FOUNDRY_RESOURCE_GROUP")
    if expected and declared_rg != expected:
        report.fail(
            "plan:resource-group-name",
            declared_rg,
            f"does not match the expected boundary {expected!r}; "
            "changing the boundary must be a deliberate, reviewed edit",
        )

    report.checks_run.append("plan:relative-scopes")
    for section, entry in _iter_scoped_entries(plan):
        name = str(entry.get("name", entry.get("id", "<unnamed>")))
        subject = f"{section}/{name}"
        scope = str(entry.get("scope", ""))

        if not scope:
            report.fail("plan:relative-scopes", subject, "no 'scope' declared")
            continue
        if scope.startswith("/"):
            report.fail(
                "plan:relative-scopes",
                subject,
                f"scope {scope!r} is absolute. Scopes must be relative to the resource group so "
                "they cannot address another subscription or resource group.",
            )
            continue
        if ".." in scope:
            report.fail("plan:relative-scopes", subject, f"scope {scope!r} traverses upwards")
            continue
        if re.search(r"resourcegroups?/", scope, re.I):
            report.fail("plan:relative-scopes", subject, f"scope {scope!r} names a resource group")
            continue
        if scope.lower().startswith("subscriptions/"):
            report.fail("plan:relative-scopes", subject, f"scope {scope!r} names a subscription")

    report.checks_run.append("plan:scope-uniqueness")
    by_scope: dict[str, list[str]] = {}
    for section in ("resources", "identities"):
        for entry in plan.get(section, []) or []:
            scope = str(entry.get("scope", "")).strip().lower().rstrip("/")
            if scope:
                by_scope.setdefault(scope, []).append(
                    f"{section}/{entry.get('name', '<unnamed>')}"
                )
    for scope, subjects in by_scope.items():
        if len(subjects) > 1:
            report.fail(
                "plan:scope-uniqueness",
                scope,
                f"scope is declared more than once: {subjects}",
            )

    report.checks_run.append("plan:no-reuse")
    for section, entry in _iter_scoped_entries(plan):
        name = str(entry.get("name", entry.get("id", "<unnamed>")))
        if section in {"resources", "identities"} and entry.get("reuse_existing"):
            report.fail(
                "plan:no-reuse",
                f"{section}/{name}",
                "declares reuse_existing. Phase 0 forbids adopting any pre-existing resource or identity.",
            )

    report.checks_run.append("plan:role-assignment-scopes")
    for entry in plan.get("role_assignments", []) or []:
        name = str(entry.get("name", "<unnamed>"))
        scope = str(entry.get("scope", ""))
        widens = (
            scope in _FORBIDDEN_SCOPE_PREFIXES
            or scope.strip().lower() in _FORBIDDEN_SCOPE_WORDS
            or scope.startswith("/subscriptions")
            or scope.startswith("/providers/Microsoft.Management")
        )
        if widens:
            report.fail("plan:role-assignment-scopes", name, f"scope {scope!r} is subscription- or tenant-wide")
        if not entry.get("role"):
            report.fail("plan:role-assignment-scopes", name, "no 'role' declared")

    report.checks_run.append("plan:diagnostic-targets")
    declared_resources = {
        str(entry.get("name"))
        for entry in plan.get("resources", []) or []
        if entry.get("name")
    }
    for entry in plan.get("diagnostic_settings", []) or []:
        name = str(entry.get("name", "<unnamed>"))
        workspace = str(entry.get("target_workspace", ""))
        if not workspace:
            report.fail("plan:diagnostic-targets", name, "no 'target_workspace' declared")
        elif workspace not in declared_resources:
            report.fail(
                "plan:diagnostic-targets",
                name,
                f"target_workspace {workspace!r} is not a resource declared in this plan, "
                "so telemetry could be routed to a shared or pre-existing workspace",
            )

    report.checks_run.append("plan:teardown-completeness")
    creatable = {
        str(e.get("name"))
        for section, e in _iter_scoped_entries(plan)
        if section in {"resources", "identities"}
    }
    teardown_targets = {str(e.get("name")) for e in plan.get("teardown", []) or []}
    if plan.get("teardown") is not None:
        missing = creatable - teardown_targets
        # Deleting the resource group removes everything inside it, so a single
        # resource-group-scoped teardown entry satisfies the whole plan.
        deletes_group = any(
            str(e.get("kind", "")).lower() == "resource-group" for e in plan.get("teardown", []) or []
        )
        if missing and not deletes_group:
            report.fail(
                "plan:teardown-completeness",
                "teardown",
                f"no teardown path declared for: {sorted(missing)}",
            )

    return report


def check_live(
    report: BoundaryReport,
    plan: dict[str, Any],
    *,
    enabled_modules: Iterable[str] | None = None,
) -> BoundaryReport:
    """Confirm the live subscription matches the plan's assumptions.

    Read-only. Records the resource groups the project must never touch, so the
    protected set is captured as evidence rather than assumed.
    """
    plan = _active_plan(plan, enabled_modules)
    try:
        groups = azure_cli.run(["group", "list"]) or []
    except azure_cli.AzureCliError as exc:
        report.checks_run.append("live:subscription-inventory")
        report.fail(
            "live:subscription-inventory",
            report.resource_group,
            f"could not inspect the live subscription: {exc}",
        )
        return report

    report.live = True
    report.checks_run.append("live:protected-resource-groups")
    names = [str(g.get("name")) for g in groups]
    report.protected_resource_groups = sorted(n for n in names if n != report.resource_group)
    report.target_exists = report.resource_group in names

    report.checks_run.append("live:target-not-adopted")
    if report.target_exists:
        target_group = next(g for g in groups if str(g.get("name")) == report.resource_group)
        expected_tags = plan.get("tags", {}) or {}
        actual_tags = target_group.get("tags", {}) or {}
        mismatched_tags = {
            key: {"expected": value, "actual": actual_tags.get(key)}
            for key, value in expected_tags.items()
            if actual_tags.get(key) != value
        }
        if mismatched_tags:
            report.fail(
                "live:target-ownership-tags",
                report.resource_group,
                f"ownership tags do not match the plan: {mismatched_tags}",
            )

        try:
            resources = azure_cli.run(["resource", "list", "-g", report.resource_group]) or []
        except azure_cli.AzureCliError as exc:
            report.checks_run.append("live:resource-inventory")
            report.fail(
                "live:resource-inventory",
                report.resource_group,
                f"could not inspect the live resource inventory: {exc}",
            )
            return report
        if not plan.get("allow_existing_resource_group", False):
            report.fail(
                "live:target-not-adopted",
                report.resource_group,
                "already exists and the plan does not permit a previously verified project-owned group",
            )
        else:
            report.checks_run.append("live:declared-resource-inventory")
            declared_entries = [
                (
                    f"{section}/{entry.get('name', '<unnamed>')}[{index}]",
                    str(entry.get("scope", "")).lower(),
                    entry,
                )
                for section in ("resources", "identities")
                for index, entry in enumerate(plan.get(section, []) or [])
            ]
            declared_scopes = [
                (subject, pattern)
                for subject, pattern, _entry in declared_entries
            ]
            live_resources = {
                _relative_live_scope(str(resource.get("id", "")), report.resource_group): resource
                for resource in resources
            }
            try:
                _augment_declared_inventory(report, plan, live_resources)
            except (azure_cli.AzureCliError, requests.RequestException, ValueError) as exc:
                report.fail(
                    "live:declared-resource-inventory",
                    report.resource_group,
                    f"could not inspect declared child resources: {exc}",
                )
            declaration_matches = {
                subject: sorted(
                    scope for scope in live_resources if _scope_matches(scope, pattern)
                )
                for subject, pattern in declared_scopes
            }
            resource_matches = {
                scope: sorted(
                    subject
                    for subject, pattern in declared_scopes
                    if _scope_matches(scope, pattern)
                )
                for scope in live_resources
            }
            invalid_declarations = {
                subject: matches
                for subject, matches in declaration_matches.items()
                if not (
                    len(matches) == int(
                        next(
                            entry.get("expected_live_count", 1)
                            for candidate, _pattern, entry in declared_entries
                            if candidate == subject
                        )
                    )
                    or (
                        not matches
                        and next(
                            entry.get("required_live", True)
                            for candidate, _pattern, entry in declared_entries
                            if candidate == subject
                        )
                        is False
                    )
                )
            }
            invalid_resources = {
                scope: matches
                for scope, matches in resource_matches.items()
                if len(matches) != 1
            }
            if invalid_declarations or invalid_resources:
                report.fail(
                    "live:declared-resource-inventory",
                    report.resource_group,
                    "resource inventory is not one-to-one with the plan; "
                    f"declaration_matches={invalid_declarations}, "
                    f"resource_matches={invalid_resources}",
                )

            identities = azure_cli.try_run(
                ["identity", "list", "--resource-group", report.resource_group],
                default=[],
            ) or []
            for identity in identities:
                live_resources[_relative_live_scope(str(identity.get("id", "")), report.resource_group)] = identity

            declared_by_name = {
                str(entry.get("name")): str(entry.get("scope", "")).lower()
                for section, entry in _iter_scoped_entries(plan)
                if section in {"resources", "identities"}
            }
            referenced_principals = {
                str(assignment.get("principal", ""))
                for assignment in plan.get("role_assignments", []) or []
            }
            for principal_alias in referenced_principals:
                principal_pattern = declared_by_name.get(principal_alias, "")
                for scope, resource in list(live_resources.items()):
                    resource_id = str(resource.get("id", ""))
                    if (
                        principal_pattern
                        and _scope_matches(scope, principal_pattern)
                        and not _principal_id(resource)
                        and resource_id
                    ):
                        detailed = azure_cli.try_run(
                            ["resource", "show", "--ids", resource_id],
                            default={},
                        ) or {}
                        if isinstance(detailed, dict):
                            live_resources[scope] = detailed

            report.checks_run.append("live:declared-role-assignments")
            subscription_assignments = azure_cli.try_run(
                ["role", "assignment", "list", "--all", "--include-inherited"],
                default=[],
            ) or []
            _check_live_role_assignments(report, plan, live_resources, subscription_assignments)
    return report


def require_clean_live(
    path: Path,
    *,
    enabled_modules: Iterable[str] | None = None,
) -> BoundaryReport:
    """Load the boundary and require its exact live inventory to pass."""
    plan = load_plan(path)
    report = check_plan(plan)
    if not report.ok:
        raise PermissionError(
            f"the declared ownership boundary failed {len(report.violations)} static check(s)"
        )
    report = check_live(report, plan, enabled_modules=enabled_modules)
    if not report.ok:
        raise PermissionError(
            f"the live ownership boundary failed {len(report.violations)} check(s)"
        )
    return report


def ensure_resource_group(
    report: BoundaryReport, location: str, tags: dict[str, str], *, dry_run: bool = True
) -> str:
    """Create the one resource group this project owns.

    Refuses unless the boundary report is clean, which is what makes this the
    gate rather than a convenience wrapper.
    """
    if not report.ok:
        raise PermissionError(
            "refusing to create anything: the ownership boundary check failed with "
            f"{len(report.violations)} violation(s)"
        )
    if report.target_exists:
        return f"resource group {report.resource_group!r} already exists; nothing to do"
    if dry_run:
        return (
            f"DRY RUN — would create resource group {report.resource_group!r} in {location!r} "
            f"with tags {tags}"
        )

    tag_args = [f"{k}={v}" for k, v in tags.items()]
    azure_cli.run(
        ["group", "create", "-n", report.resource_group, "-l", location, "--tags", *tag_args],
        allow_write=True,
    )
    return f"created resource group {report.resource_group!r} in {location!r}"


def render_markdown(report: BoundaryReport) -> str:
    verdict = "PASS" if report.ok else "FAIL"
    lines = [
        "# Ownership boundary check",
        "",
        f"Generated: `{report.generated_at}`  ",
        f"Boundary: **`{report.resource_group}`** — the only resource group this project may mutate.  ",
        f"Result: **{verdict}**",
        "",
        "## Checks run",
        "",
    ]
    lines += [f"- `{c}`" for c in report.checks_run]

    lines += ["", "## Violations", ""]
    if report.violations:
        lines += [f"- {v}" for v in report.violations]
    else:
        lines.append("_None._")

    if report.live:
        lines += [
            "",
            "## Live subscription state",
            "",
            f"- Target resource group exists: **{report.target_exists}**",
            f"- Resource groups outside the boundary (read-only, never modified): "
            f"**{len(report.protected_resource_groups)}**",
        ]
    return "\n".join(lines) + "\n"


def to_json(report: BoundaryReport) -> str:
    return json.dumps(
        {
            "generated_at": report.generated_at,
            "resource_group": report.resource_group,
            "ok": report.ok,
            "live": report.live,
            "target_exists": report.target_exists,
            "checks_run": report.checks_run,
            "violations": [{"check": v.check, "subject": v.subject, "message": v.message} for v in report.violations],
            "protected_resource_groups": report.protected_resource_groups,
        },
        indent=2,
    )
