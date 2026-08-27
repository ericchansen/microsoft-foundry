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
6. Create the external registration with `AIProjectClient(allow_preview=True)`.

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
uncorrelated evidence fails without mutation.
