# Sources

Every product, behaviour and pricing claim on this site traces to a first-party
Microsoft page. All links were verified on **2026-08-25**.

Where a capability is in **public preview** it is labelled as such wherever it
appears. Preview features can change without notice; re-check the source before
relying on one.

## Microsoft Foundry

| Topic | Source |
| --- | --- |
| Region support | <https://learn.microsoft.com/azure/foundry/reference/region-support> |
| Agent Service overview | <https://learn.microsoft.com/azure/foundry/agents/overview> |
| Agent Service limits, quotas and regions | <https://learn.microsoft.com/azure/foundry/agents/concepts/limits-quotas-regions> |
| Model quotas and limits | <https://learn.microsoft.com/azure/foundry/openai/quotas-limits> |
| Prompt agent quickstart | <https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent> |
| Client-side agent tracing | <https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-client-side> |
| Azure Developer CLI evaluation | <https://learn.microsoft.com/azure/foundry/observability/how-to/azure-developer-cli-evaluation> |
| Agent Framework hosted agents | <https://learn.microsoft.com/azure/foundry/how-to/develop/framework-hosted-agents> |
| Unified `azure.yaml` reference | <https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference> |
| Hosted-agent deployment and protocol contract | <https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent> |
| Hosted-agent versions, routing and monitoring | <https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-agent> |
| Hosted-agent permissions | <https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions> |
| Private Azure Container Registry deployment | <https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-private-azure-container-registry> |
| Hosted-agent virtual networking | <https://learn.microsoft.com/azure/foundry/agents/how-to/virtual-networks> |
| Agent Framework agent observability | <https://learn.microsoft.com/agent-framework/agents/observability> |
| Agent Framework workflow observability | <https://learn.microsoft.com/agent-framework/workflows/observability> |
| Hosted agents | <https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents> |
| Hosted runtime contract | <https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-contract> |
| Foundry SDKs and project endpoint shape | <https://learn.microsoft.com/azure/foundry/how-to/develop/sdk-overview> |
| LangGraph tracing | <https://learn.microsoft.com/azure/foundry/how-to/develop/langchain-traces> |
| Foundry Models pricing | <https://azure.microsoft.com/pricing/details/foundry-models/> |
| External-agent registration and limitations (public preview) | <https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent> |

Model quota is **subscription-level**, and Global Standard deployments draw from
a single pool shared across regions. A region can therefore pass a model
availability check and still have no usable quota, which is why availability
and quota are separate gates.

Hosted agents use a unified `azure.yaml`, and the Responses protocol supplies
conversation lifecycle, streaming, cancellation, and health handling. Each
deployed agent receives its own identity; downstream roles are assigned only
after that identity exists and only at the resource it must access.

!!! warning "Public preview"
    Hosted agents and parts of the unified deployment configuration are public
    preview. Managed hosting is billed by CPU and memory consumed across active
    sessions.

## API Management

| Topic | Source |
| --- | --- |
| v2 tiers overview | <https://learn.microsoft.com/azure/api-management/v2-service-tiers-overview> |
| v2 tier region availability | <https://learn.microsoft.com/azure/api-management/api-management-region-availability> |
| Key concepts | <https://learn.microsoft.com/azure/api-management/api-management-key-concepts> |
| Configure AI Gateway in Foundry | <https://learn.microsoft.com/azure/foundry/configuration/enable-ai-api-management-gateway-portal> |
| AI Gateway static model discovery | <https://github.com/Azure-Samples/AI-Gateway/tree/main/labs/model-routing-factory> |
| API Management monitoring | <https://learn.microsoft.com/azure/api-management/api-management-howto-use-azure-monitor> |
| Gateway log schema | <https://learn.microsoft.com/azure/azure-monitor/reference/tables/apimanagementgatewaylogs> |
| LLM token limits | <https://learn.microsoft.com/azure/api-management/llm-token-limit-policy> |
| Import a Foundry API | <https://learn.microsoft.com/azure/api-management/azure-ai-foundry-api> |

The v2 tiers are available in a **narrower** set of regions than API Management
itself. The region availability page is the authority, not the resource
provider's location list.

## Model governance and safety

| Topic | Source |
| --- | --- |
| Built-in model deployment policies | <https://learn.microsoft.com/azure/foundry/how-to/model-deployment-policy> |
| RAI policy Bicep resource | <https://learn.microsoft.com/azure/templates/microsoft.cognitiveservices/2026-05-01/accounts/raipolicies> |
| Content filtering | <https://learn.microsoft.com/azure/foundry/openai/concepts/content-filter> |
| Abuse monitoring | <https://learn.microsoft.com/azure/foundry/openai/concepts/abuse-monitoring> |

## Azure SRE Agent

