from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from contoso_foundry import gateway


def test_gateway_config_has_safe_project_defaults(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")

    assert set(config.projects) == {"travel", "support", "research", "platform"}
    assert config.default_project.tokens_per_minute == 2_000
    assert config.default_project.total_token_quota == 100_000
    assert config.projects["travel"].tokens_per_minute == 20_000
    assert config.projects["platform"].total_token_quota == 500_000
    assert all(limit.total_token_quota >= limit.tokens_per_minute for limit in config.projects.values())
    assert {deployment.deployment_name for deployment in config.model_deployments} == {
        "travel-gpt-5-4-mini",
        "contoso-field-model",
    }
    assert config.model_deployments[0].rai_policy_name == "contoso-agents-guardrails"


def test_model_allow_list_is_exact_and_blocks_preview(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")

    assert config.allowed_publishers == ()
    assert all(asset.endswith("/") for asset in config.allowed_asset_ids)
    for model in ("gpt-5.4-mini", "gpt-4.1-mini", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra"):
        assert any(f"/{model}/" in asset for asset in config.allowed_asset_ids)
    assert config.only_allow_direct_from_azure
    assert config.deny_preview_models


def test_token_fragment_is_valid_and_contains_both_limits():
    limits = gateway.ProjectLimits(100, 1_000, "Monthly")
    policy = gateway.render_token_fragment("travel", limits)
    node = ET.fromstring(policy).find("llm-token-limit")

    assert node is not None
    assert node.attrib["counter-key"] == "project:travel"
    assert node.attrib["tokens-per-minute"] == "100"
    assert node.attrib["token-quota"] == "1000"
    assert node.attrib["token-quota-period"] == "Monthly"


def test_limit_overrun_status_contract():
    assert gateway.expected_overrun_status("tokens_per_minute") == 429
    assert gateway.expected_overrun_status("total_token_quota") == 403
    with pytest.raises(gateway.GatewayConfigError):
        gateway.expected_overrun_status("requests")


def test_config_rejects_prefix_ambiguous_asset_id(tmp_path: Path):
    path = tmp_path / "gateway.yaml"
    path.write_text(
        """
version: 1
default_project:
  tokens_per_minute: 1
  total_token_quota: 2
  quota_period: Monthly
expected_model_deployments:
  - deployment_name: travel
    model_name: gpt-5
    version: "1"
    format: OpenAI
    rai_policy_name: Microsoft.DefaultV2
projects:
  travel:
    tokens_per_minute: 1
    total_token_quota: 2
    quota_period: Monthly
model_governance:
  allowed_publishers: []
  allowed_asset_ids:
    - azureml://registries/azure-openai/models/gpt-5
  only_allow_direct_from_azure: true
  deny_preview_models: true
""",
        encoding="utf-8",
    )

    with pytest.raises(gateway.GatewayConfigError, match="end in '/'"):
        gateway.load_config(path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("only_allow_direct_from_azure", '"false"'),
        ("deny_preview_models", "1"),
    ],
)
def test_config_rejects_non_boolean_governance_values(
    repo_root: Path,
    tmp_path: Path,
    key: str,
    value: str,
):
    config_text = (repo_root / "config" / "gateway.yaml").read_text(encoding="utf-8")
    path = tmp_path / "gateway.yaml"
    path.write_text(
        config_text.replace(f"{key}: true", f"{key}: {value}"),
        encoding="utf-8",
    )

    with pytest.raises(gateway.GatewayConfigError, match=f"{key} must be a boolean"):
        gateway.load_config(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("tokens_per_minute", "2000"), ("total_token_quota", "100000")],
)
def test_config_rejects_boolean_token_limits(
    repo_root: Path,
    tmp_path: Path,
    field: str,
    value: str,
):
    config_text = (repo_root / "config" / "gateway.yaml").read_text(encoding="utf-8")
    path = tmp_path / "gateway.yaml"
    path.write_text(
        config_text.replace(f"{field}: {value}", f"{field}: true", 1),
        encoding="utf-8",
    )

    with pytest.raises(gateway.GatewayConfigError, match=f"{field} must be a positive integer"):
        gateway.load_config(path)


def _live_contract(config: gateway.GatewayConfig) -> dict[str, object]:
    subscription = "00000000-0000-0000-0000-000000000000"
    resource_group = "rg-contoso-agents"
    resource_prefix = "contoso-agents"
    gateway_url = "https://gateway.example.test"
    gateway_id = (
        f"/subscriptions/{subscription}/resourceGroups/{resource_group}/providers/"
        f"Microsoft.ApiManagement/service/{resource_prefix}-gateway"
    )
    models = gateway.expected_connection_models(config)
    metadata = {
        "authConfig": json.dumps(gateway.CONNECTION_AUTH_CONFIG),
        "deploymentInPath": "false",
        "models": json.dumps(models),
    }
    connection_names = [f"ai-gateway-{project}" for project in sorted(config.projects)]
    connection_names.append("ai-gateway-default")
    connections = [
        {
            "name": name,
            "properties": {
                "authType": "ApiKey",
                "category": "ApiManagement",
                "isSharedToAll": name == "ai-gateway-default",
                "metadata": metadata,
                "target": f"{gateway_url}/models/{name.removeprefix('ai-gateway-')}",
            },
        }
        for name in connection_names
    ]
    all_limits = {"default": config.default_project, **config.projects}
    fragments = [
        {
            "name": f"{project}-token-governance",
            "properties": {"value": gateway.render_token_fragment(project, limits)},
        }
        for project, limits in all_limits.items()
    ]
    api_policies = {
        route: {
            "properties": {
                "value": (
                    "<policies><inbound><base />"
                    f'<include-fragment fragment-id="{route}-token-governance" />'
                    '<set-backend-service backend-id="foundry-models" />'
                    '<authentication-managed-identity '
                    'resource="https://cognitiveservices.azure.com" />'
                    "</inbound><backend><forward-request /></backend>"
                    "<outbound><base /></outbound><on-error><base /></on-error></policies>"
                )
            }
        }
        for route in ("default", *sorted(config.projects))
    }
    apis = [
        {
            "name": f"foundry-{route}",
            "path": f"models/{route}",
            "displayName": f"Foundry {route}",
            "subscriptionRequired": True,
        }
        for route in ("default", *sorted(config.projects))
    ]
    deployments = [
        {
            "name": deployment.deployment_name,
            "properties": {
                "model": {
                    "name": deployment.model_name,
                    "version": deployment.version,
                    "format": deployment.format,
                },
                "raiPolicyName": deployment.rai_policy_name,
                "provisioningState": "Succeeded",
            },
        }
        for deployment in config.model_deployments
    ]
    assignments = [
        {
            "name": f"{resource_prefix}-approved-models",
            "enforcementMode": "Default",
            "policyDefinitionId": gateway.APPROVED_MODELS_POLICY,
            "parameters": {
                "effect": {"value": "Deny"},
                "allowedPublishers": {"value": list(config.allowed_publishers)},
                "allowedAssetIds": {"value": list(config.allowed_asset_ids)},
            },
        },
        {
            "name": f"{resource_prefix}-model-eligibility",
            "enforcementMode": "Default",
            "policyDefinitionId": gateway.MODEL_ELIGIBILITY_POLICY,
            "parameters": {
                "effect": {"value": "Deny"},
                "onlyAllowDirectFromAzure": {"value": config.only_allow_direct_from_azure},
                "denyPreviewModels": {"value": config.deny_preview_models},
            },
        },
    ]
    filters = [
        {
            "name": name,
            "source": source,
            "enabled": True,
            "blocking": True,
            "severityThreshold": "Medium",
        }
        for name, source in sorted(gateway._SEVERITY_FILTERS)
    ]
    filters.extend(
        {
            "name": name,
            "source": source,
            "enabled": True,
            "blocking": True,
        }
        for name, source in sorted(gateway._BINARY_FILTERS)
    )
    guardrail = {
        "name": f"{resource_prefix}-guardrails",
        "properties": {
            "mode": "Blocking",
            "basePolicyName": "Microsoft.DefaultV2",
            "contentFilters": filters,
        },
    }
    diagnostics = [
        {
            "name": f"{resource_prefix}-gateway-logs",
            "logAnalyticsDestinationType": "Dedicated",
            "workspaceId": (
                f"/subscriptions/{subscription}/resourceGroups/{resource_group}/providers/"
                f"Microsoft.OperationalInsights/workspaces/{resource_prefix}-logs"
            ),
            "logs": [{"category": "GatewayLogs", "enabled": True}],
            "metrics": [{"category": "AllMetrics", "enabled": True}],
        }
    ]
    return {
        "subscription": subscription,
        "resource_group": resource_group,
        "resource_prefix": resource_prefix,
        "gateway_url": gateway_url,
        "gateway_id": gateway_id,
        "connections": connections,
        "fragments": fragments,
        "api_policies": api_policies,
        "apis": apis,
        "deployments": deployments,
        "assignments": assignments,
        "guardrail": guardrail,
        "diagnostics": diagnostics,
    }


def test_live_verifier_accepts_the_complete_control_plane_contract(repo_root: Path, monkeypatch):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)

    def fake_run(args):
        if args[:2] == ["apim", "show"]:
            return {
                "id": live["gateway_id"],
                "provisioningState": "Succeeded",
                "sku": {"name": "BasicV2"},
                "location": "North Central US",
                "identity": {"type": "SystemAssigned"},
                "gatewayUrl": live["gateway_url"],
            }
        if args[:2] == ["account", "show"]:
            return {"id": live["subscription"]}
        if args[:3] == ["monitor", "diagnostic-settings", "list"]:
            return live["diagnostics"]
        if args[:3] == ["apim", "api", "list"]:
            return live["apis"]
        if args[:4] == ["cognitiveservices", "account", "deployment", "list"]:
            return live["deployments"]
        if args[:3] == ["policy", "assignment", "list"]:
            return live["assignments"]
        url = args[args.index("--url") + 1]
        if "/projects/" in url and "/connections?" in url:
            project = url.split("/projects/", maxsplit=1)[1].split("/", maxsplit=1)[0]
            return {
                "value": [
                    connection
                    for connection in live["connections"]
                    if connection["name"] == f"ai-gateway-{project}"
                ]
            }
        if "/connections?" in url:
            return {
                "value": [
                    connection
                    for connection in live["connections"]
                    if connection["name"] == "ai-gateway-default"
                ]
            }
        if "/policyFragments?" in url:
            return {"value": live["fragments"]}
        if "/apis/foundry-" in url and "/policies/policy?" in url:
            route = url.split("/apis/foundry-", maxsplit=1)[1].split("/", maxsplit=1)[0]
            return live["api_policies"][route]
        if "/raiPolicies/" in url:
            return live["guardrail"]
        raise AssertionError(f"unexpected Azure CLI call: {args}")

    monkeypatch.setattr(gateway.azure_cli, "run", fake_run)
    status = gateway.collect_status(
        str(live["resource_group"]),
        str(live["resource_prefix"]),
        config,
    )

    assert status.ok
    assert status.verified_model_deployments == ("contoso-field-model", "travel-gpt-5-4-mini")
    assert status.api_policy_routes == ("default", "platform", "research", "support", "travel")
    assert status.diagnostic_workspace_name == "contoso-agents-logs"


