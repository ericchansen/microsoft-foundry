"""Static contracts for the deployable telemetry spine.

These tests intentionally inspect source rather than calling Azure or Bicep. They
protect the security and ownership decisions even on machines without either CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def infra(repo_root: Path) -> dict[str, str]:
    paths = {
        "main": repo_root / "infra" / "main.bicep",
        "params": repo_root / "infra" / "main.bicepparam",
        "monitoring": repo_root / "infra" / "modules" / "monitoring.bicep",
        "gateway": repo_root / "infra" / "modules" / "gateway.bicep",
        "association": repo_root / "infra" / "modules" / "gateway-association.bicep",
        "governance": repo_root / "infra" / "modules" / "model-governance.bicep",
        "association_entrypoint": repo_root / "infra" / "gateway-association.bicep",
        "governance_entrypoint": repo_root / "infra" / "model-governance.bicep",
        "deny_probe": repo_root / "infra" / "validation" / "unapproved-model.bicep",
        "custom_agent": repo_root / "infra" / "policies" / "custom-agent.xml",
        "identity": repo_root / "infra" / "modules" / "identity.bicep",
        "data": repo_root / "infra" / "modules" / "secure-data.bicep",
    }
    return {name: path.read_text(encoding="utf-8") for name, path in paths.items()}


def test_template_is_resource_group_scoped(infra):
    assert "targetScope = 'resourceGroup'" in infra["main"]
    assert "scope: subscription()" not in infra["main"]


def test_uses_current_foundry_apis(infra):
    assert "Microsoft.CognitiveServices/accounts@2026-07-01" in infra["main"]
    assert "Microsoft.CognitiveServices/accounts/projects@2026-07-01" in infra["main"]
    assert "accounts/projects/connections@2026-05-01" in infra["main"]


def test_foundry_account_disables_local_auth(infra):
    assert "kind: 'AIServices'" in infra["main"]
    assert "disableLocalAuth: true" in infra["main"]
    assert "allowProjectManagement: true" in infra["main"]


def test_declares_exactly_four_projects(infra):
    for name in ("travel", "support", "research", "platform"):
        assert f"'{name}'" in infra["main"]
    assert "projectNames" in infra["main"]


def test_each_project_gets_the_shared_app_insights_connection(infra):
    assert "for (projectName, index) in projectNames" in infra["main"]
    assert "category: 'AppInsights'" in infra["main"]
    assert "name: 'appinsights-${projectName}'" in infra["main"]
    assert "isDefault" not in infra["main"]


def test_projects_can_read_shared_telemetry(infra):
    assert "73c42c96-874c-492b-b04d-ab87d138a893" in infra["main"]
    assert "dbc9c667-e97f-4491-aee6-90b9cf960190" in infra["main"]
    assert "principalId: foundryProjects[index].identity.principalId" in infra["main"]
    assert "scope: applicationInsights" in infra["main"]


def test_monitoring_retention_is_at_least_ninety_days(infra):
    assert "@minValue(90)" in infra["monitoring"]
    assert "retentionInDays: 90" in infra["main"]


def test_gateway_uses_basic_v2_in_selected_region(infra):
    assert "Microsoft.ApiManagement/service@2024-05-01" in infra["gateway"]
    assert "name: 'BasicV2'" in infra["gateway"]
    assert "capacity: 1" in infra["gateway"]
    assert "location: location" in infra["gateway"]
    assert "location: location" in infra["main"]


def test_gateway_has_owned_identity_and_no_secret(infra):
    assert "type: 'SystemAssigned'" in infra["gateway"]
    assert "publisherEmail: budgetContactEmails[0]" in infra["main"]
    assert "password" not in infra["gateway"].lower()
    assert "secret" not in infra["gateway"].lower()


def test_gateway_logs_use_resource_specific_schema(infra):
    assert "Microsoft.Insights/diagnosticSettings@2021-05-01-preview" in infra["gateway"]
    assert "logAnalyticsDestinationType: 'Dedicated'" in infra["gateway"]
    assert "category: 'GatewayLogs'" in infra["gateway"]
    assert "logAnalyticsWorkspaceResourceId: monitoring.outputs.logAnalyticsWorkspaceResourceId" in infra["main"]


def test_each_project_has_an_explicit_gateway_route_and_connection(infra):
    assert "for projectName in projectNames" in infra["association"]
    assert "name: 'foundry-${projectName}'" in infra["association"]
    assert "name: 'ai-gateway-${projectName}'" in infra["association"]
    assert "category: 'ApiManagement'" in infra["association"]
    assert "authHeaderName: 'Ocp-Apim-Subscription-Key'" in infra["association"]
    assert "projectSubscriptions[index].listSecrets().primaryKey" in infra["association"]
    assert "gatewayConfig.projects" in infra["association_entrypoint"]


def test_gateway_backend_uses_managed_identity(infra):
    assert "authentication-managed-identity" in infra["association"]
    assert "https://cognitiveservices.azure.com" in infra["association"]
    assert "principalId: gateway.identity.principalId" in infra["association"]
    assert "scope: foundryAccount" in infra["association"]


def test_token_governance_has_rate_and_total_quota(infra):
    assert "llm-token-limit" in infra["association"]
    assert "tokens-per-minute=" in infra["association"]
    assert "token-quota=" in infra["association"]
    assert "token-quota-period=" in infra["association"]
    assert "estimate-prompt-tokens=\"true\"" in infra["association"]


def test_model_governance_is_resource_group_scoped(infra):
    assert "Microsoft.Authorization/policyAssignments@2025-03-01" in infra["governance"]
    assert "tenantResourceId(" in infra["governance"]
    assert "scope: subscription()" not in infra["governance"]
    assert "scope: tenant()" not in infra["governance"]
    assert "value: 'Deny'" in infra["governance"]
    assert "denyPreviewModels" in infra["governance"]
    assert "onlyAllowDirectFromAzure" in infra["governance"]


def test_guardrail_policy_uses_stable_api_and_requires_deployment_attachment(infra):
    assert "accounts/raiPolicies@2026-05-01" in infra["governance"]
    assert "basePolicyName: 'Microsoft.DefaultV2'" in infra["governance"]
    assert "name: 'Jailbreak'" in infra["governance"]
    assert "output guardrailPolicyName" in infra["governance"]
    assert "output guardrailPolicyName string = modelGovernance.outputs.guardrailPolicyName" in infra["main"]
    guardrail_output = "output guardrailPolicyName string = governance.outputs.guardrailPolicyName"
    assert guardrail_output in infra["governance_entrypoint"]
    assert "raiPolicyName" not in infra["association"]


def test_unapproved_model_probe_is_what_if_only_contract(infra):
    assert "unapproved-policy-probe" in infra["deny_probe"]
    assert "Microsoft.CognitiveServices/accounts/deployments@2025-06-01" in infra["deny_probe"]


def test_custom_agent_template_keeps_backend_auth_at_gateway(infra):
    assert "{{CUSTOM_AGENT_BACKEND_ID}}" in infra["custom_agent"]
    assert "{{CUSTOM_AGENT_ENTRA_RESOURCE}}" in infra["custom_agent"]
    assert "authentication-managed-identity" in infra["custom_agent"]
    assert "llm-token-limit" in infra["custom_agent"]
    assert "set-backend-service" in infra["custom_agent"]


def test_app_insights_local_auth_exception_is_explicit(infra):
    assert "DisableLocalAuth: false" in infra["main"]
    assert "IngestionMode: 'LogAnalytics'" in infra["main"]


def test_key_vault_is_rbac_only(infra):
    assert "enableRbacAuthorization: true" in infra["data"]
    assert "enablePurgeProtection: true" in infra["data"]


def test_storage_requires_oauth(infra):
    assert "allowSharedKeyAccess: false" in infra["data"]
    assert "defaultToOAuthAuthentication: true" in infra["data"]
    assert "allowBlobPublicAccess: false" in infra["data"]


def test_acr_uses_cost_aware_secure_baseline(infra):
    assert "name: 'Basic'" in infra["data"]
    assert "adminUserEnabled: false" in infra["data"]
    assert "retentionPolicy:" in infra["data"]


def test_budget_alerts_cannot_omit_recipients(infra):
    assert "@minLength(1)\nparam budgetContactEmails array" in infra["main"]
    assert "readEnvironmentVariable('BUDGET_CONTACT_EMAIL')" in infra["params"]
    for threshold in (50, 80, 100):
        assert f"threshold: {threshold}" in infra["main"]


def test_github_oidc_has_no_secret(infra):
    assert "federatedIdentityCredentials@2024-11-30" in infra["identity"]
    assert "https://token.actions.githubusercontent.com" in infra["identity"]
    assert "api://AzureADTokenExchange" in infra["identity"]
    assert "password" not in infra["identity"].lower()
    assert "secret" not in infra["identity"].lower()


def test_role_assignments_are_principal_driven_and_group_scoped(infra):
    assert "principalId: deployIdentity.properties.principalId" in infra["identity"]
    assert "scope: foundryAccount" in infra["identity"]
    assert "scope: subscription()" not in infra["identity"]
    assert "scope: tenant()" not in infra["identity"]


def test_current_foundry_user_role_is_pinned(infra):
    assert "53ca6127-db72-4b80-b1b0-d745d6d5456d" in infra["identity"]


def test_no_compiled_arm_template_is_tracked(repo_root):
    assert not (repo_root / "infra" / "main.json").exists()
