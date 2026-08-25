"""Machine-checkable platform coverage for Foundry Control Plane."""

from __future__ import annotations

import json
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


def _find_agent_actions(resource: dict[str, Any]) -> list[dict[str, Any]]:
    definition = (resource.get("properties") or {}).get("definition") or {}
    actions = definition.get("actions") or {}
    return [action for action in actions.values() if isinstance(action, dict) and action.get("type") == "Agent"]


def _check_live_platform(
    entry: dict[str, Any],
    result: PlatformResult,
    resource: dict[str, Any],
    ownership_tags: dict[str, str],
    resource_group: str,
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
        properties = resource.get("properties") or {}
        app_insights = (properties.get("logConfiguration") or {}).get("applicationInsightsConfiguration") or {}
        if not app_insights.get("appId") or not app_insights.get("connectionString"):
            result.errors.append("SRE Agent is not connected to Application Insights")
        else:
            result.checks.append("live:application-insights")

        managed = (properties.get("knowledgeGraphConfiguration") or {}).get("managedResources") or []
        expected_suffix = f"/resourceGroups/{resource_group}".lower()
        if not any(str(item).lower().endswith(expected_suffix) for item in managed):
            result.errors.append("SRE Agent does not manage the project-owned resource group")
        else:
            result.checks.append("live:owned-scope-only")

        action = properties.get("actionConfiguration") or {}
        if action.get("accessLevel") != "Low" or action.get("mode") != "Review":
            result.errors.append("SRE Agent must use Low access and Review mode")
        else:
            result.checks.append("live:review-only")

    if entry["resource_type"].lower() == "microsoft.logic/workflows":
        if resource.get("kind") != entry.get("expected_kind"):
            result.errors.append(
                f"Logic Apps kind is {resource.get('kind')!r}; expected {entry.get('expected_kind')!r}"
            )
        else:
            result.checks.append("live:agentic-kind")

        agent_actions = _find_agent_actions(resource)
        if not agent_actions:
            result.errors.append("Logic Apps workflow contains no Agent action")
        else:
            result.checks.append("live:agent-loop")

        if agent_actions:
            if any(action.get("tools") is None for action in agent_actions):
                result.errors.append("Logic Apps Agent action must expose a bounded synthetic tool")
            else:
                result.checks.append("live:synthetic-tool")


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
        _check_live_platform(entry, result, resource, ownership_tags, report.resource_group)
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
