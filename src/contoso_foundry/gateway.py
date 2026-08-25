"""AI gateway configuration, policy rendering, and live verification."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from . import azure_cli

QUOTA_PERIODS = frozenset({"Hourly", "Daily", "Weekly", "Monthly", "Yearly"})


class GatewayConfigError(ValueError):
    """Raised when the gateway configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class ProjectLimits:
    tokens_per_minute: int
    total_token_quota: int
    quota_period: str


@dataclass(frozen=True)
class GatewayConfig:
    projects: dict[str, ProjectLimits]
    allowed_publishers: tuple[str, ...]
    allowed_asset_ids: tuple[str, ...]
    only_allow_direct_from_azure: bool
    deny_preview_models: bool


@dataclass(frozen=True)
class GatewayStatus:
    apim_state: str
    apim_sku: str
    apim_location: str
    managed_identity: bool
    resource_specific_logs: bool
    enrolled_projects: tuple[str, ...]
    token_policy_projects: tuple[str, ...]
    policy_assignments: tuple[str, ...]
    guardrail_policy_name: str
    guardrail_policy_mode: str
    guardrail_base_policy: str
    guardrail_filter_count: int

    @property
    def ok(self) -> bool:
        return (
            self.apim_state == "Succeeded"
            and self.apim_sku == "BasicV2"
            and self.apim_location.replace(" ", "").lower() == "northcentralus"
            and self.managed_identity
            and self.resource_specific_logs
            and self.enrolled_projects == self.token_policy_projects
            and len(self.policy_assignments) == 2
            and self.guardrail_policy_name == "contoso-agents-guardrails"
            and self.guardrail_policy_mode == "Blocking"
            and self.guardrail_base_policy == "Microsoft.DefaultV2"
            and self.guardrail_filter_count == 10
        )


def load_config(path: Path) -> GatewayConfig:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if document.get("version") != 1:
        raise GatewayConfigError("gateway config version must be 1")

    raw_projects = document.get("projects")
    if not isinstance(raw_projects, dict) or not raw_projects:
        raise GatewayConfigError("gateway config must define at least one project")

    projects: dict[str, ProjectLimits] = {}
    for name, raw in raw_projects.items():
        if not isinstance(name, str) or not name or not isinstance(raw, dict):
            raise GatewayConfigError("each project limit must be a named mapping")
        tpm = raw.get("tokens_per_minute")
        quota = raw.get("total_token_quota")
        period = raw.get("quota_period")
        if not isinstance(tpm, int) or tpm <= 0:
            raise GatewayConfigError(f"{name}: tokens_per_minute must be a positive integer")
        if not isinstance(quota, int) or quota <= 0:
            raise GatewayConfigError(f"{name}: total_token_quota must be a positive integer")
        if period not in QUOTA_PERIODS:
            raise GatewayConfigError(f"{name}: quota_period must be one of {sorted(QUOTA_PERIODS)}")
        projects[name] = ProjectLimits(tpm, quota, period)

    governance = document.get("model_governance")
    if not isinstance(governance, dict):
        raise GatewayConfigError("gateway config must define model_governance")
    publishers = governance.get("allowed_publishers")
    assets = governance.get("allowed_asset_ids")
    if not isinstance(publishers, list) or not all(isinstance(item, str) for item in publishers):
        raise GatewayConfigError("allowed_publishers must be a list of strings")
    if not isinstance(assets, list) or not assets or not all(isinstance(item, str) for item in assets):
        raise GatewayConfigError("allowed_asset_ids must be a non-empty list of strings")
    if any(not asset.endswith("/") for asset in assets):
        raise GatewayConfigError("model-family asset IDs must end in '/' to avoid prefix collisions")

    return GatewayConfig(
        projects=projects,
        allowed_publishers=tuple(publishers),
        allowed_asset_ids=tuple(assets),
        only_allow_direct_from_azure=bool(governance.get("only_allow_direct_from_azure")),
        deny_preview_models=bool(governance.get("deny_preview_models")),
    )


def render_token_fragment(project: str, limits: ProjectLimits) -> str:
    return (
        "<fragment>"
        f'<llm-token-limit counter-key="project:{project}" '
        f'tokens-per-minute="{limits.tokens_per_minute}" '
        f'token-quota="{limits.total_token_quota}" '
        f'token-quota-period="{limits.quota_period}" '
        'estimate-prompt-tokens="true" '
        'remaining-tokens-header-name="x-contoso-remaining-tpm" '
        'remaining-quota-tokens-header-name="x-contoso-remaining-quota" />'
        "</fragment>"
    )