| Topic | Source |
| --- | --- |
| Overview | <https://learn.microsoft.com/azure/sre-agent/overview> |
| Supported regions | <https://learn.microsoft.com/azure/sre-agent/supported-regions> |
| Pricing and billing | <https://learn.microsoft.com/azure/sre-agent/pricing-billing> |
| Infrastructure as code | <https://learn.microsoft.com/azure/sre-agent/deploy-iac> |
| ARM resource definition | <https://learn.microsoft.com/azure/templates/microsoft.app/2026-01-01/agents> |
| SRE Agent Standard User built-in role | <https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/management-and-governance#sre-agent-standard-user> |
| Supported regions | <https://learn.microsoft.com/azure/sre-agent/supported-regions> |

Billed in Azure Agent Units. The AAU consumption rates are published; a
USD-per-AAU conversion is not, and the Retail Prices API returns no SRE Agent
meter. See [Cost model](../platform/costs.md).

## Logic Apps

| Topic | Source |
| --- | --- |
| Agentic workflow concepts | <https://learn.microsoft.com/azure/logic-apps/agent-workflows-concepts> |
| Creating autonomous agent workflows | <https://learn.microsoft.com/azure/logic-apps/create-autonomous-agent-workflows> |
| Workflow ARM/Bicep resource schema | <https://learn.microsoft.com/azure/templates/microsoft.logic/workflows> |
| Pricing | <https://azure.microsoft.com/pricing/details/logic-apps/> |
| Consumption autonomous workflow guide | <https://learn.microsoft.com/azure/logic-apps/create-autonomous-agent-workflows> |

!!! warning "Public preview"
    Consumption autonomous agentic workflows are in public preview. Microsoft
    publishes no region table for them. The billing meters for agent-loop input
    and output tokens do exist in the Retail Prices API and are priced live.

## Container Apps

| Topic | Source |
| --- | --- |
| FAQ, including the region query | <https://learn.microsoft.com/azure/container-apps/faq> |
| Pricing, including free grants | <https://azure.microsoft.com/pricing/details/container-apps/> |
| Scheduled and manual jobs | <https://learn.microsoft.com/azure/container-apps/jobs> |
| Security | <https://learn.microsoft.com/azure/container-apps/security> |
| Managed identities | <https://learn.microsoft.com/azure/container-apps/managed-identity> |
| Scaling and scale-to-zero | <https://learn.microsoft.com/azure/container-apps/scale-app> |

There is no static region page. The documented method is to query the resource
provider, which is what region selection does.

The monthly free grants are a **billing credit**, not a price tier, so they never
appear in a Retail Prices API response. The cost model subtracts them explicitly.

## Microsoft Foundry Control Plane

| Topic | Source |
| --- | --- |
| Inventory, supported platforms, observability and lifecycle | <https://learn.microsoft.com/azure/foundry/control-plane/how-to-manage-agents> |

Control Plane automatically discovers Azure SRE Agent and Logic Apps agent
loops. Both platforms support start and stop. Logic Apps agent loops appear in
inventory, but their traces and metrics are explicitly unsupported.

## Azure Monitor

| Topic | Source |
| --- | --- |
| Log Analytics cost calculations | <https://learn.microsoft.com/azure/azure-monitor/logs/cost-logs> |
| Pricing | <https://azure.microsoft.com/pricing/details/monitor/> |
| Azure Monitor OpenTelemetry configuration | <https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-configuration?tabs=python> |

## Azure Kubernetes Service

| Topic | Source |
| --- | --- |
| Microsoft Entra Workload ID | <https://learn.microsoft.com/azure/aks/workload-identity-overview> |
| Azure Key Vault Secrets Store CSI Driver | <https://learn.microsoft.com/azure/aks/csi-secrets-store-driver> |
| CSI Driver identity access | <https://learn.microsoft.com/azure/aks/csi-secrets-store-identity-access> |

## Pricing API

| Topic | Source |
| --- | --- |
| Azure Retail Prices API | <https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices> |
| Pricing Calculator (human cross-check only) | <https://azure.microsoft.com/pricing/calculator/> |

Filter values are **case-sensitive**. The API version is pinned in the client so
a server-side default change cannot silently move the numbers.

## Copilot Studio and Power Platform

