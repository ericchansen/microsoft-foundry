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
| Agent Framework hosted agents | <https://learn.microsoft.com/azure/foundry/how-to/develop/framework-hosted-agents> |
| Unified `azure.yaml` reference | <https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference> |
| Hosted-agent deployment and protocol contract | <https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent> |
| Hosted-agent versions, routing and monitoring | <https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-agent> |
| Hosted-agent permissions | <https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions> |
| Private Azure Container Registry deployment | <https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-private-azure-container-registry> |
| Hosted-agent virtual networking | <https://learn.microsoft.com/azure/foundry/agents/how-to/virtual-networks> |
| Agent Framework agent observability | <https://learn.microsoft.com/agent-framework/agents/observability> |
| Agent Framework workflow observability | <https://learn.microsoft.com/agent-framework/workflows/observability> |

Model quota is **subscription-level**, and Global Standard deployments draw from
a single pool shared across regions. A region can therefore pass a model
availability check and still have no usable headroom, which is why availability
and quota are separate gates.

## API Management

| Topic | Source |
| --- | --- |
| v2 tiers overview | <https://learn.microsoft.com/azure/api-management/v2-service-tiers-overview> |
| v2 tier region availability | <https://learn.microsoft.com/azure/api-management/api-management-region-availability> |
| Key concepts | <https://learn.microsoft.com/azure/api-management/api-management-key-concepts> |

The v2 tiers are available in a **narrower** set of regions than API Management
itself. The region availability page is the authority, not the resource
provider's location list.

## Azure SRE Agent

| Topic | Source |
| --- | --- |
| Overview | <https://learn.microsoft.com/azure/sre-agent/overview> |
| Supported regions | <https://learn.microsoft.com/azure/sre-agent/supported-regions> |
| Pricing and billing | <https://learn.microsoft.com/azure/sre-agent/pricing-billing> |

Billed in Azure Agent Units. The AAU consumption rates are published; a
USD-per-AAU conversion is not, and the Retail Prices API returns no SRE Agent
meter. See [Cost model](../platform/costs.md).

## Logic Apps

| Topic | Source |
| --- | --- |
| Agentic workflow concepts | <https://learn.microsoft.com/azure/logic-apps/agent-workflows-concepts> |
| Creating autonomous agent workflows | <https://learn.microsoft.com/azure/logic-apps/create-autonomous-agent-workflows> |
| Pricing | <https://azure.microsoft.com/pricing/details/logic-apps/> |

!!! warning "Public preview"
    Consumption autonomous agentic workflows are in public preview. Microsoft
    publishes no region table for them. The billing meters for agent-loop input
    and output tokens do exist in the Retail Prices API and are priced live.

## Container Apps

| Topic | Source |
| --- | --- |
| FAQ, including the region query | <https://learn.microsoft.com/azure/container-apps/faq> |
| Pricing, including free grants | <https://azure.microsoft.com/pricing/details/container-apps/> |

There is no static region page. The documented method is to query the resource
provider, which is what region selection does.

The monthly free grants are a **billing credit**, not a price tier, so they never
appear in a Retail Prices API response. The cost model subtracts them explicitly.

## Azure Monitor

| Topic | Source |
| --- | --- |
| Log Analytics cost calculations | <https://learn.microsoft.com/azure/azure-monitor/logs/cost-logs> |
| Pricing | <https://azure.microsoft.com/pricing/details/monitor/> |

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
| Overage enforcement | <https://learn.microsoft.com/microsoft-copilot-studio/requirements-messages-management#overage-enforcement> |
| Managing Copilot credit capacity | <https://learn.microsoft.com/power-platform/admin/manage-copilot-studio-copilot-credits-capacity> |
| Data location | <https://learn.microsoft.com/microsoft-copilot-studio/data-location> |
| Power Platform regions | <https://learn.microsoft.com/power-platform/admin/regions-overview> |
| Creating an environment | <https://learn.microsoft.com/power-platform/admin/create-environment> |
| Publishing channels | <https://learn.microsoft.com/microsoft-copilot-studio/publication-fundamentals-publish-channels> |

Copilot Studio's billing currency changed from *messages* to **Copilot Credits**.
The USD-per-credit rate is published in the licensing guide PDF rather than on
Learn, so this project treats its planning figure as unverified until confirmed
against an invoice.

Power Platform environments can be created in any region **except** India and
Australia, which are restricted to tenants based in those countries. If a
tenant's location has no matching Copilot Studio data location, data is stored in
the United States.

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

## Tooling

| Topic | Source |
| --- | --- |
| Material for MkDocs diagrams | <https://squidfunk.github.io/mkdocs-material/reference/diagrams/> |
| Publishing a Material site | <https://squidfunk.github.io/mkdocs-material/publishing-your-site/> |
| GitHub Pages custom workflows | <https://docs.github.com/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages> |
