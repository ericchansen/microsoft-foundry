"""Static contracts for the deployable telemetry spine.

These tests intentionally inspect source rather than calling Azure or Bicep. They
protect the security and ownership decisions even on machines without either CLI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


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
        "custom_agent": repo_root / "infra" / "policies" / "custom-agent.xml",
        "identity": repo_root / "infra" / "modules" / "identity.bicep",
        "data": repo_root / "infra" / "modules" / "secure-data.bicep",
        "workflow": repo_root / ".github" / "workflows" / "infra.yml",
        "platforms": repo_root / "infra" / "modules" / "control-plane-platforms.bicep",
        "azure": repo_root / "azure.yaml",
    }
    return {name: path.read_text(encoding="utf-8") for name, path in paths.items()}


def test_template_is_resource_group_scoped(infra):
    assert "targetScope = 'resourceGroup'" in infra["main"]
    assert "scope: subscription()" not in infra["main"]


def test_documented_install_includes_all_test_extras(repo_root):
    required = 'pip install -e ".[dev,docs,research,field]"'
    assert required in (repo_root / "README.md").read_text(encoding="utf-8")
    assert required in (repo_root / "docs" / "operations" / "verification.md").read_text(encoding="utf-8")


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


def test_projects_and_deployer_have_least_privilege_foundry_data_roles(infra):
    assert "foundryUserRoleId" in infra["main"]
    assert "principalId: foundryProjects[index].identity.principalId" in infra["main"]
    assert "foundryProjectManagerRoleId" in infra["identity"]
    assert "scope: foundryProject" in infra["identity"]


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
    assert "name: 'Ocp-Apim-Subscription-Key'" in infra["association"]
    assert "authConfig: string(connectionAuthConfig)" in infra["association"]
    assert "models: string(connectionModels)" in infra["association"]
    assert "models: '[]'" not in infra["association"]
    assert "projectSubscriptions[index].listSecrets().primaryKey" in infra["association"]
    assert "Microsoft.CognitiveServices/accounts/connections@2026-05-01" in infra["association"]
    assert "name: 'ai-gateway-default'" in infra["association"]
    project_connections = infra["association"].split(
        "resource projectGatewayConnections",
        maxsplit=1,
    )[1].split("resource defaultGatewayConnection", maxsplit=1)[0]
    default_connection = infra["association"].split(
        "resource defaultGatewayConnection",
        maxsplit=1,
    )[1]
    assert "isSharedToAll: false" in project_connections
    assert "isSharedToAll: true" in default_connection
    assert "defaultProjectTokenLimits: gatewayConfig.default_project" in infra["association_entrypoint"]
    assert "param existingModelDeployments array = []" in infra["association_entrypoint"]
    assert "modelDeployments: existingModelDeployments" in infra["association_entrypoint"]
    assert "modelDeployments: existingModelDeployments" in infra["main"]
    assert "gatewayConfig.expected_model_deployments" not in infra["association_entrypoint"]
    assert "gatewayConfig.expected_model_deployments" not in infra["main"]
    assert "gatewayConfig.projects" in infra["association_entrypoint"]


def test_deploy_workflow_checks_live_boundary_after_login(infra):
    login = infra["workflow"].index("uses: azure/login@")
    live_boundary = infra["workflow"].index("- name: Verify live ownership boundary")
    catalog_attestation = infra["workflow"].index(
        "- name: Attest existing Foundry model deployments"
    )
    what_if = infra["workflow"].index("- name: What-if")
    deploy = infra["workflow"].index("- name: Deploy")

    assert login < live_boundary < catalog_attestation < what_if
    assert login < live_boundary < catalog_attestation < deploy
    assert "existing_model_deployments_json:" in infra["workflow"]
    assert 'default: "[]"' in infra["workflow"]
    assert '--parameters existingModelDeployments="$EXISTING_MODEL_DEPLOYMENTS_JSON"' in infra["workflow"]
    assert "isinstance(json.loads" in infra["workflow"]
    assert "foundry gateway attest-model-deployments" in infra["workflow"]
    assert 'if test "${{ inputs.operation }}" != "verify"; then' in infra["workflow"]
    assert "optional_args+=(--deployment-readiness)" in infra["workflow"]


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
    assert infra["data"].count("publicNetworkAccess: 'Disabled'") == 2
    assert "defaultAction: 'Deny'" in infra["data"]


def test_shared_acr_requires_identity_and_supports_hosted_agent_images(infra):
    assert "name: 'Basic'" in infra["data"]
    assert "adminUserEnabled: false" in infra["data"]
    assert "anonymousPullEnabled: false" in infra["data"]
    assert "publicNetworkAccess: 'Enabled'" in infra["data"]
    assert "retentionPolicy:" in infra["data"]
    assert "privateEndpoints@" not in infra["data"]
    assert "privateDnsZones@" not in infra["data"]
    assert "deployAcrPush" in infra["data"]
    assert "scope: registry" in infra["data"]
    assert "deployPrincipalId: identities.outputs.deployIdentityPrincipalId" in infra["main"]


def test_support_model_deployment_is_explicit_and_owned_outside_gateway(infra):
    config = yaml.safe_load(infra["azure"])
    deployment = config["services"]["support-project"]["deployments"][0]

    assert deployment == {
        "name": "support-gpt-5-4-mini",
        "model": {
            "format": "OpenAI",
            "name": "gpt-5.4-mini",
            "version": "2026-03-17",
        },
        "sku": {"name": "GlobalStandard", "capacity": 10},
    }
    assert "accounts/deployments@" not in infra["main"]
    assert "param existingModelDeployments array = []" in infra["main"]


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
    assert "kind: 'Agentic'" not in infra["platforms"]
    assert "type: 'Agent'" in infra["platforms"]
    assert "Create_approval_recommendation" in infra["platforms"]
    assert "Create_synthetic_review_envelope" in infra["platforms"]
    assert "requiresHumanApproval: true" in infra["platforms"]
    assert "additionalProperties: false" in infra["platforms"]
    assert "expression: '@equals(triggerBody()?[\\'synthetic\\'], true)'" in infra["platforms"]
    assert "synthetic: '@triggerBody()?[\\'synthetic\\']'" in infra["platforms"]
    assert "count: 5" in infra["platforms"]


def test_platforms_have_dedicated_identities_and_bounded_rbac(infra):
    assert "'${resourcePrefix}-sre'" in infra["platforms"]
    assert "'${resourcePrefix}-approvals'" in infra["platforms"]
    assert "acdd72a7-3385-48ef-bd42-f606fba81ae7" in infra["platforms"]
    assert "43d0d8ad-25c7-4714-9337-8ba259a9fe05" in infra["platforms"]
    assert "73c42c96-874c-492b-b04d-ab87d138a893" in infra["platforms"]
    assert "2d84a65a-63b2-4343-bbb6-31105d857bc1" in infra["platforms"]
    assert "principalType: 'Group'" in infra["platforms"]
    assert "scope: sreAgent" in infra["platforms"]
    assert "Contributor" not in infra["platforms"]
    assert "scope: subscription()" not in infra["platforms"]


def test_optional_control_plane_is_disabled_by_default_and_uses_selected_region(infra):
    assert "@secure()\nparam sreOperatorGroupObjectId string = ''" in infra["main"]
    assert "param deploySreAgent bool = false" in infra["main"]
    assert "param deployApprovalsWorkflow bool = false" in infra["main"]
    assert "param deploySreAgent = false" in infra["params"]
    assert "param deployApprovalsWorkflow = false" in infra["params"]
    assert "sreLocation" not in infra["main"]
    assert "sreLocation" not in infra["params"]
    assert "sreLocation" not in infra["platforms"]
    assert "if (deploySreAgent)" in infra["platforms"]
    assert "if (deployApprovalsWorkflow)" in infra["platforms"]
    assert "fail('sreOperatorGroupObjectId is required when deploySreAgent is true')" in infra["platforms"]
    assert "principalId: validatedSreOperatorGroupObjectId" in infra["platforms"]
    assert "sreOperatorGroupObjectId: sreOperatorGroupObjectId" in infra["main"]


def test_deploy_identity_can_create_everything_main_deploys(infra, repo_root):
    """The deploy identity's declared roles must cover the template it deploys.

    Contributor excludes `Microsoft.Authorization/*/Write`, so a resource-group
    Contributor cannot create the model-governance policy assignments. The live
    boundary check also rejects role assignments that the plan does not declare,
    so the missing role cannot be granted out of band: it has to be deployed and
    declared, or the only automated deployment path fails authorization.
    """
    assert "Microsoft.Authorization/policyAssignments" in infra["governance"]
    assert "36243c78-bf99-498c-9df9-86d9f8d28608" in infra["identity"]
    assert "principalId: deployIdentity.properties.principalId" in infra["identity"]

    plan = yaml.safe_load((repo_root / "config" / "boundary.yaml").read_text(encoding="utf-8"))
    declared = {
        (entry["role"], entry["principal"], entry["scope"])
        for entry in plan["role_assignments"]
    }
    assert ("Resource Policy Contributor", "github-deploy-identity", ".") in declared
    assert (
        "Foundry User",
        "github-deploy-identity",
        "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry",
    ) in declared
    assert "resource deployFoundryUser" in infra["identity"]
    assert (
        "AcrPull",
        "support-project",
        "providers/Microsoft.ContainerRegistry/registries/contosoagents*",
    ) in declared
    assert (
        "AcrPull",
        "research-project",
        "providers/Microsoft.ContainerRegistry/registries/contosoagents*",
    ) in declared
    assert "resource hostedProjectRegistryPull" in infra["main"]
    assert "subject: githubOidcSubject" in infra["identity"]
    assert "readEnvironmentVariable('AZURE_GITHUB_OIDC_SUBJECT')" in infra["params"]


def test_no_compiled_arm_template_is_tracked(repo_root):
    assert not (repo_root / "infra" / "main.json").exists()


def test_all_resource_group_mutation_workflows_share_one_lock(repo_root):
    workflows = [
        (repo_root / ".github" / "workflows" / name).read_text(encoding="utf-8")
        for name in ("infra.yml", "support-agent.yml", "travel.yml")
    ]
    assert all("group: azure-rg-contoso-agents" in workflow for workflow in workflows)
    assert all("Refuse concurrent resource-group deployments" in workflow for workflow in workflows)
    assert all(
        "boundary --deployment-readiness" in workflow
        for workflow in workflows
        if "azd provision" in workflow or "Deploy authenticated Travel tool service" in workflow
    )
    sensitive_names = (
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
        "FOUNDRY_TRAVEL_PROJECT_ENDPOINT",
        "BUDGET_CONTACT_EMAIL",
        "SRE_OPERATOR_GROUP_OBJECT_ID",
    )
    for workflow in workflows:
        for name in sensitive_names:
            assert f"vars.{name}" not in workflow


def test_all_external_github_actions_are_commit_pinned(repo_root):
    for path in (repo_root / ".github" / "workflows").glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("uses:", "- uses:")) or "uses: ./" in stripped:
                continue
            reference = stripped.split("uses:", maxsplit=1)[1].strip().split()[0]
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), (path, reference)


def test_travel_release_rotation_is_gated_and_decommissions_previous_release(repo_root):
    workflow = (repo_root / ".github" / "workflows" / "travel.yml").read_text(
        encoding="utf-8"
    )
    readiness = workflow.index("foundry boundary --deployment-readiness")
    deploy = workflow.index("- name: Deploy authenticated Travel tool service")
    candidate = workflow.index("- name: Create, smoke, and evaluate exact candidate")
    decommission = workflow.index("- name: Decommission superseded tool release")
    converged = workflow.index("- name: Verify converged live boundary")

    assert readiness < deploy < candidate < decommission < converged
    assert "rotate_tool_key:" in workflow
    assert 'mode=rotate' in workflow
    assert 'mode=resume' in workflow
    assert 'More than two Travel tool releases exist' in workflow
    assert 'surplus_releases="$(comm -13' in workflow
    assert 'partial Travel deployment exists for an unexpected release' in workflow
    assert 'live_image="$(az containerapp show' in workflow
    assert 'test "$previous" != "$current"' in workflow
    assert "travel-openapi-${previous}" in workflow
    travel_bicep = (repo_root / "infra" / "travel.bicep").read_text(encoding="utf-8")
    assert "TRAVEL_TOOL_CREDENTIAL_REVISION" in travel_bicep
    assert "value: uniqueString(toolApiKey)" in travel_bicep


def test_infra_confirmation_input_is_not_interpolated_into_shell(repo_root):
    workflow = (repo_root / ".github" / "workflows" / "infra.yml").read_text(
        encoding="utf-8"
    )
    assert "CONFIRM_RESOURCE_GROUP: ${{ inputs.confirm_resource_group }}" in workflow
    assert 'test "$CONFIRM_RESOURCE_GROUP" = "$RESOURCE_GROUP"' in workflow
    assert 'test "${{ inputs.confirm_resource_group }}"' not in workflow


def test_runtime_images_remove_build_package_managers(repo_root):
    dockerfiles = [
        repo_root / "Dockerfile",
        repo_root / "agents" / "research" / "Dockerfile",
        repo_root / "agents" / "travel" / "Dockerfile",
        repo_root / "agents" / "field" / "Dockerfile",
    ]
    for path in dockerfiles:
        source = path.read_text(encoding="utf-8")
        assert "python -m pip check" in source, path
        assert "/site-packages/pip" in source, path
        assert "/site-packages/setuptools" in source, path
        assert "/usr/local/bin/pip3.13" in source, path


def test_container_vulnerability_gates_cover_every_shipped_image(repo_root):
    travel_workflow = (repo_root / ".github" / "workflows" / "travel.yml").read_text(
        encoding="utf-8"
    )
    ci_workflow = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    workflows = travel_workflow + ci_workflow

    assert workflows.count("uses: aquasecurity/trivy-action@") == 4
    assert workflows.count('exit-code: "1"') == 4
    assert workflows.count("severity: HIGH,CRITICAL") == 4
    for image in ("contoso-travel:ci", "contoso-support:ci", "contoso-research:ci", "contoso-field:ci"):
        assert f"image-ref: {image}" in workflows
    for copied_path in ("pyproject.toml", "README.md", "src/**", "config/**", "data/**", ".dockerignore"):
        assert f'- "{copied_path}"' in travel_workflow
    assert "skip-dirs" not in workflows
    assert "skip-files" not in workflows
    assert "trivyignores" not in workflows
    assert not (repo_root / ".trivyignore").exists()
    assert not (repo_root / ".trivyignore.yaml").exists()


def test_optional_platform_deploy_prices_the_exact_combination(repo_root):
    workflow = (repo_root / ".github" / "workflows" / "infra.yml").read_text(
        encoding="utf-8"
    )
    assert "FOUNDRY_MONTHLY_BUDGET_USD: ${{ vars.FOUNDRY_MONTHLY_BUDGET_USD }}" in workflow
    assert "foundry costs --enable-module approvals --enable-module sre" in workflow


def test_bicep_experimental_assertions_are_not_required(repo_root):
    config = yaml.safe_load(
        (repo_root / "infra" / "bicepconfig.json").read_text(encoding="utf-8")
    )
    assert "assertions" not in config["experimentalFeaturesEnabled"]
