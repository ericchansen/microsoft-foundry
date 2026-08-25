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
from fnmatch import fnmatchcase
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
        if scope.lower().startswith("subscriptions/"):
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
        report.checks_run.append(f"live:skipped ({exc})")
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

        resources = azure_cli.try_run(["resource", "list", "-g", report.resource_group], default=[]) or []
        if not plan.get("allow_existing_resource_group", False):
            report.fail(
                "live:target-not-adopted",
                report.resource_group,
                "already exists and the plan does not permit a previously verified project-owned group",
            )
        else:
            declared_scopes = [
                str(entry.get("scope", "")).lower()
                for section, entry in _iter_scoped_entries(plan)
                if section in {"resources", "identities"}
            ]
            marker = f"/resourcegroups/{report.resource_group.lower()}/"
            unexpected: list[str] = []
            for resource in resources:
                resource_id = str(resource.get("id", "")).lower()
                if marker not in resource_id:
                    unexpected.append(resource_id or str(resource.get("name", "<unnamed>")))
                    continue
                relative_scope = resource_id.split(marker, maxsplit=1)[1]
                if not any(fnmatchcase(relative_scope, pattern) for pattern in declared_scopes):
                    unexpected.append(relative_scope)
            if unexpected:
                report.fail(
                    "live:declared-resource-inventory",
                    report.resource_group,
                    f"contains resources not declared by the ownership plan: {sorted(unexpected)}",
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
