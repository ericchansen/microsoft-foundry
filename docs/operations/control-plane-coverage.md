# Control Plane platform coverage

This runbook deploys and verifies the optional Contoso SRE and Contoso Approvals
coverage without crossing the `rg-contoso-agents` ownership boundary. Both
modules are disabled by default; the core platform has no dependency on them.

## Before deployment

Use the branch-local environment and run the static contracts first:

```powershell
$env:PYTHONPATH = "src"
python -m contoso_foundry.cli boundary --no-live
python -m contoso_foundry.cli platform-inventory --no-live
az bicep lint --file infra\main.bicep
az bicep build --file infra\main.bicep --stdout | Out-Null
```

Set `SRE_OPERATOR_GROUP_OBJECT_ID` only when enabling the optional coverage. It
must identify the dedicated Microsoft
Entra security group whose members operate Contoso SRE. The value is a required,
secure deployment parameter and must not be added to a tracked file.

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

Price the exact optional combination before what-if or deployment:

```powershell
python -m contoso_foundry.cli costs --enable-module approvals --enable-module sre
```

The command enforces the environment's configured cost policy. If the optional
combination exceeds that policy, either enable the modules independently or have an
operator deliberately change the environment-specific ceiling; do not bypass the
gate or lower workload assumptions solely to make it pass.

## What-if and deploy

Run a resource-group what-if before the incremental deployment:

```powershell
az deployment group what-if `
  --resource-group rg-contoso-agents `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam `
  --parameters deploySreAgent=true deployApprovalsWorkflow=true `
  --parameters sreOperatorGroupObjectId=$env:SRE_OPERATOR_GROUP_OBJECT_ID

az deployment group create `
  --name control-plane-platforms `
  --resource-group rg-contoso-agents `
  --template-file infra\main.bicep `
  --parameters infra\main.bicepparam `
  --parameters deploySreAgent=true deployApprovalsWorkflow=true `
  --parameters sreOperatorGroupObjectId=$env:SRE_OPERATOR_GROUP_OBJECT_ID
```

Do not use complete mode. Do not change the resource group name or add an
absolute role-assignment scope.

## Machine verification

```powershell
python -m contoso_foundry.cli platform-inventory --include-optional
```

The command queries each expected resource through Azure Resource Manager and
fails unless:

- both exact resource names and types exist;
- ownership tags and dedicated identities match;
- Contoso SRE has exactly one UAMI for actions and knowledge, exactly one managed
  resource group, exact resource-scoped identity RBAC, an agent-scoped Standard
  User operator group, shared Application Insights, and review-only access;
- Contoso Approvals has an exact recursively allowlisted action graph, rejects
  any value other than `synthetic: true` before execution, and preserves that
  marker in its human-review result. The
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

Run this only in a private terminal. The callback URL is a secret: the sequence
keeps it in memory, never prints or writes it, and removes it in `finally`.
The false case is accepted by the HTTP trigger with status 202 but must create
no workflow run because its trigger condition fails. The true case must return
only the fictional human-review envelope.

```powershell
$workflowId = az resource show `
  --resource-group rg-contoso-agents `
  --resource-type Microsoft.Logic/workflows `
  --name contoso-agents-approvals-loop --query id --output tsv
$trigger = "Receive_synthetic_approval_scenario"
$runsUrl = "https://management.azure.com$workflowId/runs?api-version=2016-06-01"
$callback = az rest --method post `
  --url "https://management.azure.com$workflowId/triggers/$trigger/listCallbackUrl?api-version=2016-06-01" `
  --query value --output tsv
if (-not $callback) { throw "The private callback URL was not resolved." }

function Get-WorkflowRuns {
  param([string]$Url)
  $runs = @()
  while ($Url) {
    $page = az rest --method get --url $Url | ConvertFrom-Json
    $runs += @($page.value)
    $Url = $page.nextLink
  }
  return $runs
}

try {
  $before = Get-WorkflowRuns -Url $runsUrl |
    Sort-Object startTime -Descending |
    Select-Object -First 1 -ExpandProperty name
  $falseResponse = Invoke-WebRequest -Method Post -Uri $callback `
    -ContentType "application/json" -SkipHttpErrorCheck `
    -Body (@{
      scenario = "Synthetic request that must not start the agent"
      synthetic = $false
    } | ConvertTo-Json -Compress)
  if ($falseResponse.StatusCode -ne 202) {
    throw "The false synthetic trigger did not return transport status 202."
  }
  Start-Sleep -Seconds 10
  $afterFalse = Get-WorkflowRuns -Url $runsUrl |
    Sort-Object startTime -Descending |
    Select-Object -First 1 -ExpandProperty name
  if ($afterFalse -ne $before) {
    throw "synthetic=false created a workflow run."
  }

  $trueResponse = Invoke-RestMethod -Method Post -Uri $callback `
    -ContentType "application/json" `
    -Body (@{
      scenario = "Synthetic request to raise a fictional service limit"
      synthetic = $true
    } | ConvertTo-Json -Compress)
  if (
    $trueResponse.message -ne "Synthetic agent loop completed; no change was executed." -or
    $trueResponse.result.requiresHumanApproval -ne $true -or
    $trueResponse.result.synthetic -ne $true -or
    $trueResponse.result.agentLoopCompleted -ne $true
  ) {
    throw "The synthetic approval response was not the bounded review envelope."
  }
}
finally {
  Remove-Variable callback -ErrorAction SilentlyContinue
  Remove-Variable trueResponse -ErrorAction SilentlyContinue
  Remove-Variable falseResponse -ErrorAction SilentlyContinue
}
```

Do not echo `$callback`, enable shell tracing, capture this terminal, or replace
the fictional scenario with customer data.
