# Contoso Concierge teardown

Deleting the Azure resource group does not remove anything in Power Platform,
Copilot Studio, Microsoft 365, or the tenant's Copilot Credits pool. Teardown
therefore follows an explicit dependency order and records sanitized completion
evidence without publishing tenant identifiers.

!!! warning "Run only with authorized tenant administration"

    This is a runbook, not an automation claim. The current read-only operator
    cannot perform these steps, and no live teardown was attempted.

## Ordered runbook

1. **Stop new sessions.** Remove every deployed channel, unpublish the agent,
   remove Microsoft 365 and Teams availability, and remove any tenant or user
   pin. Confirm the agent is no longer discoverable before deleting its
   definition.
2. **Disable dependent automation.** Turn off agent flows and any pipeline
   extensions that can invoke or redeploy the agent.
3. **Remove runtime bindings.** Delete the target connector connections, then the
   `ccs_FoundrySpecialist` connection-reference binding and custom connector.
   Do not delete a shared connection until its inventory proves no other
   solution uses it.
4. **Stop telemetry collection.** Disable PROD agent-level Application Insights,
   remove DEV/TEST environment-level export, and confirm Dataverse transcript
   saving is off. Remove any remaining transcript records according to the
   tenant's retention policy.
5. **Revoke deployment access.** Remove the deployment application's environment
   security roles in DEV, TEST, and PROD. Deactivate and then delete its Power
   Platform application users after reassigning any owned records. Revoke every
   client secret and federated credential used by the deployment workflow, then
   delete the service principal/app registration only after proving no other
   workload uses it.
6. **Release capacity.** Remove or reassign environment-specific Copilot Credit
   allocations only after channels and flows are stopped. Record the resulting
   capacity state through an authorized source.
7. **Remove solutions.** Uninstall the managed solution from PROD and then TEST.
   Remove the unmanaged solution from DEV last. Resolve dependencies rather than
   forcing deletion.
8. **Delete environments.** Delete the dedicated PROD, TEST, and DEV environments
   only after confirming they contain no unrelated assets.
9. **Retire GitHub deployment configuration.** Delete Concierge secrets from
   `concierge-dev`, `concierge-test`, `concierge-test-acceptance`, and
   `concierge-prod-approval`. Delete those GitHub environments only after retained
   deployment evidence no longer depends on their protection history.
10. **Retain release evidence.** Keep the accepted managed ZIP, archive digest,
   attestations, exact source SHA, TEST acceptance, approvals, and direct-import
   workflow history for the declared retention period. Store any
   identifier-bearing evidence under the unpublished `internal/` convention,
   never in docs or git.

The shared Application Insights component belongs to the Azure project boundary,
not solely to Concierge. Concierge teardown removes its settings and exports but
does not delete that shared component. Full Azure teardown remains the single
resource-group operation documented on the [ownership boundary
page](../platform/boundaries.md).

## Completion inventory

The machine-checked inventory in `config/concierge/alm.yaml` covers channels,
pins, connections, connection references, custom connectors, flows, agent and
environment telemetry, transcript settings, deployment service principal,
Power Platform application users, client secrets, federated credentials,
environment roles, GitHub environment secrets and deployment environments,
managed solutions, the unmanaged DEV solution, capacity allocations, and
environments. A teardown is incomplete while any inventory item lacks an
authorized completion record.

## First-party guidance

- [Connect and configure an agent for Teams and Microsoft 365](https://learn.microsoft.com/microsoft-copilot-studio/publication-add-bot-to-microsoft-teams)
- [Delete Copilot Studio agents with the Power Platform API](https://learn.microsoft.com/microsoft-copilot-studio/admin-api-delete)
- [Control transcript access and retention](https://learn.microsoft.com/microsoft-copilot-studio/admin-transcript-controls)
- [Manage Copilot Credits allocations](https://learn.microsoft.com/power-platform/admin/programmability-tutorial-manage-copilot-credit-allocations)
- [Manage Power Platform application users and roles](https://learn.microsoft.com/power-platform/admin/manage-application-users)
- [Remove unused Microsoft Entra application credentials](https://learn.microsoft.com/entra/identity/monitoring-health/recommendation-remove-unused-credential-from-apps)
