# Control Plane platform coverage

This runbook deploys and verifies Contoso SRE and Contoso Approvals without
crossing the `rg-contoso-agents` ownership boundary.

## Before deployment

Use the branch-local environment and run the static contracts first:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\foundry.exe boundary --no-live
.\.venv\Scripts\foundry.exe platform-inventory --no-live
az bicep lint --file infra\main.bicep
az bicep build --file infra\main.bicep --stdout | Out-Null
```

Confirm the owned resource group has no active deployment:

```powershell
az deployment group list `
  --resource-group rg-contoso-agents `
  --query "[?properties.provisioningState=='Running' || properties.provisioningState=='Accepted'].name" `
  --output tsv
```

Any output is a stop condition. Wait for the other deployment to finish rather
than racing it. The infrastructure workflow also uses one resource-group-wide
concurrency key and repeats this check after Azure login.

## What-if and deploy

Run a resource-group what-if before the incremental deployment:

```powershell
az deployment group what-if `
  --resource-group rg-contoso-agents `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam

az deployment group create `
  --name control-plane-platforms `
  --resource-group rg-contoso-agents `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam
```

Do not use complete mode. Do not change the resource group name or add an
absolute role-assignment scope.

## Machine verification

```powershell
.\.venv\Scripts\foundry.exe platform-inventory
```

The command queries each expected resource through Azure Resource Manager and
fails unless:

- both exact resource names and types exist;
- ownership tags and dedicated identities match;
- Contoso SRE's returned Application Insights application ID matches the shared
  component, and it uses the owned resource-group scope and review-only access;
- Contoso Approvals has an `Agent` action and a bounded synthetic tool. The
  [`Microsoft.Logic/workflows@2019-05-01` resource API](https://learn.microsoft.com/azure/templates/microsoft.logic/workflows)
  does not expose a separate agentic workflow kind, so inventory verification
  uses the workflow definition rather than an unsupported resource property.

The full JSON evidence is written under `internal/`; the sanitized summary is
written under `reports/`. Neither directory is published or committed.

The Azure Resource Manager check proves the resources are eligible for automatic
Control Plane discovery. The current public
[Control Plane documentation](https://learn.microsoft.com/azure/foundry/control-plane/how-to-manage-agents)
does not expose a documented inventory REST API. Final UI verification therefore
uses **Operate > Assets > Agents** and records any permission or discovery gap as
exact unpublished evidence rather than treating an ARM resource as proof that a
portal row rendered.

## Synthetic approval test

Retrieve the HTTP trigger callback URL without storing or publishing it, then
send only a fictional scenario:

```json
{
  "scenario": "Synthetic request to raise a fictional service limit",
  "synthetic": true
}
```

A successful response says that the synthetic agent loop completed and no
change was executed. Its result has `requiresHumanApproval: true` and
`synthetic: true`. Delete any local file that contains the callback URL after
the test.
