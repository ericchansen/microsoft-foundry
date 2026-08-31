# Contoso Field

Contoso Field is a production-representative
[Pydantic AI](https://ai.pydantic.dev/) external agent for field engineers. It
runs in Azure Container Apps and keeps the Microsoft Foundry external-agent
registration separate from the runtime: Foundry stores registration metadata
and matches telemetry, but it does not host, proxy or invoke the agent. External
agents and their registration experience are **public preview**. See the
[current registration documentation](https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent)
before relying on the preview contract.

## What the agent can know

The runtime rebuilds the deterministic [Contoso data spine](../data/overview.md)
when a replica starts. Its Pydantic AI tools are thin adapters over the shared
[Toolbox contracts](../data/toolbox.md), not a second data-access layer:

- work orders and locations come from the `operations` contract;
- customers come from the `customer` contract; and
- products come from the `catalog` contract.

The synthetic APAC field-engineer principal is resolved server-side from the
canonical `oid` and `tid` fixture keys. Prompts and tool arguments cannot select
another principal, role or region. The agent is read-only: it can inspect and
summarize work, but cannot dispatch, close or modify a work order.

Golden scenarios pin a linked work order, customer, product and location so
changes in model glue cannot silently drift away from the same canonical rows
used by every other agent.

## Container Apps boundary

The live deployment creates only resources declared inside
`rg-contoso-agents`:

- a dedicated user-assigned managed identity;
- one Global Standard GPT-4.1 mini deployment;
- one Container Apps environment; and
- one internal-ingress Container App.

The app uses managed identity for Foundry inference, Azure Monitor ingestion and
private registry pulls. No API key, registry password, token or resolved Azure
identifier is embedded in source or a manifest. The Application Insights
connection string is read from the existing project-owned component during
deployment and authenticated with Microsoft Entra ID. The runtime has no public
endpoint: Container Apps ingress is internal, TLS-only and scales from zero to
one replica. Microsoft documents the
[managed identity](https://learn.microsoft.com/azure/container-apps/managed-identity),
[security](https://learn.microsoft.com/azure/container-apps/security) and
[scale-to-zero](https://learn.microsoft.com/azure/container-apps/scale-app)
behaviours separately; it does not document a special identity guarantee for a
cold start, so the live smoke test reacquires a token rather than assuming it.
The deployment accepts an ACR repository path plus a 64-character digest and
constructs the immutable `registry/repository@sha256:digest` reference itself;
it cannot accept a mutable tag or an arbitrary registry image string.

## Telemetry identity before registration

The Azure Monitor distro exports Pydantic AI's OpenTelemetry spans into the
already-deployed shared Application Insights component. Prompt and completion
content capture is disabled, and `OTEL_SERVICE_NAME` is fixed to
`contoso-field`. Configuration follows the
[Azure Monitor OpenTelemetry guidance](https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-configuration?tabs=python).

Foundry matches an external registration to
`gen_ai.agent.id`. Registration is therefore evidence-gated:

1. Deploy without project enrichment and run the golden smoke scenario.
2. Query the actual Pydantic AI spans in Application Insights.
3. If, and only if, those spans omit `gen_ai.agent.id`, enable
   `MissingAgentIdSpanProcessor`.
4. Record the smoke correlation ID, UTC start time and Container App revision.
5. Query only the operation rooted at that correlated smoke span and require every
   observed Pydantic AI span to carry exactly `contoso-field-v1`.
6. Run the registration command in its default verification-only mode.
7. Create the external registration with `AIProjectClient(allow_preview=True)`
   only after adding `--register --confirm-registration contoso-field`.
8. Read the registration back through the SDK and require the name, agent ID,
   version ID, version and OpenTelemetry ID to be present and exact.

`MissingAgentIdSpanProcessor` is project glue, not a Pydantic AI feature. It
touches only spans whose instrumentation scope is `pydantic-ai`, preserves an ID
already supplied by the framework, and exists solely to bridge the current
preview matching contract. The live result is recorded below after deployment.

## Cost

The estimate assigns Field a bounded monthly input/output token workload. The
estate-wide Container Apps aggregate already includes compute, memory, and
requests, so Field does not add a duplicate always-on line. `foundry costs`
obtains both token meters from the
[Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices);
the resolved prices are operational evidence, not constants in decision logic.

## Alternative AKS hosting

`agents/field/deploy/aks/` contains linted `Deployment`, `ClusterIP` `Service`,
Workload Identity `ServiceAccount` and Azure Key Vault
`SecretProviderClass` artifacts. They demonstrate the
[Workload Identity](https://learn.microsoft.com/azure/aks/workload-identity-overview)
and [CSI identity](https://learn.microsoft.com/azure/aks/csi-secrets-store-identity-access)
shape. Operators supply environment-specific image and tenant values through a
private overlay; the repository deliberately provides no apply command. The
reference hosting model remains Azure Container Apps. The AKS pod reads
non-secret endpoint configuration from the overlay and the Azure Monitor
connection string from a read-only Key Vault CSI JSON file. Its read-only root
filesystem has one writable `emptyDir` for deterministically generated
`FIELD_DATA_DIR` content.

## Unsupported preview capabilities

Microsoft currently lists three explicit limitations for external agents:

- **Human evaluation is unsupported.**
- **Converting traces to an evaluation dataset is unsupported.**
- **AI red teaming cannot target an external agent.**

These are not backlog claims or inferred gaps; they are the
[current external-agent limitations](https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent#current-limitations)
and must be rechecked before deployment.

## Registration evidence contract

Agent-ID enrichment is disabled by default. Operators may enable the narrowly
scoped `MissingAgentIdSpanProcessor` only after a correlated smoke run proves
that native Pydantic AI spans omit `gen_ai.agent.id`.

Registration never accepts a historical observation. The command first requires
an exact clean live ownership inventory, then queries spans newer than the
supplied UTC start time for one correlation ID and Container App revision. Every
span in that operation must carry exactly `contoso-field-v1`; only then does the
explicit write-authorized SDK call create a version. Missing, stale, mixed, or
uncorrelated evidence fails without mutation. The default command is read-only
even after all gates pass. Registration requires both `--register` and the exact
agent-name confirmation; the command then reads the new registration back and
fails if its identity or `otel_agent_id` differs.

## Presenter runbook

Do not expose ingress or paste resolved endpoints, resource IDs, subscription
IDs, connection strings or agent IDs into presenter output. Set
`FOUNDRY_PROJECT_ENDPOINT` from the platform project's **Overview** page without
echoing it.

First require the exact live boundary and confirm that no resource-group
deployment is active:

```powershell
$env:PYTHONPATH = Join-Path $PWD "src"
python -m contoso_foundry.cli boundary --enable-module optional-control-plane
$active = az deployment group list `
  --resource-group rg-contoso-agents `
  --query "[?properties.provisioningState=='Running' || properties.provisioningState=='Accepted'].name" `
  -o tsv
if ($active) { throw "A resource-group deployment is active." }
```

Stop if either check fails. A failed boundary is not permission to weaken the
gate. With a healthy replica, generate the correlation values immediately before
the canonical scenario and keep the JSON output in ignored `internal/`:

```powershell
$correlation = [guid]::NewGuid().ToString()
$startedAt = (Get-Date).ToUniversalTime().ToString("o")
$smokeRevision = az containerapp show `
  --name contoso-field `
  --resource-group rg-contoso-agents `
  --query properties.latestReadyRevisionName `
  -o tsv
az containerapp exec `
  --name contoso-field `
  --resource-group rg-contoso-agents `
  --revision $smokeRevision `
  --command "python -m contoso_foundry.field.smoke --correlation-id $correlation" `
  | Set-Content -Encoding utf8 internal\field-smoke.json
```

An internal app at zero replicas has no exec target. Only after the live boundary
passes, use this bounded alternative. It captures the new revision created by
the scale change, restores the exact prior minimum in a `finally` block, and
waits for scale-to-zero:

```powershell
$previousMin = [int](az containerapp show `
  --name contoso-field `
  --resource-group rg-contoso-agents `
  --query properties.template.scale.minReplicas `
  -o tsv)
$previousRevision = az containerapp show `
  --name contoso-field `
  --resource-group rg-contoso-agents `
  --query properties.latestReadyRevisionName `
  -o tsv
if ($previousMin -ne 0) { throw "Expected the recorded minimum to be zero." }
try {
  az containerapp update `
    --name contoso-field `
    --resource-group rg-contoso-agents `
    --min-replicas 1 | Out-Null
  $readyDeadline = (Get-Date).AddMinutes(10)
  do {
    Start-Sleep -Seconds 5
    $smokeRevision = az containerapp show `
      --name contoso-field `
      --resource-group rg-contoso-agents `
      --query properties.latestReadyRevisionName `
      -o tsv
  } while (
    (-not $smokeRevision -or $smokeRevision -eq $previousRevision) `
      -and (Get-Date) -lt $readyDeadline
  )
  if (-not $smokeRevision -or $smokeRevision -eq $previousRevision) {
    throw "The temporary revision did not become ready."
  }

  $correlation = [guid]::NewGuid().ToString()
  $startedAt = (Get-Date).ToUniversalTime().ToString("o")
  az containerapp exec `
    --name contoso-field `
    --resource-group rg-contoso-agents `
    --revision $smokeRevision `
    --command "python -m contoso_foundry.field.smoke --correlation-id $correlation" `
    | Set-Content -Encoding utf8 internal\field-smoke.json
}
finally {
  az containerapp update `
    --name contoso-field `
    --resource-group rg-contoso-agents `
    --min-replicas $previousMin | Out-Null
}

$deadline = (Get-Date).AddMinutes(10)
do {
  Start-Sleep -Seconds 10
  $replicaCount = @(
    az containerapp replica list `
      --name contoso-field `
      --resource-group rg-contoso-agents `
      -o json | ConvertFrom-Json
  ).Count
} while ($replicaCount -ne 0 -and (Get-Date) -lt $deadline)
if ($replicaCount -ne 0) { throw "contoso-field did not scale back to zero." }
```

Public ingress is never an alternative.

Verify fresh telemetry without registering:

```powershell
python -m contoso_foundry.field.register `
  --project-endpoint $env:FOUNDRY_PROJECT_ENDPOINT `
  --confirm-resource-group rg-contoso-agents `
  --enable-module optional-control-plane `
  --smoke-correlation-id $correlation `
  --smoke-started-at $startedAt `
  --container-app-revision $smokeRevision
```

The safe success shape reports a nonzero live span count, the exact
`gen_ai.agent.id=contoso-field-v1`, and `registration not requested`. Only after
reviewing that result, repeat the command with:

```text
--register --confirm-registration contoso-field
```

The registration success shape includes the read-back version but omits Azure
resource and version IDs from console output. In the Foundry portal, open the
platform project, select **Agents**, select **contoso-field**, then select
**Traces**. Filter by the fresh smoke time and confirm the matching trace.
