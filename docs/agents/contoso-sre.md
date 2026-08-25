# Contoso SRE

> **Status:** deployed by the current GA `Microsoft.App/agents@2026-01-01`
> resource API. The agent is isolated to the project-owned resource group and
> starts in review-only mode.

Contoso SRE is a new Azure SRE Agent created for this platform. It does not
reuse, clone, or modify another SRE Agent. The Bicep deployment gives it a
distinct name, ownership tags, and a dedicated user-assigned managed identity.
[Microsoft's SRE Agent IaC guidance](https://learn.microsoft.com/azure/sre-agent/deploy-iac)
identifies the agent, managed identity, telemetry, and RBAC assignments as the
ARM-managed deployment layer.

## Safety boundary

The agent can inspect only `rg-contoso-agents`, the same resource group that
contains it. Its user-assigned and system-assigned identities receive:

- Reader on the owned resource group;
- Monitoring Reader on the owned resource group;
- Log Analytics Reader on the shared Application Insights component.

No Contributor role is assigned. The action configuration is `Low` access with
`Review` mode, so the platform cannot silently turn an investigation into a
change. This follows the [SRE Agent approval
model](https://learn.microsoft.com/azure/sre-agent/overview), where proposed
changes require human sign-off.

Members of one deployment-supplied Microsoft Entra security group receive
[SRE Agent Standard User](https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/management-and-governance#sre-agent-standard-user)
on this agent resource only. That role permits incident triage and diagnostics;
the group receives neither SRE Agent Administrator nor a resource-group role.
The group object ID comes from `SRE_OPERATOR_GROUP_OBJECT_ID` at deployment time
and is never stored in tracked configuration.

The deployment uses East US 2 because it is one of the currently documented
[SRE Agent regions](https://learn.microsoft.com/azure/sre-agent/supported-regions).
The managed workload remains the project-owned resource group; a resource does
not have to share the agent's region to be inspected.

## Telemetry and Control Plane

Contoso SRE connects directly to the shared workspace-based Application
Insights component. Its resource and telemetry tags use the distinct service
name `contoso-sre-control-plane`.

[Foundry Control Plane automatically discovers Azure SRE Agent
resources](https://learn.microsoft.com/azure/foundry/control-plane/how-to-manage-agents#azure-sre-agent).
The same Control Plane documentation lists start and stop as supported lifecycle
operations. SRE Agent telemetry is eligible for Control Plane metrics and traces
because the agent is explicitly connected to Application Insights.

## Safe synthetic exercise

The first exercise is advisory: ask the agent to review a synthetic alert trend
and explain which additional signals it would inspect. Do not provide a live
incident, invoke a remediation, or approve a proposed action. The exercise
proves identity, telemetry, and inventory coverage without changing a resource.

## Cost treatment

SRE Agent remains an unpublished-price line in the
[cost model](../platform/costs.md). Microsoft documents Azure Agent Unit
consumption, but no matching USD Retail Prices API meter exists. The project
therefore preserves its monthly reserve rather than inventing a price or
recording zero.
