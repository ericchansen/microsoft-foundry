# Contoso Approvals

> **Status:** **public preview.** Consumption autonomous agentic workflows are
> covered by the [Azure preview supplemental
> terms](https://azure.microsoft.com/support/legal/preview-supplemental-terms/).

Contoso Approvals is a Consumption Logic Apps agent loop that classifies
synthetic approval scenarios. It uses the `Agent` workflow action with the
region-provided `gpt-4o-mini` model described in the
[autonomous workflow guide](https://learn.microsoft.com/azure/logic-apps/create-autonomous-agent-workflows).

## Deliberately harmless tools

The loop has one tool: build a recommendation containing an outcome, rationale,
and `requiresHumanApproval: true`. The tool is a Logic Apps `Compose` action. It
has no HTTP call, connector, Azure mutation, or secret. The agent loop is capped
at five iterations and five minutes.

The request contract requires both a scenario and a `synthetic` marker. The
workflow always creates a separate synthetic human-review envelope after the
agent loop and returns it with HTTP 202. The envelope cannot approve or mutate
anything; it proves that the demonstration ends at a human decision point.

## Identity and telemetry

The workflow has its own user-assigned managed identity. It receives no Azure
RBAC role because the synthetic `Compose` tool needs no resource access. Zero
data-plane permissions are the least-privilege grant for this scenario.

Logic Apps platform diagnostics go to the Log Analytics workspace that backs the
shared Application Insights component. These are workflow runtime logs and
Azure metrics, not agent-loop traces.

!!! warning "Control Plane observability is unsupported"
    Foundry Control Plane automatically discovers Logic Apps resources that
    contain agent loops and supports start and stop lifecycle operations.
    However, the current [Control Plane platform support
    documentation](https://learn.microsoft.com/azure/foundry/control-plane/how-to-manage-agents#azure-logic-apps-agent-loop)
    explicitly says that traces and metrics are not supported for Logic Apps
    agent loops. The deployment does not fabricate, translate, or label platform
    diagnostics as agent observability.

## Cost treatment

Input tokens, output tokens, and connector actions remain separate live-priced
entries in the [cost model](../platform/costs.md). The safe scenario has no
managed connector action, but the connector reserve remains for later bounded
tools.
