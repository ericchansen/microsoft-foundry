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
        "identity": repo_root / "infra" / "modules" / "identity.bicep",
        "data": repo_root / "infra" / "modules" / "secure-data.bicep",
        "platforms": repo_root / "infra" / "modules" / "control-plane-platforms.bicep",
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


def test_sre_agent_is_new_isolated_and_review_only(infra):
    assert "Microsoft.App/agents@2026-01-01" in infra["platforms"]
    assert "name: '${resourcePrefix}-sre-control-plane'" in infra["platforms"]
    assert "accessLevel: 'Low'" in infra["platforms"]
    assert "mode: 'Review'" in infra["platforms"]
    assert "managedResources:" in infra["platforms"]
    assert "resourceGroup().id" in infra["platforms"]


def test_sre_agent_uses_shared_application_insights(infra):
    assert "applicationInsights.properties.AppId" in infra["platforms"]
    assert "applicationInsights.properties.ConnectionString" in infra["platforms"]
    assert "'service-name': 'contoso-sre-control-plane'" in infra["platforms"]


def test_logic_apps_agent_loop_is_bounded_and_synthetic(infra):
    assert "Microsoft.Logic/workflows@2019-05-01" in infra["platforms"]
    assert "kind: 'Agentic'" in infra["platforms"]
    assert "type: 'Agent'" in infra["platforms"]
    assert "Create_approval_recommendation" in infra["platforms"]
    assert "requiresHumanApproval: true" in infra["platforms"]
    assert "synthetic: true" in infra["platforms"]
    assert "count: 5" in infra["platforms"]


def test_platforms_have_dedicated_identities_and_bounded_rbac(infra):
    assert "'${resourcePrefix}-sre'" in infra["platforms"]
    assert "'${resourcePrefix}-approvals'" in infra["platforms"]
    assert "acdd72a7-3385-48ef-bd42-f606fba81ae7" in infra["platforms"]
    assert "43d0d8ad-25c7-4714-9337-8ba259a9fe05" in infra["platforms"]
    assert "73c42c96-874c-492b-b04d-ab87d138a893" in infra["platforms"]
    assert "Contributor" not in infra["platforms"]
    assert "scope: subscription()" not in infra["platforms"]


def test_no_compiled_arm_template_is_tracked(repo_root):
    assert not (repo_root / "infra" / "main.json").exists()