def expected_overrun_status(limit: str) -> int:
    statuses = {"tokens_per_minute": 429, "total_token_quota": 403}
    try:
        return statuses[limit]
    except KeyError as exc:
        raise GatewayConfigError(f"unknown token limit {limit!r}") from exc


def write_policy_fragments(config: GatewayConfig, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for project, limits in config.projects.items():
        path = output_dir / f"{project}-token-governance.xml"
        path.write_text(render_token_fragment(project, limits) + "\n", encoding="utf-8")
        written.append(path)
    return written


def collect_status(resource_group: str, resource_prefix: str, config: GatewayConfig) -> GatewayStatus:
    gateway_name = f"{resource_prefix}-gateway"
    foundry_name = f"{resource_prefix}-foundry"
    gateway = azure_cli.run(["apim", "show", "-g", resource_group, "-n", gateway_name])
    gateway_id = gateway["id"]

    diagnostics = azure_cli.run(["monitor", "diagnostic-settings", "list", "--resource", gateway_id]) or []
    if isinstance(diagnostics, dict):
        diagnostics = diagnostics.get("value", [])
    resource_specific_logs = any(
        item.get("logAnalyticsDestinationType") == "Dedicated"
        and any(log.get("enabled") and log.get("category") == "GatewayLogs" for log in item.get("logs", []))
        for item in diagnostics
    )

    subscription_id = azure_cli.run(["account", "show"])["id"]
    account_url = (
        f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{foundry_name}"
    )
    connections = azure_cli.run(
        ["rest", "--method", "get", "--url", f"{account_url}/connections?api-version=2026-05-01"]
    ).get("value", [])
    enrolled = tuple(
        sorted(
            connection["name"].removeprefix("ai-gateway-")
            for connection in connections
            if connection.get("properties", {}).get("category") == "ApiManagement"
            and connection["name"].startswith("ai-gateway-")
        )
    )

    fragments = azure_cli.run(
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"{gateway_id}/policyFragments?api-version=2024-05-01",
        ]
    ).get("value", [])
    fragment_projects = tuple(
        sorted(
            fragment["name"].removesuffix("-token-governance")
            for fragment in fragments
            if fragment["name"].endswith("-token-governance")
        )
    )

    assignments = azure_cli.run(
        [
            "policy",
            "assignment",
            "list",
            "--scope",
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}",
        ]
    )
    assignment_names = tuple(
        sorted(
            assignment["name"]
            for assignment in assignments
            if assignment["name"]
            in {
                f"{resource_prefix}-approved-models",
                f"{resource_prefix}-model-eligibility",
            }
        )
    )
    guardrail = azure_cli.run(
        [
            "rest",
            "--method",
            "get",
            "--url",
            (
                f"{account_url}/raiPolicies/{resource_prefix}-guardrails"
                "?api-version=2026-05-01"
            ),
        ]
    )
    guardrail_properties = guardrail.get("properties", {})

    expected = tuple(sorted(config.projects))
    if enrolled != expected:
        raise GatewayConfigError(f"expected enrolled projects {expected}, found {enrolled}")
    if fragment_projects != expected:
        raise GatewayConfigError(f"expected token policies {expected}, found {fragment_projects}")

    return GatewayStatus(
        apim_state=gateway.get("provisioningState", ""),
        apim_sku=gateway.get("sku", {}).get("name", ""),
        apim_location=gateway.get("location", ""),
        managed_identity=gateway.get("identity", {}).get("type") == "SystemAssigned",
        resource_specific_logs=resource_specific_logs,
        enrolled_projects=enrolled,
        token_policy_projects=fragment_projects,
        policy_assignments=assignment_names,
        guardrail_policy_name=guardrail.get("name", ""),
        guardrail_policy_mode=guardrail_properties.get("mode", ""),
        guardrail_base_policy=guardrail_properties.get("basePolicyName", ""),
        guardrail_filter_count=len(guardrail_properties.get("contentFilters", [])),
    )


def status_to_json(status: GatewayStatus) -> str:
    return json.dumps({"ok": status.ok, **asdict(status)}, indent=2)
