# Contoso Research

Contoso Research is a production-representative LangGraph hosted agent over the
repository's canonical synthetic company data. It answers bounded operational
questions without granting a model direct database access or allowing a request
to choose its own identity scope.

!!! warning "Public preview"
    Microsoft Foundry hosted agents and parts of the unified `azure.yaml`
    deployment surface are public preview. The implementation exercises the
    current contract, but preview limitations still apply.

## Transparent workflow

```mermaid
flowchart LR
    request["Responses request"] --> planner["planner"]
    planner --> retrieval["retrieval"]
    retrieval --> synthesis["synthesis"]
    synthesis --> response["Responses answer"]
    toolbox["Scoped Toolbox 1.0.0"] --> retrieval
    data["Canonical synthetic data"] --> toolbox
```

The compiled graph keeps these fields visible after every node:

- `question` — normalized user question;
- `plan` — exact Toolbox calls and arguments;
- `evidence` — synthetic records returned by those calls;
- `audit` — this invocation's tool, persona, argument names, and result counts;
- `answer` and `messages` — the evidence-bound synthesis result.

Planning is deterministic and bounded to customer, order, invoice, product, and
stock questions. Before the graph runs, the Responses host requires the
platform-injected user partition key and opaque call ID, maps the user key through
an immutable server allow-list to a canonical synthetic principal, and rejects
missing or unknown values. Prompt content and pass-through client headers cannot
select a persona, region, role, `oid`, or `tid`.

Retrieval opens a new read-only SQLite connection and constructs a new scoped
Toolbox for every invocation. Its audit list is therefore request-local even when
requests run concurrently. The graph retains the server-resolved route and opaque
call ID as internal state, but does not copy either into the answer or audit. The
model cannot call SQLite directly or invent a new capability. Synthesis receives
only the question and retrieved evidence; an empty model response is an error.
The opaque call ID is forwarded on the downstream first-party Foundry model call,
while the user partition key is never forwarded as downstream identity.

The production allow-list is JSON supplied through the deployment environment
and frozen at server startup. It maps opaque hosted user partition keys to only
the three canonical fixture routes. The repository's deterministic evaluator has
a separate synthetic mapping; production never falls back to it.

## Exact contracts and routing

The deployment pins four independent versions:

| Contract | Version |
| --- | --- |
| Agent behavior | `1.0.0` |
| Hosted agent version | `1` |
| Responses protocol | `2.0.0` |
| Toolbox capabilities | `1.0.0` |
| Model | `gpt-5.4-mini`, `2026-03-17`, Global Standard |

The Entra-authorized endpoint sends 100% of traffic to hosted version `1`; it
does not use `@latest`. At startup the container compares the platform-injected
agent name and version with that route and exits on a mismatch. The canonical
data lock and every Toolbox contract version are verified before the graph is
served.

## Runtime and identity

The root `Dockerfile` is the deployable image. The current
[Responses protocol](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#protocols-responses-invocations-and-invocations-websocket)
provides conversation history, streaming lifecycle, background execution,
health, and cancellation handling; this agent does not implement a legacy
`/invoke` endpoint.

The unified `azure.yaml` connects to the existing Research project and confines
deployment to `rg-contoso-agents`. It declares the exact model deployment,
container resources, endpoint authorization, and version route. Its fail-closed
`predeploy` hook derives the exact account and project names from the relative
ownership declaration, resolves both resources through ARM, and rejects an
endpoint or resource that does not match the declared resource group.

The Foundry platform creates a dedicated agent identity. Any future downstream
role must be assigned only after that identity exists, following the
[hosted-agent permissions reference](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions),
and never above the project-owned resource group.

The project can use a
[private Azure Container Registry](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent-private-azure-container-registry).
Nothing in this slice enables anonymous access or weakens the registry network
posture.

## Telemetry

`AzureAIOpenTelemetryTracer` traces all graph nodes to the Research project's
shared Application Insights connection. The service name and agent identifier
are both `contoso-research`, producing the expected OpenTelemetry generative-AI
agent attributes. Content recording is disabled; node timing, tool failures,
and standard operational attributes remain available.

See [one telemetry spine](../platform/telemetry-spine.md) for the shared
monitoring design.

## Evaluations and failure behavior

`config/research-evals.yaml` contains deterministic synthetic goldens for:

1. overdue receivables;
2. outstanding receivables;
3. stock availability.

The evaluator checks the agent, hosted route, protocol, platform-user mapping,
persona, plan, row counts, and answer conditions. Unknown identities, missing
call context, wrong versions, data-lock drift, contract drift, unsupported
questions, tool failures, and empty synthesis all fail closed. Tests also run
concurrent personas and prove their rows and audit records stay disjoint. Model
synthesis is mocked, so the suite needs neither Azure credentials nor network
access.

Run the local gates from the branch-local environment:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python -m contoso_foundry.research.evaluate
.\.venv\Scripts\python -m pytest tests\test_research.py
docker build --tag contoso-research:local .
```

## Deployment sequence

The deployment uses the current unified configuration rather than legacy split
agent manifests:

```powershell
$env:PYTHONPATH = "src"
$routes = Get-Content .\internal\research-user-routes.json -Raw
azd env set CONTOSO_RESEARCH_USER_ROUTE_ALLOWLIST $routes
$endpoint = .\.venv\Scripts\python -m contoso_foundry.research.deployment --resolve
azd env set CONTOSO_RESEARCH_PROJECT_ENDPOINT $endpoint
azd deploy contoso-research
azd ai agent show contoso-research
```

The resolver reads only the exact account and nested project named by
`config/boundary.yaml`, verifies both live ARM identities are in
`rg-contoso-agents`, and returns the documented
[project endpoint](https://learn.microsoft.com/azure/foundry/how-to/develop/sdk-overview)
shape. The endpoint value remains an environment input and must not be committed.
The `predeploy` hook repeats the ARM and endpoint checks, so bypassing the
resolver still fails closed.

`internal/research-user-routes.json` is a local, gitignored deployment input whose
keys are the platform-provided opaque user partition values and whose values are
canonical route names. A missing, malformed, empty, or unknown mapping prevents
the hosted server from starting.

After deployment, smoke tests must call the protocol-specific Responses endpoint
for hosted version `1` and use only synthetic prompts from the golden suite. Any
provisioning, API, evaluation, or smoke failure blocks promotion.