| Topic | Source |
| --- | --- |
| What Copilot Studio is | <https://learn.microsoft.com/microsoft-copilot-studio/fundamentals-what-is-copilot-studio> |
| Billing and licensing | <https://learn.microsoft.com/microsoft-copilot-studio/billing-licensing> |
| Message and credit management | <https://learn.microsoft.com/microsoft-copilot-studio/requirements-messages-management> |
| Managing Copilot credit capacity | <https://learn.microsoft.com/power-platform/admin/manage-copilot-studio-copilot-credits-capacity> |
| Data location | <https://learn.microsoft.com/microsoft-copilot-studio/data-location> |
| Power Platform regions | <https://learn.microsoft.com/power-platform/admin/regions-overview> |
| Creating an environment | <https://learn.microsoft.com/power-platform/admin/create-environment> |
| Publishing channels | <https://learn.microsoft.com/microsoft-copilot-studio/publication-fundamentals-publish-channels> |
| Copilot Studio ALM strategy | <https://learn.microsoft.com/microsoft-copilot-studio/guidance/alm> |
| Copilot Studio solutions | <https://learn.microsoft.com/microsoft-copilot-studio/authoring-solutions-overview> |
| Power Platform CLI solution commands | <https://learn.microsoft.com/power-platform/developer/cli/reference/solution> |
| Install Power Platform CLI with .NET tool | <https://learn.microsoft.com/power-platform/developer/howto/install-cli-net-tool> |
| Managed and unmanaged solution packaging | <https://learn.microsoft.com/power-platform/alm/solution-packager-tool#managed-and-unmanaged-solutions> |
| Pipelines in Power Platform | <https://learn.microsoft.com/power-platform/alm/pipelines> |
| Redeploy past solution versions | <https://learn.microsoft.com/power-platform/alm/redeploy-past-solution-versions> |
| Manage Power Platform application users | <https://learn.microsoft.com/power-platform/admin/manage-application-users> |
| Remove unused Entra application credentials | <https://learn.microsoft.com/entra/identity/monitoring-health/recommendation-remove-unused-credential-from-apps> |
| Copilot Studio authoring permissions | <https://learn.microsoft.com/microsoft-copilot-studio/guidance/sec-gov-phase3#assign-copilot-studio-authoring-permissions-by-using-security-roles> |
| Agent-level Application Insights telemetry | <https://learn.microsoft.com/microsoft-copilot-studio/advanced-bot-framework-composer-capture-telemetry> |
| Environment-level Application Insights telemetry (public preview) | <https://learn.microsoft.com/microsoft-copilot-studio/advanced-environment-level-agent-telemetry> |
| Transcript access and retention | <https://learn.microsoft.com/microsoft-copilot-studio/admin-transcript-controls> |
| Microsoft 365 agent administration | <https://learn.microsoft.com/microsoft-365/copilot/extensibility/manage#agents-built-with-microsoft-copilot-studio> |
| Copilot Credits allocation | <https://learn.microsoft.com/power-platform/admin/programmability-tutorial-manage-copilot-credit-allocations> |

Copilot Studio's billing currency changed from *messages* to **Copilot Credits**.
The USD-per-credit rate is published in the licensing guide PDF rather than on
Learn, so this project treats its planning figure as unverified until confirmed
against an invoice.

Power Platform environments can be created in any region **except** India and
Australia, which are restricted to tenants based in those countries. If a
tenant's location has no matching Copilot Studio data location, data is stored in
the United States.

Environment-level Copilot Studio export to Application Insights is **public
preview** and includes conversation, tool, and identity detail. This project
allows it only for synthetic DEV/TEST sessions. PROD uses agent-level telemetry
with conversation detail disabled, and Dataverse transcript saving is disabled.

## Data residency

| Topic | Source |
| --- | --- |
| Data residency | <https://azure.microsoft.com/explore/global-infrastructure/data-residency/> |
| Geographies | <https://azure.microsoft.com/explore/global-infrastructure/geographies/> |
| Products available by region | <https://azure.microsoft.com/explore/global-infrastructure/products-by-region/> |

## Azure CLI

| Topic | Source |
| --- | --- |
| `az provider` | <https://learn.microsoft.com/cli/azure/provider> |
| `az cognitiveservices model` | <https://learn.microsoft.com/cli/azure/cognitiveservices/model> |
| `az cognitiveservices usage` | <https://learn.microsoft.com/cli/azure/cognitiveservices/usage> |
| Azure Developer CLI hooks | <https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-extensibility> |
| List role assignments with Azure CLI | <https://learn.microsoft.com/azure/role-based-access-control/role-assignments-list-cli#list-role-assignments-for-a-subscription> |

## Tooling

| Topic | Source |
| --- | --- |
| Material for MkDocs diagrams | <https://squidfunk.github.io/mkdocs-material/reference/diagrams/> |
| Publishing a Material site | <https://squidfunk.github.io/mkdocs-material/publishing-your-site/> |
| GitHub Pages custom workflows | <https://docs.github.com/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages> |
| Pydantic AI OpenTelemetry instrumentation | <https://ai.pydantic.dev/logfire/> |
| GitHub Actions workflow-run metadata | <https://docs.github.com/rest/actions/workflow-runs#get-a-workflow-run> |
| GitHub Actions artifact digests | <https://docs.github.com/actions/tutorials/store-and-share-data#validating-artifacts> |
| GitHub artifact attestations | <https://docs.github.com/actions/concepts/security/artifact-attestations> |
