# AI gateway

> **Takeaway:** clients send model requests through one project-owned API
> Management gateway; request logs flow separately to the shared telemetry spine.

```mermaid
flowchart LR
    client["Agent or application"]
    gateway["API Management<br/>Basic v2"]
    foundry["Foundry project<br/>model endpoint"]
    logs["ApiManagementGatewayLogs"]
    workspace["Shared Log Analytics"]
    external["External telemetry<br/>read-only correlation"]

    client -->|"model request"| gateway
    gateway -->|"governed request"| foundry
    gateway -.->|"resource-specific log"| logs
    logs --> workspace
    external -.->|"correlation only"| workspace
```

The gateway uses one Basic v2 unit in North Central US and a system-assigned
managed identity. The identity is project-owned and is the future authentication
boundary for Foundry model backends; no backend key is stored in the repository.
Basic v2 is the least expensive v2 tier with an SLA and is already included in
the [cost model](costs.md).

The API Management diagnostic setting sends the `GatewayLogs` category to the
project's Log Analytics workspace with the destination type set to `Dedicated`.
That combination produces the resource-specific
[`ApiManagementGatewayLogs`](https://learn.microsoft.com/azure/azure-monitor/reference/tables/apimanagementgatewaylogs)
table instead of the legacy `AzureDiagnostics` table. Request and response
bodies are not enabled by this diagnostic setting.

The request path and the external telemetry path remain separate. API
Management governs traffic before it reaches a model. External telemetry can be
correlated in Log Analytics, but it does not bypass the gateway or become a
backend dependency.

## Project enrollment and token governance

Each existing project has its own HTTPS API path, API Management subscription,
and Foundry `ApiManagement` connection. The connection key is generated and
passed between Azure resources only during deployment; it is never an output or
repository value. Reapplying `infra/gateway-association.bicep` reconciles the
same resources, so enrollment is idempotent.

| Project | Tokens/minute | Monthly token quota |
| --- | ---: | ---: |
| Travel | 20,000 | 2,000,000 |
| Support | 10,000 | 1,000,000 |
| Research | 10,000 | 1,000,000 |
| Platform | 5,000 | 500,000 |

These are safe demonstration defaults in `config/gateway.yaml`, not service
maximums. A configuration-only deployment adjusts them without changing Python
or Bicep. The `llm-token-limit` policy returns **429 Too Many Requests** for a
TPM overrun and **403 Forbidden** for a total-quota overrun.

New projects must be added to the same configuration before they receive an
independent route and counter. Existing gateway connections are discoverable
through the Foundry account, but sharing a connection is not a substitute for a
project-specific limit.

## Custom-agent readiness

`infra/policies/custom-agent.xml` is the route policy template for a future
registered custom agent. It requires:

- an APIM subscription key at the client-facing route;
- an Entra-only backend audience supplied as
  `CUSTOM_AGENT_ENTRA_RESOURCE`;
- API Management managed-identity authentication to that backend; and
- the same TPM and total-token quota controls.

The future backend must reject direct anonymous or key-based access before the
route is enabled. This branch intentionally does not deploy a custom agent or
claim that a not-yet-created backend is restricted.

## Logs and KQL

After a safe request reaches the gateway, operators can find it without reading
request or response bodies:

```kusto
ApiManagementGatewayLogs
| where TimeGenerated > ago(1h)
| project TimeGenerated, ApiId, OperationId, ResponseCode, TotalTime,
    BackendResponseCode, CorrelationId
| order by TimeGenerated desc
```

Quota responses can be isolated with:

```kusto
ApiManagementGatewayLogs
| where TimeGenerated > ago(1h)
| where ResponseCode in (403, 429)
| summarize Requests = count() by ResponseCode, ApiId, bin(TimeGenerated, 5m)
```

## Deployment boundary

The gateway, its identity, and its diagnostic setting are deployed only inside
the project resource group. The diagnostic destination is the shared,
project-owned workspace from the [telemetry spine](telemetry-spine.md). No
subscription-wide identity or role assignment is created.

## Current validation limits

The gateway control plane, diagnostic destination, project enrollments, and
stored limit policies can be verified without a model deployment:

```bash
foundry gateway verify
```

The Travel model was not present when this layer was deployed. Therefore live
429 and 403 responses are a **post-Travel acceptance gate**, not a claimed
result. After Travel attaches `contoso-agents-guardrails` to its model deployment
and publishes `gpt-5.4-mini`, send low-cost requests through the Travel APIM URL,
first exceeding TPM and then total quota, and retain the 429/403 responses under
`internal/`. Direct calls to the Foundry backend must not be used as gateway
evidence.

## Sources

- [Configure AI Gateway in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/configuration/enable-ai-api-management-gateway-portal)
- [API Management v2 tiers](https://learn.microsoft.com/azure/api-management/v2-service-tiers-overview)
- [Monitor API Management](https://learn.microsoft.com/azure/api-management/api-management-howto-use-azure-monitor)
- [`ApiManagementGatewayLogs` schema](https://learn.microsoft.com/azure/azure-monitor/reference/tables/apimanagementgatewaylogs)
- [Limit LLM token usage](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy)
- [Import a Microsoft Foundry API](https://learn.microsoft.com/azure/api-management/azure-ai-foundry-api)