def test_status_accepts_dynamic_project_count_and_guardrail_name():
    projects = ("fifth", "platform", "research", "support", "travel")
    status = gateway.GatewayStatus(
        apim_state="Succeeded",
        apim_sku="BasicV2",
        apim_location="North Central US",
        managed_identity=True,
        resource_specific_logs=True,
        diagnostic_workspace_name="alternate-logs",
        shared_default_connection=True,
        enrolled_projects=projects,
        verified_connections=tuple(
            sorted(("ai-gateway-default", *(f"ai-gateway-{project}" for project in projects)))
        ),
        verified_model_deployments=("model",),
        token_policy_projects=projects,
        api_policy_routes=tuple(sorted(("default", *projects))),
        policy_assignments=("alternate-approved-models", "alternate-model-eligibility"),
        guardrail_policy_name="alternate-guardrails",
        guardrail_policy_mode="Blocking",
        guardrail_base_policy="Microsoft.DefaultV2",
        guardrail_filters_valid=True,
    )

    assert status.ok


def test_live_verifier_passes_unexpected_connections_to_inventory_check(
    repo_root: Path,
    monkeypatch,
):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    unexpected = {
        "name": "ai-gateway-bypass",
        "properties": {"category": "ApiManagement"},
    }

    def fake_run(args):
        if args[:2] == ["apim", "show"]:
            return {
                "id": live["gateway_id"],
                "gatewayUrl": live["gateway_url"],
            }
        if args[:2] == ["account", "show"]:
            return {"id": live["subscription"]}
        if args[:3] == ["monitor", "diagnostic-settings", "list"]:
            return live["diagnostics"]
        url = args[args.index("--url") + 1]
        if "/projects/" in url:
            project = url.split("/projects/", maxsplit=1)[1].split("/", maxsplit=1)[0]
            return {
                "value": [
                    connection
                    for connection in live["connections"]
                    if connection["name"] == f"ai-gateway-{project}"
                ]
            }
        return {
            "value": [
                *(
                    connection
                    for connection in live["connections"]
                    if connection["name"] == "ai-gateway-default"
                ),
                unexpected,
            ]
        }

    monkeypatch.setattr(gateway.azure_cli, "run", fake_run)

    with pytest.raises(gateway.GatewayConfigError, match="ai-gateway-bypass"):
        gateway.collect_status(
            str(live["resource_group"]),
            str(live["resource_prefix"]),
            config,
        )


