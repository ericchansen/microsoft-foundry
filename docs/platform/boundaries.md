# Ownership boundary

`foundry boundary` validates the platform deployment declared in
`config/boundary.yaml` before provisioning.

The live RBAC comparison uses the principal, role and scope fields returned by
the documented [Azure CLI role-assignment listing](https://learn.microsoft.com/azure/role-based-access-control/role-assignments-list-cli#list-role-assignments-for-a-subscription).

## What is checked

| Check | What it asserts |
| --- | --- |
| `plan:resource-group-name` | The plan targets the expected resource group, and its name is a valid Azure resource-group name. |
| `plan:relative-scopes` | Every scope is **relative** to the resource group. |
| `plan:no-reuse` | No identity is marked for reuse; every identity is created inside the boundary. |
| `plan:role-assignment-scopes` | No role assignment is scoped at subscription or management-group level. |
| `plan:teardown-completeness` | Every declared resource is covered by a teardown target. |
| `live:target-ownership-tags` | An existing resource group carries every project ownership tag declared by the plan. |
| `live:declared-resource-inventory` | Required declarations match their declared counts, and each live resource resolves to exactly one declaration. Undeclared resources, unexpected counts, and overlapping declarations fail. Explicitly optional entries may be absent. |
| `live:declared-role-assignments` | Live assignments match declared principal, role and resource-relative scope; missing required assignments and unexpected assignments fail. |
| `live:protected-resource-groups` | Lists resource groups outside the deployment. |

Scopes are relative to the deployment resource group. The subscription is
supplied at runtime rather than stored in tracked configuration.

## What lives outside the boundary

Two things the platform depends on are **not** in Azure and cannot be removed by
deleting the resource group:

- A dedicated **Power Platform environment**.
- The **Copilot Studio concierge agent** and any tenant-wide publication of it.

Their teardown inventory is recorded in `config/boundary.yaml`.

## Current state

Travel names its current backend bundle and the optional preceding rollback
bundle separately. Keeping the preceding bundle does not permit an arbitrary
third release. Promotion and rollback additionally verify that both required
images, connections, identities, and scoped permissions still exist.

The live result is deliberately generated rather than asserted in this page.
`foundry boundary` inventories the current resource group and role assignments on
each run; identifier-bearing evidence belongs in ignored `internal/`, never in
the public documentation.

## Re-running it

```bash
foundry boundary              # plan checks plus live subscription checks
foundry boundary --no-live    # plan checks only; no Azure login required
```
