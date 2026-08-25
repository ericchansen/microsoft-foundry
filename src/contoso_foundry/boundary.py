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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
_IDENTITY_RESOURCE_TYPES = frozenset(
    {
        "microsoft.cognitiveservices/accounts",
        "microsoft.cognitiveservices/accounts/projects",
        "microsoft.managedidentity/userassignedidentities",
    }
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
    for section in ("resources", "identities", "role_assignments", "diagnostic_settings", "teardown"):
        for entry in plan.get(section, []) or []:
            entries.append((section, entry))
    return entries


def _scope_matches(scope: str, pattern: str) -> bool:
    """Match one ARM path segment at a time so wildcards cannot cross `/`."""
    expression = re.escape(pattern).replace(r"\*", "[^/]*")
    return re.fullmatch(expression, scope, flags=re.IGNORECASE) is not None


def _relative_scope(resource_id: str, resource_group_id: str) -> str | None:
    marker = f"{resource_group_id.rstrip('/')}/"
    if not resource_id.lower().startswith(marker.lower()):
        return None
    return resource_id[len(marker) :]


def _collection_values(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("value"), list):
        raise azure_cli.AzureCliError("Azure resource collection response did not contain a value array")
    return [item for item in document["value"] if isinstance(item, dict)]


def _enumerate_managed_children(
    plan: dict[str, Any],
    resources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Enumerate child types that `az resource list` omits."""
    children: list[dict[str, Any]] = []

    declared_projects = {
        str(entry.get("scope", "")).lower()
        for entry in plan.get("resources", []) or []
        if str(entry.get("kind", "")).lower() == "microsoft.cognitiveservices/accounts/projects"
    }
    for resource in resources:
        resource_id = str(resource.get("id", ""))
        resource_type = str(resource.get("type", "")).lower()
        if resource_type == "microsoft.cognitiveservices/accounts":
            projects = _collection_values(
                azure_cli.run(
                    [
                        "rest",
                        "--method",
                        "get",
                        "--url",
                        (
                            f"https://management.azure.com{resource_id}/projects"
                            "?api-version=2026-07-01"
                        ),
                    ]
                )
            )
            children.extend(projects)
            for project in projects:
                project_id = str(project.get("id", ""))
                children.extend(
                    _collection_values(
                        azure_cli.run(
                            [
                                "rest",
                                "--method",
                                "get",
                                "--url",
                                (
                                    f"https://management.azure.com{project_id}/connections"
                                    "?api-version=2026-05-01"
                                ),
                            ]
                        )
                    )
                )
        elif resource_type == "microsoft.cognitiveservices/accounts/projects":
            relative = next(
                (
                    scope
                    for scope in declared_projects
                    if resource_id.lower().endswith(f"/{scope}")
                ),
                None,
            )
            if relative:
                children.extend(
                    _collection_values(
                        azure_cli.run(
                            [
                                "rest",
                                "--method",
                                "get",
                                "--url",
                                (
                                    f"https://management.azure.com{resource_id}/connections"
                                    "?api-version=2026-05-01"
                                ),
                            ]
                        )
                    )
                )
        elif resource_type == "microsoft.managedidentity/userassignedidentities":
            children.extend(
                _collection_values(
                    azure_cli.run(
                        [
                            "rest",
                            "--method",
                            "get",
                            "--url",
                            (
                                f"https://management.azure.com{resource_id}/federatedIdentityCredentials"
                                "?api-version=2024-11-30"
                            ),
                        ]
                    )
                )
            )
        elif resource_type == "microsoft.storage/storageaccounts":
            children.extend(
                _collection_values(
                    azure_cli.run(
                        [
                            "rest",
                            "--method",
                            "get",
                            "--url",
                            (
                                f"https://management.azure.com{resource_id}/blobServices"
                                "?api-version=2025-08-01"
                            ),
                        ]
                    )
                )
            )
    return children


def _principal_id(resource: dict[str, Any]) -> str | None:
    identity = resource.get("identity", {}) or {}
    properties = resource.get("properties", {}) or {}
    value = identity.get("principalId") or properties.get("principalId")
    return str(value).lower() if value else None


def _check_live_role_assignments(
    report: BoundaryReport,
    plan: dict[str, Any],
    resource_group_id: str,
    resources_by_declaration: dict[str, dict[str, Any]],
) -> None:
    role_ids = {
        str(name): str(role_id).lower()
        for name, role_id in (plan.get("role_definitions", {}) or {}).items()
    }
    principals: dict[str, str] = {}
    referenced_principals = {
        str(assignment.get("principal", ""))
        for assignment in plan.get("role_assignments", []) or []
    }
    identity_resources = {
        name
        for name, resource in resources_by_declaration.items()
        if str(resource.get("type", "")).lower() in _IDENTITY_RESOURCE_TYPES
        or _principal_id(resource)
    }
    for name in referenced_principals | identity_resources:
        resource = resources_by_declaration.get(name)
        if not resource:
            continue
        if resolved := _principal_id(resource):
            principals[name] = resolved
            continue
        detail = azure_cli.run(["resource", "show", "--ids", str(resource["id"])])
        if isinstance(detail, dict) and (resolved := _principal_id(detail)):
            principals[name] = resolved
        else:
            report.fail(
                "live:principal-inventory",
                name,
                "could not resolve the principal ID for an owned identity-bearing resource",
            )

    expected: set[tuple[str, str, str]] = set()
    for assignment in plan.get("role_assignments", []) or []:
        principal = principals.get(str(assignment.get("principal", "")))
        role_id = role_ids.get(str(assignment.get("role", "")))
        scope = str(assignment.get("scope", ""))
        if not principal or not role_id:
            continue
        full_scope = resource_group_id if scope == "." else f"{resource_group_id}/{scope}"
        expected.add((principal, role_id, full_scope.lower()))

    live_assignments = azure_cli.run(["role", "assignment", "list", "--all"]) or []
    unexpected: list[str] = []
    owned_principals = set(principals.values())
    for assignment in live_assignments:
        if not isinstance(assignment, dict):
            continue
        scope = str(assignment.get("scope", "")).lower()
        principal = str(assignment.get("principalId", "")).lower()
        inside_boundary = (
            scope == resource_group_id.lower()
            or scope.startswith(f"{resource_group_id.lower()}/")
        )
        if not inside_boundary and principal not in owned_principals:
            continue
        actual = (
            principal,
            str(assignment.get("roleDefinitionId", "")).rsplit("/", maxsplit=1)[-1].lower(),
            scope,
        )
        if actual not in expected:
            unexpected.append(
                f"principal={actual[0] or '<missing>'}, role={actual[1] or '<missing>'}, scope={scope}"
            )
    if unexpected:
        report.fail(
            "live:declared-role-assignments",
            report.resource_group,
            f"contains undeclared direct role assignments: {sorted(unexpected)}",
        )


def check_plan(plan: dict[str, Any], *, expected_resource_group: str | None = None) -> BoundaryReport:
    """Static validation. Requires no credentials, so CI always runs it."""
    declared_rg = str(plan.get("resource_group", ""))
    report = BoundaryReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        resource_group=declared_rg,
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
        if re.search(r"subscriptions/", scope, re.I):
            report.fail("plan:relative-scopes", subject, f"scope {scope!r} names a subscription")

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


def check_live(report: BoundaryReport, plan: dict[str, Any]) -> BoundaryReport:
    """Confirm the live subscription matches the plan's assumptions.

    Read-only. Records the resource groups the project must never touch, so the
    protected set is captured as evidence rather than assumed.
    """
    try:
        groups = azure_cli.run(["group", "list"]) or []
    except azure_cli.AzureCliError as exc:
        report.checks_run.append("live:protected-resource-groups")
        report.fail(
            "live:protected-resource-groups",
            report.resource_group,
            f"could not enumerate resource groups: {exc}",
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
            top_level_resources = azure_cli.run(["resource", "list", "-g", report.resource_group]) or []
            all_resources = [*top_level_resources, *_enumerate_managed_children(plan, top_level_resources)]
            resources = list({
                str(resource.get("id", "")).lower(): resource
                for resource in all_resources
                if resource.get("id")
            }.values())
        except azure_cli.AzureCliError as exc:
            report.fail(
                "live:resource-inventory",
                report.resource_group,
                f"could not enumerate the complete resource inventory: {exc}",
            )
            return report
        if not plan.get("allow_existing_resource_group", False):
            report.fail(
                "live:target-not-adopted",
                report.resource_group,
                "already exists and the plan does not permit a previously verified project-owned group",
            )
        else:
            declarations = {
                str(entry.get("name")): str(entry.get("scope", ""))
                for section, entry in _iter_scoped_entries(plan)
                if section in {"resources", "identities"}
            }
            resource_group_id = str(target_group.get("id", ""))
            if not resource_group_id:
                report.fail(
                    "live:resource-inventory",
                    report.resource_group,
                    "Azure did not return the resource group ID required for inventory attestation",
                )
                return report
            unexpected: list[str] = []
            match_counts = {name: 0 for name in declarations}
            resources_by_declaration: dict[str, dict[str, Any]] = {}
            for resource in resources:
                resource_id = str(resource.get("id", ""))
                relative_scope = _relative_scope(resource_id, resource_group_id)
                if relative_scope is None:
                    unexpected.append(resource_id or str(resource.get("name", "<unnamed>")))
                    continue
                matches = [
                    name
                    for name, pattern in declarations.items()
                    if _scope_matches(relative_scope, pattern)
                ]
                if len(matches) != 1:
                    unexpected.append(relative_scope)
                    continue
                match_counts[matches[0]] += 1
                resources_by_declaration[matches[0]] = resource
            duplicates = sorted(name for name, count in match_counts.items() if count > 1)
            if unexpected:
                report.fail(
                    "live:declared-resource-inventory",
                    report.resource_group,
                    f"contains resources not declared by the ownership plan: {sorted(unexpected)}",
                )
            if duplicates:
                report.fail(
                    "live:declared-resource-cardinality",
                    report.resource_group,
                    f"contains multiple resources matching one declaration: {duplicates}",
                )
            try:
                _check_live_role_assignments(
                    report,
                    plan,
                    resource_group_id,
                    resources_by_declaration,
                )
            except azure_cli.AzureCliError as exc:
                report.fail(
                    "live:role-assignment-inventory",
                    report.resource_group,
                    f"could not enumerate role assignments: {exc}",
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