def test_verifier_rejects_token_limit_drift(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    fragments = copy.deepcopy(live["fragments"])
    fragments[0]["properties"]["value"] = fragments[0]["properties"]["value"].replace(
        'tokens-per-minute="2000"',
        'tokens-per-minute="2001"',
    )

    with pytest.raises(gateway.GatewayConfigError, match="token fragment attributes"):
        gateway._verify_token_fragments(fragments, config)


def test_verifier_rejects_api_policy_drift(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    policies = copy.deepcopy(live["api_policies"])
    policies["travel"]["properties"]["value"] = policies["travel"]["properties"]["value"].replace(
        "https://cognitiveservices.azure.com",
        "https://management.azure.com",
    )

    with pytest.raises(gateway.GatewayConfigError, match="managed-identity audience"):
        gateway._verify_api_policies(policies, config)


def test_verifier_rejects_undeclared_apim_api(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    apis = copy.deepcopy(live["apis"])
    apis.append(
        {
            "name": "unguarded-model-route",
            "path": "models/unguarded",
            "displayName": "Unguarded",
            "subscriptionRequired": False,
        }
    )

    with pytest.raises(gateway.GatewayConfigError, match="APIM API inventory mismatch"):
        gateway._verify_api_inventory(apis, config)


def test_verifier_does_not_trust_the_builtin_echo_display_signature(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    apis = copy.deepcopy(live["apis"])
    apis.append(
        {
            "name": "echo-api",
            "path": "echo",
            "displayName": "Echo API",
            "subscriptionRequired": True,
        }
    )

    with pytest.raises(gateway.GatewayConfigError, match="echo-api"):
        gateway._verify_api_inventory(apis, config)


def test_verifier_rejects_wrong_diagnostic_workspace(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    diagnostics = copy.deepcopy(live["diagnostics"])
    diagnostics[0]["workspaceId"] = "/subscriptions/redacted/wrong"

    with pytest.raises(gateway.GatewayConfigError, match="wrong Log Analytics workspace"):
        gateway._verify_diagnostics(
            diagnostics,
            subscription_id=str(live["subscription"]),
            resource_group=str(live["resource_group"]),
            resource_prefix=str(live["resource_prefix"]),
        )


def test_verifier_rejects_empty_connection_models(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    connections = copy.deepcopy(live["connections"])
    connections[0]["properties"]["metadata"]["models"] = "[]"

    with pytest.raises(gateway.GatewayConfigError, match="static model metadata"):
        gateway._verify_connections(
            connections,
            config=config,
            gateway_url=str(live["gateway_url"]),
        )


def test_empty_dependency_catalog_does_not_claim_live_models(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    clean_config = replace(config, model_deployments=())

    assert gateway.expected_connection_models(clean_config) == []
    assert gateway._verify_model_deployments([], clean_config) == ()


def test_model_deployment_input_attests_exact_live_catalog(repo_root: Path, monkeypatch):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    raw = json.dumps([asdict(deployment) for deployment in config.model_deployments])

    def fake_run(args):
        if args[:3] == ["cognitiveservices", "account", "list"]:
            return [{"name": "contoso-agents-foundry"}]
        if args[:4] == ["cognitiveservices", "account", "deployment", "list"]:
            return live["deployments"]
        raise AssertionError(f"unexpected Azure CLI call: {args}")

    monkeypatch.setattr(gateway.azure_cli, "run", fake_run)

    assert gateway.attest_model_deployment_input(
        "rg-contoso-agents",
        "contoso-agents",
        config,
        raw,
    ) == ("contoso-field-model", "travel-gpt-5-4-mini")


@pytest.mark.parametrize("mutation", ["absent", "extra"])
def test_model_deployment_input_rejects_catalog_inventory_drift(
    repo_root: Path,
    mutation: str,
    monkeypatch,
):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    supplied = [asdict(deployment) for deployment in config.model_deployments]
    if mutation == "absent":
        supplied.pop()
    else:
        supplied.append(
            {
                "deployment_name": "unapproved",
                "model_name": "gpt-unapproved",
                "version": "1",
                "format": "OpenAI",
                "rai_policy_name": "Microsoft.DefaultV2",
            }
        )

    monkeypatch.setattr(
        gateway.azure_cli,
        "run",
        lambda _args: [{"name": "contoso-agents-foundry"}],
    )
    with pytest.raises(gateway.GatewayConfigError, match="does not match expected"):
        gateway.attest_model_deployment_input(
            "rg-contoso-agents",
            "contoso-agents",
            config,
            json.dumps(supplied),
        )


def test_model_deployment_input_allows_empty_catalog_before_account_exists(
    repo_root: Path,
    monkeypatch,
):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    monkeypatch.setattr(gateway.azure_cli, "run", lambda _args: [])

    assert gateway.attest_model_deployment_input(
        "rg-contoso-agents",
        "contoso-agents",
        config,
        "[]",
    ) == ()


def test_model_deployment_input_rejects_failed_live_deployment(repo_root: Path, monkeypatch):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    deployments = copy.deepcopy(live["deployments"])
    deployments[0]["properties"]["provisioningState"] = "Failed"
    raw = json.dumps([asdict(deployment) for deployment in config.model_deployments])

    def fake_run(args):
        if args[:3] == ["cognitiveservices", "account", "list"]:
            return [{"name": "contoso-agents-foundry"}]
        return deployments

    monkeypatch.setattr(gateway.azure_cli, "run", fake_run)

    with pytest.raises(gateway.GatewayConfigError, match="does not match config"):
        gateway.attest_model_deployment_input(
            "rg-contoso-agents",
            "contoso-agents",
            config,
            raw,
        )


def test_verifier_rejects_shared_project_connection(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    connections = copy.deepcopy(live["connections"])
    project_connection = next(
        connection for connection in connections if connection["name"] != "ai-gateway-default"
    )
    project_connection["properties"]["isSharedToAll"] = True

    with pytest.raises(gateway.GatewayConfigError, match="isSharedToAll"):
        gateway._verify_connections(
            connections,
            config=config,
            gateway_url=str(live["gateway_url"]),
        )


def test_verifier_rejects_disabled_policy_enforcement(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    assignments = copy.deepcopy(live["assignments"])
    assignments[0]["enforcementMode"] = "DoNotEnforce"

    with pytest.raises(gateway.GatewayConfigError, match="enforcementMode"):
        gateway._verify_policy_assignments(
            assignments,
            config=config,
            resource_prefix=str(live["resource_prefix"]),
        )


def test_verifier_rejects_nonblocking_guardrail_filter(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")
    live = _live_contract(config)
    guardrail = copy.deepcopy(live["guardrail"])
    guardrail["properties"]["contentFilters"][0]["blocking"] = False

    with pytest.raises(gateway.GatewayConfigError, match="enabled and blocking"):
        gateway._verify_guardrail_policy(
            guardrail,
            expected_name="contoso-agents-guardrails",
        )
