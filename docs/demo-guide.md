# Demo operator guide

This runbook is for the presenter who needs to demonstrate the Contoso Foundry
platform without memorizing its implementation. The core path is a ten-minute
Contoso Travel story followed by five to seven minutes of Contoso Field.
Everything uses checked-in synthetic data. The normal presentation path is
read-only, including the simulated booking, which makes no purchase or
reservation. Travel deployment and Field registration are separately marked
operator writes and are not routine presentation steps.

> **Presenter rule:** never paste a tenant ID, subscription ID, endpoint,
> connection string, key, token, trace ID, or real personal data into slides,
> chat, source control, or screenshots. Keep identifier-bearing evidence under
> the ignored `internal/` directory.

## Two-minute preflight

Do this before the audience arrives.

1. In a private terminal, sign in and select the intended Azure context:

   ```powershell
   az login
   az account list --query "[].{Name:name,IsDefault:isDefault}" --output table
   az account set --subscription "<intended subscription name or ID>"
   az account show --query "{Name:name,Tenant:tenantDisplayName}" --output table
   ```

   Read the values on screen, but do not record them. Azure CLI documents
   [subscription selection](https://learn.microsoft.com/cli/azure/manage-azure-subscriptions-azure-cli).
   If either value is wrong, stop and correct the login context.

2. Confirm the owned demo boundary exists:

   ```powershell
   az group show --name rg-contoso-agents `
     --query "{Name:name,State:properties.provisioningState}" --output table
   ```

   The expected relative name is `rg-contoso-agents`. Do not substitute another
   resource group or broaden a role assignment.

3. Open [Microsoft Foundry](https://ai.azure.com/). In the project selector,
   choose **View all resources**, open `contoso-agents-foundry`, then select the
   `travel` project.

   The project selector is a recent-project convenience, not the authoritative
   inventory. A project can be healthy and still be absent until it has been
   opened in the current browser profile. Use **View all resources** whenever
   `travel` is not in the recent list.

4. Keep a terminal at the repository root with the local environment ready:

   ```powershell
   Set-Item Env:PYTHONPATH "src;agents\travel\src"
   ```

   Do not set a live endpoint until a CLI invocation is needed. Copy the Travel
   project endpoint from the authenticated project overview into the current
   shell only:

   ```powershell
   Set-Item Env:FOUNDRY_PROJECT_ENDPOINT "<Travel project endpoint>"
   ```

   The endpoint shape and SDK use are documented in the
   [Foundry SDK overview](https://learn.microsoft.com/azure/foundry/how-to/develop/sdk-overview).
   Never write this value to `.env`, a tracked script, or presenter notes.

## Know the stage

All mutable Azure resources belong to `rg-contoso-agents`. Stable relative names
are safe to say aloud; generated suffixes and live identifiers are not.

| Demo asset | Stable relative name | Azure type / purpose |
| --- | --- | --- |
| Ownership boundary | `rg-contoso-agents` | Resource group containing every mutable Azure asset |
| Foundry resource | `contoso-agents-foundry` | Microsoft Foundry account |
| Projects | `travel`, `support`, `research`, `platform` | Four child Foundry projects with separate identity and connections |
| Travel model | `travel-gpt-5-4-mini` | Global Standard `gpt-5.4-mini`, exact version `2026-03-17` |
| Field model | `contoso-field-model` | Global Standard `gpt-4.1-mini`, exact version `2025-04-14` |
| Monitoring | `contoso-agents-insights`, `contoso-agents-logs` | Shared workspace-based Application Insights and Log Analytics |
| AI Gateway | `contoso-agents-gateway` | API Management Basic v2 designated ingress |
| Secure data | `contoso-agents-kv-*`, `contosoagents*` | RBAC-only Key Vault, OAuth-default Storage, and Basic ACR; generated suffixes stay private |
| Core identities | `contoso-agents-runtime`, `contoso-agents-github-deploy` | Runtime and deployment user-assigned managed identities |
| Travel traffic | `contoso-agents-traffic-env`, `contoso-agents-travel-traffic`, `contoso-agents-travel-traffic` identity | Enabled twice-hourly synthetic traffic job; executions and correlated telemetry are the durable run evidence |
| Field runtime | `contoso-field`, `contoso-field-env`, `contoso-field-runtime` | Internal-ingress Container App, Container Apps environment, and runtime identity |
| Routed agents | Travel 5, Support 4, Research 4, Field external 1 | Current immutable versions accepted by live tests |
| Optional SRE | `contoso-agents-sre-control-plane`, `contoso-agents-sre` | **Stopped** until an approved operator group exists |
| Optional Approvals | `contoso-agents-approvals-loop`, `contoso-agents-approvals` | Enabled synthetic-only Logic Apps agent loop with mandatory human review |

The four-project monitoring design is described in
[one telemetry spine](platform/telemetry-spine.md). Microsoft documents
[Foundry projects and RBAC](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry),
[workspace-based Application Insights](https://learn.microsoft.com/azure/azure-monitor/app/create-workspace-resource),
[API Management concepts](https://learn.microsoft.com/azure/api-management/api-management-key-concepts),
[Container Apps managed identity](https://learn.microsoft.com/azure/container-apps/managed-identity),
and [Key Vault RBAC](https://learn.microsoft.com/azure/key-vault/general/rbac-guide).

### Why this platform matters to a frontier firm

A frontier firm will not standardize every agent on one framework or one hosting
model. It needs a control plane that can govern the differences without hiding
them. This demo deliberately combines a native prompt agent, two hosted-agent
frameworks, and an external Container Apps runtime while keeping five contracts
shared:

1. **Govern:** immutable agent/model versions, exact identity scope, approved
   model families, responsible-AI blocking, and one explicit Azure mutation
   boundary make change reviewable.
2. **Monitor:** one OpenTelemetry and Application Insights spine correlates
   native, hosted, external, model, and tool activity without pretending every
   runtime is the same.
3. **Evaluate:** fixed goldens gate a candidate, a controlled bad sample proves
   the gate fails closed, and recurring evaluation watches production-shaped
   synthetic traffic after promotion.
4. **Control:** Gateway quotas separate per-project rate and total-token policy,
   while the synthetic-only Approvals loop demonstrates bounded autonomy with a
   mandatory human-review envelope.
5. **Optimize:** scale-to-zero external compute, bounded synthetic traffic,
   explicit model deployment capacity, and a fail-closed cost gate turn
   optimization into a lifecycle control rather than an after-the-fact report.

Microsoft's [Foundry observability
overview](https://learn.microsoft.com/azure/foundry/observability/overview),
[agent evaluation
guidance](https://learn.microsoft.com/azure/foundry/observability/concept-agent-evaluation),
and [API Management AI Gateway
capabilities](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities)
describe the product surfaces behind this operating model.

## Primary demo, part 1: Contoso Travel in ten minutes

### The scenario and persona

Say:

> “I am the synthetic **EMEA Travel Coordinator**. I need to find a route from
> Contoso Seattle Headquarters to Contoso Chicago Distribution, check the
> regional policy, inspect the published fares, and simulate a trip without
> purchasing or reserving anything.”

The exact fixture persona is `emea-travel-coordinator`. The service resolves it
on the server; it is not a prompt parameter. The prompt agent follows the
[versioned Foundry prompt-agent pattern](https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent).

### Minute 0-2: establish the immutable candidate

In the `travel` project, choose **Build** > **Agents**, open the
`contoso-travel` prompt agent, and use its chat surface. Before sending a prompt,
point out:

- agent name: `contoso-travel`;
- model deployment: `travel-gpt-5-4-mini`;
- model family and exact version: `gpt-5.4-mini`, `2026-03-17`;
- deployment setting: `NoAutoUpgrade`; and
- the invoked agent version is an explicit immutable version, never “first
  agent” or a fallback candidate.

Say:

> “The code creates an immutable agent version, reads it back, binds the
> definition digest, and invokes that exact version. The model version is also
> exact and does not auto-upgrade.”

### Minute 2-4: route search

Send the checked-in golden prompt:

> Find the synthetic route from LOC-001 (Contoso Seattle Headquarters) to
> LOC-002 (Contoso Chicago Distribution).

Expected tool sequence:

```text
travel_search_routes(
  origin_location_id="LOC-001",
  destination_location_id="LOC-002"
)
```

Expected evidence is `ROUTE-0001` and an answer that labels the result
synthetic. It must not claim anything was booked or purchased.

Say:

> “The model chooses the business operation and filters. It cannot choose the
> caller, tenant, role, or region. Those fields are absent from the tool schema.”

### Minute 4-6: regional policy

Send:

> What synthetic travel policy applies to me?

Expected tool sequence:

```text
travel_get_policy()
```

The server-resolved EMEA persona returns `TPOL-EMEA-01`. Say:

> “The same prompt from another allowed persona can produce a different scoped
> result. Scope is derived before the tool exists, and an unknown principal is
> rejected before any tool call.”

This is application-layer authorization over the deterministic demo database,
not a claim of database-native row-level security. Microsoft documents the
[Foundry Toolbox concept](https://learn.microsoft.com/azure/foundry/agents/concepts/toolbox-overview);
the repository's versioned Toolbox contract supplies the stricter server-scoped
behavior shown here.

### Minute 6-9: fare and no-purchase simulation

Send the exact golden prompt:

> First use travel_search_fares to read the published synthetic fares on
> ROUTE-0001. Then simulate, but do not purchase, the first returned fare for
> 2026-09-15.

Expected sequence and exact arguments:

```text
travel_search_fares(route_id="ROUTE-0001")
travel_simulate_booking(
  route_id="ROUTE-0001",
  fare_id="FARE-00001",
  depart_on="2026-09-15"
)
```

The checked-in deterministic smoke path returns two published fares beginning
with `FARE-00001`; its known fixture result for the first fare is **allowed**
under `TPOL-EMEA-01` at `321.81 USD`. The live model answer must say
**simulation** and **no purchase** and must not say **confirmed booking**.

Say:

> “The booking tool is deliberately a simulation. The agent must search fares
> in the same response and use a returned fare; it cannot guess or reuse an
> identifier.”

### Minute 9-10: close the primary story

Say:

> “One governed platform gives each specialist the same synthetic data, scoped
> Toolbox, managed identity, telemetry, model governance, and ownership
> boundary. Framework and hosting can differ without duplicating those
> contracts.”

Open the [architecture overview](architecture/overview.md) to show how native
Foundry routes, designated Gateway routes, and the external Field runtime remain
distinct.

## Primary demo, part 2: Contoso Field in five to seven minutes

Field is the required second act. It shows that an agent can run outside
Foundry while still reusing the platform's model, identity, Toolbox, synthetic
data, and telemetry contracts. Microsoft Foundry external-agent registration is
**public preview** and links telemetry to a Foundry record; it does not host,
proxy, or invoke the Container App. See Microsoft's
[external-agent registration guide](https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent).

!!! success "Publication readiness evidence: live Field path verified"

    On 2026-08-31 UTC, the current digest-pinned image scaled from zero, ran the
    exact work-order smoke inside one named revision and replica, emitted eight
    correlated spans with `gen_ai.agent.id=contoso-field-v1`, restored zero
    replicas, and created/read back external-agent version 1. Re-run the gates
    below before presenting because this evidence is intentionally point-in-time.

### Minute 0-2: identify the external runtime boundary

In Azure portal, open `rg-contoso-agents` and identify:

- Container App `contoso-field`;
- Container Apps environment `contoso-field-env`;
- model deployment `contoso-field-model`;
- managed identity `contoso-field-runtime`; and
- shared Application Insights `contoso-agents-insights`.

Say:

> “This Pydantic AI agent runs in Container Apps. Ingress is internal and
> TLS-only, the minimum replica count is zero, and the image reference is pinned
> by SHA-256 digest. Managed identity is used for Foundry inference, telemetry,
> and private registry pull.”

Use Overview and the current revision details to show the runtime shape, but do
not reveal generated hostnames, resource IDs, registry endpoints, or identity
IDs. Show only that the image reference ends in `@sha256:<digest>`. Microsoft documents
[Container Apps security](https://learn.microsoft.com/azure/container-apps/security),
[scale-to-zero](https://learn.microsoft.com/azure/container-apps/scale-app), and
[managed identities](https://learn.microsoft.com/azure/container-apps/managed-identity).

### Minute 2-4: run one fresh correlated smoke

The exact golden prompt is:

> Summarize WO-00015, including its customer, product, and site.

The runtime must resolve the synthetic APAC field-engineer persona on the
server. The expected bounded evidence comes from these Toolbox calls:

```text
operations_lookup_work_order(work_order_id="WO-00015")
customer_lookup(customer_id="CUST-00058")
catalog_lookup_product(product_id="PROD-0092")
operations_list_locations(country="Singapore", limit=25)
```

Expected synthetic facts are:

- “Install additional access point in loading bay”;
- “Juniper Reach Industries”;
- “Contoso Compact Cable Tray H301”; and
- “Contoso Singapore Office”.

Run the smoke inside the current Container App revision. If a replica is already
running, use the sequence below. If the app is at its normal zero replicas, use
the complete bounded scale-up, smoke, `finally` restoration, and scale-to-zero
check in the [Field presenter runbook](agents/field.md#presenter-runbook).
Connecting with `az containerapp exec` is the documented
[Container Apps console path](https://learn.microsoft.com/azure/container-apps/container-console).
The command emits a `contoso.field.smoke` span and returns a JSON object with
`correlation_id`, `output`, `revision`, and `started_at`.

Use this exact preflight and smoke sequence:

```powershell
$env:PYTHONPATH = Join-Path $PWD "src"
python -m contoso_foundry.cli boundary --enable-module optional-control-plane

$active = az deployment group list --resource-group rg-contoso-agents `
  --query "[?properties.provisioningState=='Running' || properties.provisioningState=='Accepted'].name" `
  --output tsv
if ($active) { throw "A resource-group deployment is active." }

$correlation = [guid]::NewGuid().ToString()
$startedAt = (Get-Date).ToUniversalTime().ToString("o")
$smokeRevision = az containerapp show --name contoso-field `
  --resource-group rg-contoso-agents `
  --query properties.latestReadyRevisionName --output tsv

az containerapp exec --name contoso-field `
  --resource-group rg-contoso-agents `
  --revision $smokeRevision `
  --command "python -m contoso_foundry.field.smoke --correlation-id $correlation --revision $smokeRevision" |
  Set-Content -Encoding utf8 internal\field-smoke.json
```

The first command is a hard gate. If it reports declared-resource,
role-assignment, or optional-module drift, stop. Do not scale the app or run the
registration command.

The linked zero-replica procedure is part of this demo, not hidden operator
knowledge. It records the original minimum, temporarily sets it to one, waits
for the exact ready revision and replica, runs the smoke, restores the original
minimum in `finally`, and confirms zero replicas. Never make ingress external.

### Minute 4-6: prove telemetry identity

Open `contoso-agents-insights` and filter the fresh operation by:

- `cloud_RoleName = contoso-field`;
- `contoso.field.smoke.correlation_id = <fresh correlation>`;
- `contoso.field.smoke.revision = <current revision>`;
- `gen_ai.agent.name = contoso-field`; and
- `gen_ai.agent.id = contoso-field-v1`.

The registration gate begins at the captured UTC start time, finds exactly one
correlated operation, and requires **every** Pydantic AI span in that operation
to carry exactly `gen_ai.agent.id=contoso-field-v1`. A stale correlation,
mixed revision, missing span, mixed ID, or partial enrichment fails closed.
Prompt and completion content recording remains disabled. Pydantic AI emits
[OpenTelemetry instrumentation](https://ai.pydantic.dev/logfire/), while Azure
Monitor provides the
[Application Insights export](https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-configuration?tabs=python).

Say:

> “The external-agent record and the runtime meet through telemetry identity,
> not through a proxy. Foundry matches this exact `gen_ai.agent.id`; the fresh
> correlation and revision prove which live execution produced the spans.”

### Minute 6-7: show registration only when freshly proven

If a fresh registration gate and SDK readback succeeded before the demo, open the `platform` project,
choose **Build** > **Agents**, select `contoso-field`, then select **Traces**.
Show the external-agent registration and trace readback tied to
`contoso-field-v1`. Microsoft notes that trace ingestion commonly takes a few minutes in the
[external-agent guide](https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent#verify-traces-in-the-foundry-portal).

If the gate did not succeed or the record is not observable, say:

> “The live boundary did not produce all required evidence. I will show the
> deterministic contracts and will not claim fresh telemetry or a current
> Foundry registration without successful gates and readback.”

The checked-in registration command is verification-only by default. It first
requires a clean live ownership boundary and exact fresh telemetry:

```powershell
$env:PYTHONPATH = Join-Path $PWD "src"
$env:FOUNDRY_PROJECT_ENDPOINT = "<platform project endpoint>"
python -m contoso_foundry.field.register `
  --project-endpoint $env:FOUNDRY_PROJECT_ENDPOINT `
  --confirm-resource-group rg-contoso-agents `
  --enable-module optional-control-plane `
  --smoke-correlation-id "<fresh correlation ID>" `
  --smoke-started-at "<fresh UTC ISO 8601 timestamp>" `
  --container-app-revision "<exact current revision>"
```

Successful verification prints the nonzero span count and exact
`gen_ai.agent.id` without writing. Creating a new version additionally requires
`--register --confirm-registration contoso-field`; the command then reads back
the exact name, agent ID, version ID, version, and OTel ID. The official portal creation path,
when deliberately needed outside the demo, is **Build** > **Agents** >
**New agent** > **Link external agent**.

Current preview limits include no human evaluation, no conversion of traces
into an evaluation dataset, and no AI red-teaming target for external agents.
Recheck the
[documented limitations](https://learn.microsoft.com/azure/foundry/agents/how-to/register-external-agent#current-limitations)
before presenting.

## Supported invocation paths

### Browser

Use the authenticated `travel` project, choose **Build** > **Agents**, and open
the exact `contoso-travel` version in the agent chat. Send only the three prompts above. The
[prompt-agent quickstart](https://learn.microsoft.com/azure/foundry/agents/quickstarts/prompt-agent)
is the first-party reference for the portal and SDK workflow.

### Repository CLI

The supported live workflow is:

```powershell
$env:PYTHONPATH = "src;agents\travel\src"
$env:FOUNDRY_PROJECT_ENDPOINT = "<Travel project endpoint copied from the authenticated project>"
python -m contoso_travel_agent.operations deploy
python -m contoso_travel_agent.operations smoke
python -m contoso_travel_agent.operations evaluate
```

`deploy` creates a new immutable candidate, so do not run it merely to present
an already-deployed version. For a normal demo, use the existing version in the
browser. If an authorized release operator does run the sequence:

- `deploy` writes `internal/travel/agent-version.json`;
- `smoke` invokes that exact version and writes
  `internal/travel/smoke-result.json`; and
- `evaluate` writes summary and per-sample evidence under `internal/travel/`.

Those files can contain live identifiers and never belong in git or `docs/`.
The SDK authenticates through `DefaultAzureCredential`; see
[Azure Identity authentication guidance](https://learn.microsoft.com/python/api/overview/azure/identity-readme).

Use read-only Azure CLI to confirm the relative resources without copying their
IDs:

```powershell
az resource list --resource-group rg-contoso-agents `
  --query "[].{Name:name,Type:type}" --output table
$accountId = az cognitiveservices account show `
  --resource-group rg-contoso-agents `
  --name contoso-agents-foundry --query id --output tsv
az rest --method get `
  --url "https://management.azure.com$accountId/projects/travel?api-version=2026-07-01" `
  --query "{Name:name,State:properties.provisioningState}" --output table
```

## Observability walkthrough

Microsoft Foundry stores agent traces in
[Application Insights](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-setup)
using OpenTelemetry conventions. Each Foundry project has a uniquely named
connection to the shared `contoso-agents-insights` component and
`contoso-agents-logs` workspace.

1. From `travel`, choose **Manage** > **Project details** >
   **Connected resources** and identify its Application Insights connection.
   Confirm the target relative name is `contoso-agents-insights`; do not expose
   the connection string or resource ID.
2. Open the agent's tracing view in Foundry, or open
   `contoso-agents-insights` in Azure portal and use its transaction search.
3. Filter to the Travel service/agent dimensions:
   `service.name = contoso-travel`,
   `gen_ai.agent.name = contoso-travel`, and, for smoke/custom spans,
   `contoso.synthetic = true`.
4. Select a trace from the demo time window. A good trace has a root Travel
   request or smoke span, model activity, `contoso.travel.openapi` child spans, and
   `gen_ai.tool.name` values matching the route, policy, fare, and simulation
   sequence.

The smoke span also records `gen_ai.agent.version`; the traffic job records
`contoso.scenario.id` and `contoso.tool.count`. Missing spans are not proof of
success.

Prompt and completion content recording is privacy-sensitive. Client
instrumentation disables content capture, while Foundry service spans can retain
synthetic prompt/tool details needed by evaluation. Never send human or customer
content through this demo. Microsoft documents
[client-side agent tracing](https://learn.microsoft.com/azure/foundry/observability/how-to/trace-agent-client-side)
and [Azure Monitor OpenTelemetry configuration](https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-configuration?tabs=python).
Never enable content recording just to improve a demo.

## Evaluation walkthrough

The exact golden set is
`agents/travel/golden/travel.jsonl`. It has three cases, all bound to
`emea-travel-coordinator`:

| Case | Required behavior |
| --- | --- |
| `travel-route-search-001` | Exact route tool and arguments; includes `ROUTE-0001` and “synthetic” |
| `travel-policy-001` | Exact policy tool; includes “policy”; no scope override |
| `travel-booking-simulation-001` | Fare search followed by simulation with exact IDs/date; includes “simulation” and “no purchase” |

For deterministic local evidence:

```powershell
$env:PYTHONPATH = "src;agents\travel\src"
python -m pytest tests\test_travel_agent.py tests\test_travel_evaluation.py
```

Expected evidence is a passing test run proving the exact definition/model
version, immutable version routing, scope-argument rejection, exact tool order
and arguments, four evaluation criteria, and `internal/`-only live evidence.
This is local deterministic contract evidence with mocked clients; it is not a
live model-quality result.

For an authorized live candidate, run:

```powershell
python -m contoso_travel_agent.operations evaluate
```

The command invokes the exact candidate against the same golden set, then
submits captured outputs to the Foundry/OpenAI evaluation API. It requires all
four criteria to pass: **quality**, **safety**, **task correctness**, and
**tool correctness**. A completed run containing failed samples is still a
failure.

`evals/azure.eval.yaml` also defines the public-preview `azd ai eval`
trace-discovery signal described by Microsoft's
[Azure Developer CLI evaluation guidance](https://learn.microsoft.com/azure/foundry/observability/how-to/azure-developer-cli-evaluation).
It can filter by agent name but cannot pin an immutable prompt-agent version,
so it cannot promote a candidate. The fixed golden run is the release gate;
trace discovery is an additional continuous signal, not equivalent evidence.

!!! success "Publication readiness evidence: recurring evaluation completed"

    On 2026-09-01 UTC, the response-source recurring safety evaluation completed
    with one passing result and no failed or errored samples. Re-open the
    evaluation in Foundry Monitor before presenting; this is point-in-time
    readiness evidence, not a permanent health claim.

Read back the newest recurring result without displaying its evaluation ID,
run ID, or report URL:

```powershell
$env:PYTHONPATH = "src;agents\travel\src"
$env:FOUNDRY_PROJECT_ENDPOINT = "<Travel project endpoint>"
python -m contoso_travel_agent.operations verify-continuous
```

The command fails unless exactly one named continuous evaluation exists and its
newest run is completed, has at least one sample, has every sample passing, has
zero failed or errored samples, and exposes a report URL.

## AI Gateway walkthrough

`contoso-agents-gateway` is the designated API Management ingress for routes
explicitly enrolled in Gateway governance. It is not a universal proxy:
Travel, Support, and Research retain native Foundry contracts, and Field has an
authenticated external runtime. A direct Foundry invocation does not prove the
Gateway route.

Run the read-only verifier:

```powershell
$env:PYTHONPATH = "src"
python -m contoso_foundry.cli gateway verify
```

It checks the Basic v2 service, managed identity, diagnostic destination,
resource-specific log schema, APIs, backend policy, model metadata, exact model
versions, responsible-AI policy, and token-governance fragments. The safe demo
defaults live in `config/gateway.yaml`: per-project token-per-minute limits and
total monthly token quotas are configuration, not service maximums.

The repository does not ship a general Gateway request client, because a usable
request would need a live route and protected APIM credential. Use an already
authorized synthetic client from the private demo environment if one has been
prepared; otherwise show the read-only verifier and diagnostics. Do not invent
a `curl` command, reveal a key, or call a native Foundry endpoint and describe
it as Gateway traffic.

Microsoft's
[`llm-token-limit` policy](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy)
defines the evidence story:

- `200` means an authorized request reached the enrolled route successfully;
- `429` means the per-minute token limit rejected the request; and
- `403` means the total quota rejected the request.

Only show those response codes as live evidence if the current ignored
`internal/` evidence and `ApiManagementGatewayLogs` both contain the matching
safe test. Do not generate quota failures during a presentation and never show
an APIM subscription key or live endpoint.

The checked-in diagnostic contract emits the resource-specific
[`ApiManagementGatewayLogs`](https://learn.microsoft.com/azure/azure-monitor/reference/tables/apimanagementgatewaylogs)
table without request or response bodies. These existing queries are valid for
that table:

```kusto
ApiManagementGatewayLogs
| where TimeGenerated > ago(1h)
| project TimeGenerated, ApiId, OperationId, ResponseCode, TotalTime,
    BackendResponseCode, CorrelationId
| order by TimeGenerated desc
```

```kusto
ApiManagementGatewayLogs
| where TimeGenerated > ago(1h)
| where ResponseCode in (403, 429)
| summarize Requests = count() by ResponseCode, ApiId, bin(TimeGenerated, 5m)
```

## Five-minute deterministic fallback

If portal access, agent invocation, model capacity, or telemetry fails, say:

> “The live dependency is unavailable, so I will show the same checked-in
> contracts without representing them as live success.”

Then run:

```powershell
$env:PYTHONPATH = "src;agents\travel\src"
python -m contoso_foundry.cli data verify --out data\build
python -m contoso_foundry.cli toolbox smoke --out data\build
python -m pytest tests\test_travel_agent.py tests\test_travel_evaluation.py `
  tests\test_field_agent.py tests\test_field_registration.py `
  tests\test_field_deploy.py
```

Show:

1. deterministic rebuild: 3,721 rows across 19 tables, with zero integrity,
   privacy, provenance, and lock differences;
2. Toolbox smoke: EMEA Travel Coordinator, `TPOL-EMEA-01`, `ROUTE-0001`,
   `FARE-00001`, and an allowed `321.81 USD` simulation;
3. unknown-principal refusal before a tool exists;
4. the passing Travel contract/evaluation and Field runtime/registration tests;
5. Field's internal ingress, scale-to-zero, image-digest, and exact telemetry-ID
   contracts without claiming a live runtime or registration;
6. the [architecture overview](architecture/overview.md), this guide, and any
   already-generated sanitized report.

For the Field portion, say explicitly whether the live boundary, active replica,
fresh correlated spans, and registration readback were available. If any one
is missing, the deterministic proof is the Field segment; it is not a live
substitute.

Do not open raw files under `internal/` on a shared screen. Never turn a local
test into a claim that a live request, trace, or evaluation succeeded.

## Troubleshooting without bypasses

| Symptom | Safe response |
| --- | --- |
| Wrong or empty project list | Recheck `az account show`; in Foundry use project selector > **View all resources** > `contoso-agents-foundry` > `travel`. The recent list is not inventory. |
| Missing `contoso-travel` or model | Confirm the selected project and use read-only resource inventory. Do not deploy a replacement during the demo or select another agent/model. |
| Authentication or role error | Reauthenticate with the intended account and ask an authorized owner to verify [Foundry RBAC](https://learn.microsoft.com/azure/foundry/concepts/rbac-foundry). Do not add broad roles or use keys. |
| No traces yet | Narrow to the demo time window, check `contoso-travel` service/agent fields, allow ingestion time, then run the deterministic fallback. Do not enable content recording. |
| Field has zero replicas | Treat scale-to-zero as expected. Do not bypass a failing ownership gate or make ingress external; use the deterministic Field fallback unless a pre-approved maintenance smoke owns wake-up and rollback. |
| Field registration is absent | Show the external-agent contract and deterministic registration tests. Do not create a registration during the presentation or imply that Application Insights spans prove a Foundry record exists. |
| Stale portal UI | Refresh once, reopen through **View all resources**, and verify state with read-only CLI. Do not mutate a healthy resource to refresh the UI. |
| Gateway route returns an error | Run `foundry gateway verify`, inspect body-free Gateway logs, and confirm the request used an enrolled route. Do not call the backend directly and label it Gateway evidence. |
| Boundary, data, identity, version, quota, or evaluation gate fails | Stop. Preserve private evidence under `internal/`, use the fallback, and fix the declared contract later. Never weaken a fail-closed gate for the presentation. |

## Optional extensions

Choose extensions according to the time and the durable capability, not a
point-in-time deployment claim.

| Extension | Time | Durable status | What to show |
| --- | ---: | --- | --- |
| Support | +5 min | **Live public-preview hosted agent, immutable version 4** | Three-stage Agent Framework workflow, server-bound support identity, visible/hidden/concurrent RLS proof, and exact 100% routing. See [Support](agents/contoso-support.md). |
| Research | +5 min | **Live public-preview hosted agent, immutable version 4** | LangGraph planner-retrieval-synthesis, request-local audit, server-fixed caller scope, exact route/model/toolbox versions. See [Research](agents/research.md) and [LangGraph traces](https://learn.microsoft.com/azure/foundry/how-to/develop/langchain-traces). |
| Concierge ALM | +10 min | **Code-only scaffold until authorized DEV export, environments, capacity, connections, import, and publication exist** | Managed solution path, TEST/PROD artifact identity, manual publication/admin gates, honest limitations. See [Concierge ALM](operations/concierge-alm.md) and [Copilot Studio ALM guidance](https://learn.microsoft.com/microsoft-copilot-studio/guidance/alm). |
| Control Plane | +5 min | **Approvals live; SRE intentionally stopped** | Approvals rejects false synthetic input before Agent execution and returns only a human-review envelope. See [Control Plane coverage](operations/control-plane-coverage.md). |

For a compressed **15-minute** session, run the Travel story and the correlated
Field runtime/telemetry proof, shortening each close. For a **30-minute**
session, keep both required agents, then add evaluation, Gateway, and at most
one optional extension. Do not spend core time on optional SRE or Approvals.

## Presenter checklist

- [ ] Azure subscription and tenant display are correct and kept off-screen.
- [ ] `rg-contoso-agents` is present; no other resource group will be mutated.
- [ ] Foundry is on `contoso-agents-foundry` > `travel`.
- [ ] `contoso-travel` and the intended immutable version are visible.
- [ ] The three golden prompts are ready to paste exactly.
- [ ] The endpoint, keys, tokens, connection strings, IDs, and `internal/` files are hidden.
- [ ] The deterministic fallback commands work from the current checkout.
- [ ] Trace filters use `contoso-travel`; content recording remains disabled.
- [ ] Gateway claims are limited to designated routes and evidence actually observed.
- [ ] Field uses a fresh correlation, UTC start time, and exact revision.
- [ ] Field registration is shown only after successful gate and readback evidence.
- [ ] Optional modules are labelled preview, code-only, permission-dependent, or disabled as appropriate.

## Close

> “Contoso Foundry is one governed agent platform, not a pile of demos. Each
> specialist can use the right framework and hosting model while sharing
> synthetic truth, server-derived scope, managed identity, observability,
> model governance, and one explicit Azure blast radius. When live evidence is
> unavailable, the platform fails closed and the demo says so.”
