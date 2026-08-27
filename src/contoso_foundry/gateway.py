"""AI gateway configuration, policy rendering, and live verification."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import yaml

from . import azure_cli

QUOTA_PERIODS = frozenset({"Hourly", "Daily", "Weekly", "Monthly", "Yearly"})
APPROVED_MODELS_POLICY = "/providers/Microsoft.Authorization/policyDefinitions/aafe3651-cb78-4f68-9f81-e7e41509110f"
MODEL_ELIGIBILITY_POLICY = "/providers/Microsoft.Authorization/policyDefinitions/8791d062-ba96-4c34-b604-8538f7e30ca0"
CONNECTION_AUTH_CONFIG = {
    "type": "api_key",
    "name": "Ocp-Apim-Subscription-Key",
    "format": "{api_key}",
}
MODEL_DEPLOYMENT_FIELDS = frozenset(
    {"deployment_name", "model_name", "version", "format", "rai_policy_name"}
)
_SEVERITY_FILTERS = frozenset(
    (name, source) for name in ("Hate", "Sexual", "Violence", "Selfharm") for source in ("Prompt", "Completion")
)
_BINARY_FILTERS = frozenset(
    {
        ("Jailbreak", "Prompt"),
        ("Protected Material Text", "Completion"),
    }
)


class GatewayConfigError(ValueError):
    """Raised when gateway configuration or live controls are incomplete."""


@dataclass(frozen=True)
class ProjectLimits:
    tokens_per_minute: int
    total_token_quota: int
    quota_period: str


@dataclass(frozen=True)
class ModelDeployment:
    deployment_name: str
    model_name: str
    version: str
    format: str
    rai_policy_name: str


@dataclass(frozen=True)
class GatewayConfig:
    default_project: ProjectLimits
    projects: dict[str, ProjectLimits]
    model_deployments: tuple[ModelDeployment, ...]
    allowed_publishers: tuple[str, ...]
    allowed_asset_ids: tuple[str, ...]
    only_allow_direct_from_azure: bool
    deny_preview_models: bool


@dataclass(frozen=True)
class GatewayStatus:
    apim_state: str
    apim_sku: str
    apim_location: str
    expected_location: str
    managed_identity: bool
    resource_specific_logs: bool
    diagnostic_workspace_name: str
    shared_default_connection: bool
    enrolled_projects: tuple[str, ...]
    verified_connections: tuple[str, ...]
    verified_model_deployments: tuple[str, ...]
    token_policy_projects: tuple[str, ...]
    api_policy_routes: tuple[str, ...]
    policy_assignments: tuple[str, ...]
    guardrail_policy_name: str
    guardrail_policy_mode: str
    guardrail_base_policy: str
    guardrail_filters_valid: bool

    @property
    def ok(self) -> bool:
        return (
            self.apim_state == "Succeeded"
            and self.apim_sku == "BasicV2"
            and _normalise_location(self.apim_location) == _normalise_location(self.expected_location)
            and self.managed_identity
            and self.resource_specific_logs
            and self.shared_default_connection
            and self.enrolled_projects == self.token_policy_projects
            and self.api_policy_routes == tuple(sorted(("default", *self.enrolled_projects)))
            and len(self.verified_connections) == len(self.enrolled_projects) + 1
            and bool(self.verified_model_deployments)
            and len(self.enrolled_projects) == 4
            and len(self.policy_assignments) == 2
            and self.guardrail_policy_name == "contoso-agents-guardrails"
            and self.guardrail_policy_mode == "Blocking"
            and self.guardrail_base_policy == "Microsoft.DefaultV2"
            and self.guardrail_filters_valid
        )


def load_config(path: Path) -> GatewayConfig:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if document.get("version") != 1:
        raise GatewayConfigError("gateway config version must be 1")

    default_project = _load_limits("default_project", document.get("default_project"))

    raw_projects = document.get("projects")
    if not isinstance(raw_projects, dict) or not raw_projects:
        raise GatewayConfigError("gateway config must define at least one project")
    projects: dict[str, ProjectLimits] = {}
    for name, raw in raw_projects.items():
        if not isinstance(name, str) or not name:
            raise GatewayConfigError("each project limit must be a named mapping")
        projects[name] = _load_limits(name, raw)

    raw_deployments = document.get("expected_model_deployments")
    if not isinstance(raw_deployments, list):
        raise GatewayConfigError("expected_model_deployments must be a list")
    deployments = tuple(_load_model_deployment(index, raw) for index, raw in enumerate(raw_deployments))
    names = [deployment.deployment_name for deployment in deployments]
    if len(names) != len(set(names)):
        raise GatewayConfigError("expected_model_deployments must have unique deployment_name values")

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
    for deployment in deployments:
        if not any(f"/{deployment.model_name}/" in asset for asset in assets):
            raise GatewayConfigError(f"{deployment.deployment_name}: model family is not in allowed_asset_ids")
    only_allow_direct_from_azure = governance.get("only_allow_direct_from_azure")
    deny_preview_models = governance.get("deny_preview_models")
    if not isinstance(only_allow_direct_from_azure, bool):
        raise GatewayConfigError("only_allow_direct_from_azure must be a boolean")
    if not isinstance(deny_preview_models, bool):
        raise GatewayConfigError("deny_preview_models must be a boolean")

    return GatewayConfig(
        default_project=default_project,
        projects=projects,
        model_deployments=deployments,
        allowed_publishers=tuple(publishers),
        allowed_asset_ids=tuple(assets),
        only_allow_direct_from_azure=only_allow_direct_from_azure,
        deny_preview_models=deny_preview_models,
    )


def _load_limits(name: str, raw: object) -> ProjectLimits:
    if not isinstance(raw, dict):
        raise GatewayConfigError(f"{name}: token limits must be a mapping")
    tpm = raw.get("tokens_per_minute")
    quota = raw.get("total_token_quota")
    period = raw.get("quota_period")
    if isinstance(tpm, bool) or not isinstance(tpm, int) or tpm <= 0:
        raise GatewayConfigError(f"{name}: tokens_per_minute must be a positive integer")
    if isinstance(quota, bool) or not isinstance(quota, int) or quota <= 0:
        raise GatewayConfigError(f"{name}: total_token_quota must be a positive integer")
    if period not in QUOTA_PERIODS:
        raise GatewayConfigError(f"{name}: quota_period must be one of {sorted(QUOTA_PERIODS)}")
    return ProjectLimits(tpm, quota, period)


def _load_model_deployment(index: int, raw: object) -> ModelDeployment:
    context = f"expected_model_deployments[{index}]"
    if not isinstance(raw, dict):
        raise GatewayConfigError(f"{context} must be a mapping")
    if set(raw) != MODEL_DEPLOYMENT_FIELDS:
        raise GatewayConfigError(
            f"{context} fields must be exactly {sorted(MODEL_DEPLOYMENT_FIELDS)}"
        )

    def required(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise GatewayConfigError(f"{context}.{key} must be a non-empty string")
        return value

    return ModelDeployment(
        deployment_name=required("deployment_name"),
        model_name=required("model_name"),
        version=required("version"),
        format=required("format"),
        rai_policy_name=required("rai_policy_name"),
    )


def parse_model_deployment_catalog(raw: str) -> tuple[ModelDeployment, ...]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GatewayConfigError("existing model deployment input is malformed JSON") from exc
    if not isinstance(document, list):
        raise GatewayConfigError("existing model deployment input must be a JSON array")
    deployments = tuple(
        _load_model_deployment(index, item) for index, item in enumerate(document)
    )
    names = [deployment.deployment_name for deployment in deployments]
    if len(names) != len(set(names)):
        raise GatewayConfigError("existing model deployment names must be unique")
    return deployments


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


def expected_connection_models(config: GatewayConfig) -> list[dict[str, Any]]:
    return [
        {
            "name": deployment.deployment_name,
            "properties": {
                "model": {
                    "name": deployment.model_name,
                    "version": deployment.version,
                    "format": deployment.format,
                }
            },
        }
        for deployment in config.model_deployments
    ]


def _json_metadata(value: object, field: str, expected_type: type) -> Any:
    if not isinstance(value, str):
        raise GatewayConfigError(f"connection metadata {field} must be serialized JSON")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GatewayConfigError(f"connection metadata {field} is malformed JSON") from exc
    if not isinstance(parsed, expected_type):
        raise GatewayConfigError(f"connection metadata {field} must decode to {expected_type.__name__}")
    return parsed


def _verify_diagnostics(
    diagnostics: list[dict[str, Any]],
    *,
    subscription_id: str,
    resource_group: str,
    resource_prefix: str,
) -> None:
    expected_name = f"{resource_prefix}-gateway-logs"
    if {item.get("name") for item in diagnostics} != {expected_name}:
        raise GatewayConfigError(f"APIM must have exactly the {expected_name!r} diagnostic setting")
    setting = diagnostics[0]
    expected_workspace = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/"
        f"Microsoft.OperationalInsights/workspaces/{resource_prefix}-logs"
    )
    if str(setting.get("workspaceId", "")).lower() != expected_workspace.lower():
        raise GatewayConfigError("APIM diagnostic setting targets the wrong Log Analytics workspace")
    if setting.get("logAnalyticsDestinationType") != "Dedicated":
        raise GatewayConfigError("APIM diagnostic setting must use resource-specific tables")
    enabled_logs = {item.get("category") for item in setting.get("logs", []) if item.get("enabled") is True}
    if enabled_logs != {"GatewayLogs"}:
        raise GatewayConfigError("APIM diagnostics must enable only GatewayLogs")
    enabled_metrics = {item.get("category") for item in setting.get("metrics", []) if item.get("enabled") is True}
    if enabled_metrics != {"AllMetrics"}:
        raise GatewayConfigError("APIM diagnostics must enable AllMetrics")


def _verify_token_fragments(fragments: list[dict[str, Any]], config: GatewayConfig) -> tuple[str, ...]:
    expected_limits = {"default": config.default_project, **config.projects}
    expected_names = {f"{name}-token-governance" for name in expected_limits}
    by_name = {str(fragment.get("name")): fragment for fragment in fragments}
    if set(by_name) != expected_names:
        raise GatewayConfigError(
            f"token fragment inventory mismatch: expected {sorted(expected_names)}, found {sorted(by_name)}"
        )
    for project, limits in expected_limits.items():
        fragment = by_name[f"{project}-token-governance"]
        value = fragment.get("properties", {}).get("value")
        if not isinstance(value, str):
            raise GatewayConfigError(f"{project}: token fragment XML is missing")
        try:
            root = ElementTree.fromstring(value)
        except ElementTree.ParseError as exc:
            raise GatewayConfigError(f"{project}: token fragment XML is malformed") from exc
        policies = root.findall("llm-token-limit")
        if len(policies) != 1:
            raise GatewayConfigError(f"{project}: token fragment must contain one llm-token-limit")
        expected_attributes = {
            "counter-key": f"project:{project}",
            "tokens-per-minute": str(limits.tokens_per_minute),
            "token-quota": str(limits.total_token_quota),
            "token-quota-period": limits.quota_period,
            "estimate-prompt-tokens": "true",
            "remaining-tokens-header-name": "x-contoso-remaining-tpm",
            "remaining-quota-tokens-header-name": "x-contoso-remaining-quota",
        }
        if policies[0].attrib != expected_attributes:
            raise GatewayConfigError(f"{project}: token fragment attributes do not match config")
    return tuple(sorted(config.projects))


def _verify_api_policy(policy: dict[str, Any], *, route: str) -> None:
    value = policy.get("properties", {}).get("value")
    if not isinstance(value, str):
        raise GatewayConfigError(f"{route}: API policy XML is missing")
    try:
        root = ElementTree.fromstring(value)
    except ElementTree.ParseError as exc:
        raise GatewayConfigError(f"{route}: API policy XML is malformed") from exc
    inbound = root.find("inbound")
    backend = root.find("backend")
    if inbound is None or backend is None:
        raise GatewayConfigError(f"{route}: API policy must define inbound and backend sections")

    includes = inbound.findall("include-fragment")
    expected_fragment = f"{route}-token-governance"
    if len(includes) != 1 or includes[0].attrib != {"fragment-id": expected_fragment}:
        raise GatewayConfigError(f"{route}: API policy includes the wrong token fragment")

    backends = inbound.findall("set-backend-service")
    if len(backends) != 1 or backends[0].attrib.get("backend-id") != "foundry-models":
        raise GatewayConfigError(f"{route}: API policy must use the foundry-models backend")

    identities = inbound.findall("authentication-managed-identity")
    expected_audience = "https://cognitiveservices.azure.com"
    if len(identities) != 1 or identities[0].attrib.get("resource") != expected_audience:
        raise GatewayConfigError(f"{route}: API policy managed-identity audience is incorrect")

    if len(backend.findall("forward-request")) != 1:
        raise GatewayConfigError(f"{route}: API policy must forward exactly one backend request")


def _verify_api_policies(
    policies: dict[str, dict[str, Any]], config: GatewayConfig
) -> tuple[str, ...]:
    expected_routes = {"default", *config.projects}
    if set(policies) != expected_routes:
        raise GatewayConfigError(
            f"API policy inventory mismatch: expected {sorted(expected_routes)}, "
            f"found {sorted(policies)}"
        )
    for route, policy in policies.items():
        _verify_api_policy(policy, route=route)
    return tuple(sorted(expected_routes))


def _verify_api_inventory(
    apis: list[dict[str, Any]], config: GatewayConfig
) -> tuple[str, ...]:
    governed: dict[str, dict[str, Any]] = {}
    unexpected: list[str] = []
    for api in apis:
        name = str(api.get("name", ""))
        if name.startswith("foundry-"):
            governed[name.removeprefix("foundry-")] = api
        else:
            unexpected.append(name or "<unnamed>")

    expected_routes = {"default", *config.projects}
    if unexpected or set(governed) != expected_routes:
        raise GatewayConfigError(
            "APIM API inventory mismatch: "
            f"expected governed routes {sorted(expected_routes)}, "
            f"found {sorted(governed)}, unexpected {sorted(unexpected)}"
        )
    for route, api in governed.items():
        if api.get("path") != f"models/{route}":
            raise GatewayConfigError(f"{route}: APIM API path is outside the governed route")
        if api.get("subscriptionRequired") is not True:
            raise GatewayConfigError(f"{route}: APIM API must require a subscription")
    return tuple(sorted(expected_routes))


def _verify_model_catalog(
    deployments: list[dict[str, Any]],
    expected_deployments: tuple[ModelDeployment, ...],
) -> tuple[str, ...]:
    expected = {
        deployment.deployment_name: deployment for deployment in expected_deployments
    }
    by_name = {str(deployment.get("name")): deployment for deployment in deployments}
    if set(by_name) != set(expected):
        raise GatewayConfigError(
            f"model deployment inventory mismatch: expected {sorted(expected)}, found {sorted(by_name)}"
        )
    for name, declared in expected.items():
        properties = by_name[name].get("properties", {})
        model = properties.get("model", {})
        actual = (
            model.get("name"),
            model.get("version"),
            model.get("format"),
            properties.get("raiPolicyName"),
            properties.get("provisioningState"),
        )
        required = (
            declared.model_name,
            declared.version,
            declared.format,
            declared.rai_policy_name,
            "Succeeded",
        )
        if actual != required:
            raise GatewayConfigError(f"{name}: live model deployment contract does not match config")
    return tuple(sorted(expected))


def _verify_model_deployments(
    deployments: list[dict[str, Any]], config: GatewayConfig
) -> tuple[str, ...]:
    return _verify_model_catalog(deployments, config.model_deployments)


def attest_model_deployment_input(
    resource_group: str,
    resource_prefix: str,
    config: GatewayConfig,
    raw_catalog: str,
) -> tuple[str, ...]:
    supplied = parse_model_deployment_catalog(raw_catalog)
    account_name = f"{resource_prefix}-foundry"
    accounts = azure_cli.run(
        [
            "cognitiveservices",
            "account",
            "list",
            "--resource-group",
            resource_group,
        ]
    )
    account_exists = any(account.get("name") == account_name for account in accounts)
    if not account_exists:
        if supplied:
            raise GatewayConfigError(
                f"existing model deployments were supplied but account {account_name!r} is absent"
            )
        return ()
    if set(supplied) != set(config.model_deployments):
        raise GatewayConfigError(
            "existing model deployment input does not match expected_model_deployments"
        )

    deployments = azure_cli.run(
        [
            "cognitiveservices",
            "account",
            "deployment",
            "list",
            "--resource-group",
            resource_group,
            "--name",
            account_name,
        ]
    )
    return _verify_model_catalog(deployments, supplied)


def _verify_connections(
    connections: list[dict[str, Any]],
    *,
    config: GatewayConfig,
    gateway_url: str,
) -> tuple[str, ...]:
    expected_projects = tuple(sorted(config.projects))
    expected_names = {f"ai-gateway-{project}" for project in expected_projects}
    expected_names.add("ai-gateway-default")
    apim_connections = {
        str(connection.get("name")): connection
        for connection in connections
        if connection.get("properties", {}).get("category") == "ApiManagement"
    }
    if set(apim_connections) != expected_names:
        raise GatewayConfigError(
            f"gateway connection inventory mismatch: expected {sorted(expected_names)}, "
            f"found {sorted(apim_connections)}"
        )
    models = expected_connection_models(config)
    for name, connection in apim_connections.items():
        properties = connection.get("properties", {})
        route = name.removeprefix("ai-gateway-")
        expected_target = f"{gateway_url.rstrip('/')}/models/{route}"
        if properties.get("authType") != "ApiKey":
            raise GatewayConfigError(f"{name}: authType must be ApiKey")
        if properties.get("target") != expected_target:
            raise GatewayConfigError(f"{name}: target does not match the governed APIM route")
        expected_shared = name == "ai-gateway-default"
        if properties.get("isSharedToAll") is not expected_shared:
            raise GatewayConfigError(f"{name}: isSharedToAll does not match its connection scope")
        metadata = properties.get("metadata", {})
        if metadata.get("deploymentInPath") != "false":
            raise GatewayConfigError(f"{name}: deploymentInPath must be false")
        if _json_metadata(metadata.get("models"), "models", list) != models:
            raise GatewayConfigError(f"{name}: static model metadata does not match live deployments")
        if _json_metadata(metadata.get("authConfig"), "authConfig", dict) != CONNECTION_AUTH_CONFIG:
            raise GatewayConfigError(f"{name}: API-key authentication metadata is incorrect")
        unexpected = set(metadata) - {"deploymentInPath", "models", "authConfig"}
        if unexpected:
            raise GatewayConfigError(f"{name}: unexpected connection metadata {sorted(unexpected)}")
    return tuple(sorted(expected_names))


def _assignment_properties(assignment: dict[str, Any]) -> dict[str, Any]:
    properties = assignment.get("properties")
    return properties if isinstance(properties, dict) else assignment


def _parameter_value(parameters: dict[str, Any], name: str) -> Any:
    parameter = parameters.get(name)
    if not isinstance(parameter, dict) or "value" not in parameter:
        raise GatewayConfigError(f"policy assignment parameter {name!r} is missing")
    return parameter["value"]


def _verify_policy_assignments(
    assignments: list[dict[str, Any]], *, config: GatewayConfig, resource_prefix: str
) -> tuple[str, ...]:
    expected = {
        f"{resource_prefix}-approved-models": (
            APPROVED_MODELS_POLICY,
            {
                "effect": "Deny",
                "allowedPublishers": list(config.allowed_publishers),
                "allowedAssetIds": list(config.allowed_asset_ids),
            },
        ),
        f"{resource_prefix}-model-eligibility": (
            MODEL_ELIGIBILITY_POLICY,
            {
                "effect": "Deny",
                "onlyAllowDirectFromAzure": config.only_allow_direct_from_azure,
                "denyPreviewModels": config.deny_preview_models,
            },
        ),
    }
    by_name = {
        str(assignment.get("name")): assignment
        for assignment in assignments
        if str(assignment.get("name", "")).startswith(f"{resource_prefix}-")
    }
    if set(by_name) != set(expected):
        raise GatewayConfigError(
            f"policy assignment inventory mismatch: expected {sorted(expected)}, found {sorted(by_name)}"
        )
    for name, (definition, expected_parameters) in expected.items():
        properties = _assignment_properties(by_name[name])
        if str(properties.get("policyDefinitionId", "")).lower() != definition.lower():
            raise GatewayConfigError(f"{name}: policy definition is incorrect")
        if properties.get("enforcementMode") != "Default":
            raise GatewayConfigError(f"{name}: enforcementMode must be Default")
        parameters = properties.get("parameters", {})
        if set(parameters) != set(expected_parameters):
            raise GatewayConfigError(f"{name}: policy parameter inventory is incorrect")
        for parameter, value in expected_parameters.items():
            if _parameter_value(parameters, parameter) != value:
                raise GatewayConfigError(f"{name}: policy parameter {parameter!r} is incorrect")
    return tuple(sorted(expected))


def _verify_guardrail_policy(guardrail: dict[str, Any], *, expected_name: str) -> None:
    if guardrail.get("name") != expected_name:
        raise GatewayConfigError("responsible AI policy name is incorrect")
    properties = guardrail.get("properties", {})
    if properties.get("mode") != "Blocking":
        raise GatewayConfigError("responsible AI policy mode must be Blocking")
    if properties.get("basePolicyName") != "Microsoft.DefaultV2":
        raise GatewayConfigError("responsible AI base policy must be Microsoft.DefaultV2")
    filters = properties.get("contentFilters")
    if not isinstance(filters, list):
        raise GatewayConfigError("responsible AI content filters are missing")
    by_key = {(item.get("name"), item.get("source")): item for item in filters}
    expected_keys = _SEVERITY_FILTERS | _BINARY_FILTERS
    if set(by_key) != expected_keys or len(filters) != len(expected_keys):
        raise GatewayConfigError("responsible AI filter inventory is incorrect")
    for key, item in by_key.items():
        if item.get("enabled") is not True or item.get("blocking") is not True:
            raise GatewayConfigError(f"responsible AI filter {key} must be enabled and blocking")
        if key in _SEVERITY_FILTERS and item.get("severityThreshold") != "Medium":
            raise GatewayConfigError(f"responsible AI filter {key} must use the Medium threshold")


def _normalise_location(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def collect_status(
    resource_group: str,
    resource_prefix: str,
    config: GatewayConfig,
    *,
    expected_location: str,
) -> GatewayStatus:
    """Read the live control plane and fail on any governance-contract drift."""
    gateway_name = f"{resource_prefix}-gateway"
    foundry_name = f"{resource_prefix}-foundry"
    gateway = azure_cli.run(["apim", "show", "-g", resource_group, "-n", gateway_name])
    gateway_id = gateway["id"]
    subscription_id = azure_cli.run(["account", "show"])["id"]

    diagnostics = azure_cli.run(["monitor", "diagnostic-settings", "list", "--resource", gateway_id]) or []
    if isinstance(diagnostics, dict):
        diagnostics = diagnostics.get("value", [])
    _verify_diagnostics(
        diagnostics,
        subscription_id=subscription_id,
        resource_group=resource_group,
        resource_prefix=resource_prefix,
    )

    account_url = (
        f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{foundry_name}"
    )
    account_connections = azure_cli.run(
        ["rest", "--method", "get", "--url", f"{account_url}/connections?api-version=2026-05-01"]
    ).get("value", [])
    connections = list(account_connections)
    for project in sorted(config.projects):
        project_url = f"{account_url}/projects/{project}/connections?api-version=2026-05-01"
        project_connections = azure_cli.run(
            ["rest", "--method", "get", "--url", project_url]
        ).get("value", [])
        connections.extend(project_connections)
    verified_connections = _verify_connections(
        connections,
        config=config,
        gateway_url=str(gateway.get("gatewayUrl", "")),
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
    token_policy_projects = _verify_token_fragments(fragments, config)

    live_apis = azure_cli.run(
        [
            "apim",
            "api",
            "list",
            "--resource-group",
            resource_group,
            "--service-name",
            gateway_name,
        ]
    )
    api_inventory_routes = _verify_api_inventory(live_apis, config)

    api_policies = {}
    for route in api_inventory_routes:
        policy_url = (
            f"{gateway_id}/apis/foundry-{route}/policies/policy"
            "?api-version=2024-05-01"
        )
        api_policies[route] = azure_cli.run(
            [
                "rest",
                "--method",
                "get",
                "--url",
                policy_url,
                "--headers",
                "Accept=application/json",
            ]
        )
    api_policy_routes = _verify_api_policies(api_policies, config)

    live_deployments = azure_cli.run(
        [
            "cognitiveservices",
            "account",
            "deployment",
            "list",
            "--resource-group",
            resource_group,
            "--name",
            foundry_name,
        ]
    )
    verified_model_deployments = _verify_model_deployments(live_deployments, config)

    assignments = azure_cli.run(
        [
            "policy",
            "assignment",
            "list",
            "--scope",
            f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}",
        ]
    )
    assignment_names = _verify_policy_assignments(
        assignments,
        config=config,
        resource_prefix=resource_prefix,
    )

    guardrail_name = f"{resource_prefix}-guardrails"
    guardrail = azure_cli.run(
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"{account_url}/raiPolicies/{guardrail_name}?api-version=2026-05-01",
        ]
    )
    _verify_guardrail_policy(guardrail, expected_name=guardrail_name)
    guardrail_properties = guardrail.get("properties", {})
    enrolled = tuple(sorted(config.projects))

    return GatewayStatus(
        apim_state=gateway.get("provisioningState", ""),
        apim_sku=gateway.get("sku", {}).get("name", ""),
        apim_location=gateway.get("location", ""),
        expected_location=expected_location,
        managed_identity=gateway.get("identity", {}).get("type") == "SystemAssigned",
        resource_specific_logs=True,
        diagnostic_workspace_name=f"{resource_prefix}-logs",
        shared_default_connection=True,
        enrolled_projects=enrolled,
        verified_connections=verified_connections,
        verified_model_deployments=verified_model_deployments,
        token_policy_projects=token_policy_projects,
        api_policy_routes=api_policy_routes,
        policy_assignments=assignment_names,
        guardrail_policy_name=guardrail.get("name", ""),
        guardrail_policy_mode=guardrail_properties.get("mode", ""),
        guardrail_base_policy=guardrail_properties.get("basePolicyName", ""),
        guardrail_filters_valid=True,
    )


def status_to_json(status: GatewayStatus) -> str:
    return json.dumps({"ok": status.ok, **asdict(status)}, indent=2)
