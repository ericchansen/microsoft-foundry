# Operate an enterprise agent estate

Contoso has agents built with different frameworks and running on different
hosts. This demo asks an operator's questions: **What is running? What went
wrong? Which control applies? Is the proposed change actually better?**

**[Run the twenty-minute demo](demo-guide.md)**

The core story combines a native [Travel agent](agents/travel.md) with an
externally hosted [Pydantic AI Field agent](agents/field.md). The opening screen
is the real Azure Monitor **Application Insights > Agents (Preview)** experience,
not a custom dashboard or an architecture diagram.
[Microsoft's monitoring guide](https://learn.microsoft.com/azure/azure-monitor/app/agents-view)
explains its fleet view, trace search, and end-to-end transaction details.

## The story

| Minutes | Operator question | Evidence |
| --- | --- | --- |
| 0-3 | What is happening across the estate? | Native agent dashboard with real activity and framework diversity |
| 3-8 | Why did this request fail? | Business question, response, model/tool trace, and a specific cause |
| 8-11 | Does observability require rehosting? | Field's deployed runtime and attributed external-agent trace |
| 11-15 | Where is access actually enforced? | A denied and an allowed Gateway request with exact diagnostic correlation |
| 15-20 | Should we release the change? | Comparable baseline/candidate evidence and an explicit release decision |

## What is real, and what is fictional?

The people, business records, policies, and work orders are fictional.
Inference, tool execution, telemetry, and platform enforcement are real.
Travel does not buy tickets or make reservations. Workload history comes from
actual bounded calls, not invented counters or backdated telemetry.

External-agent registration and the Azure Monitor Agents experience are
**public preview**. Registration links telemetry; it does not host, proxy, or
block Field's runtime. A Gateway control affects only traffic using its
enrolled route. See [external-agent registration](https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent)
and [Gateway-backed custom agents](https://learn.microsoft.com/azure/foundry/control-plane/register-custom-agent).

## Go deeper after the demo

| Topic | Start here |
| --- | --- |
| Framework and hosting choices | [Architecture and extension paths](architecture/overview.md) |
| Fictional business evidence and authorization | [Data](data/overview.md) and [Toolbox contracts](data/toolbox.md) |
| Telemetry and enforcement boundaries | [Telemetry spine](platform/telemetry-spine.md) and [AI Gateway](platform/ai-gateway.md) |
| Operating safely in a shared subscription | [Ownership boundary](platform/boundaries.md) and [verification](operations/verification.md) |
| Workload assumptions versus observed usage | [Cost methodology](platform/costs.md) |

Support and Research extend framework coverage. Concierge is an unexported
Copilot Studio ALM scaffold; SRE and Approvals are optional coverage. None is
required to complete the core operator story.
