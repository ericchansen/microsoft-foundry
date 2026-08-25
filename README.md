# microsoft-foundry

A worked example of designing a governed, multi-agent platform on
[Microsoft Foundry](https://learn.microsoft.com/azure/foundry/) — for a
fictitious company called Contoso.

**Public documentation site:** <https://ericchansen.github.io/microsoft-foundry/>

The interesting part of this repository is not the architecture. It is that the
architecture's key decisions — *which region*, *what it costs*, *what it is
allowed to touch* — are **outputs of scripts that query live Azure APIs**, not
assertions in a document. Anyone can re-run them.

## Status: Phase 0

Phase 0 is the decision and governance foundation. **No Azure resources have been
provisioned.** This repository currently contains the tooling that decides and
verifies, plus the public documentation site.

## What is here

| Path | Contents |
| --- | --- |
| `src/contoso_foundry/` | The `foundry` CLI: discovery, region selection, cost model, boundary check, content scanner. |
| `config/` | Declarative inputs — region requirements, capability matrix, ownership boundary. |
| `costs/` | The v1 cost estimate, priced live against the Azure Retail Prices API. |
| `docs/` | Source for the public site. Everything here is published. |
| `internal/` | Identifier-bearing evidence. **Never published, never committed.** |
| `reports/` | Generated sanitized summaries. Not committed — regenerate them. |
| `tests/` | Unit tests. No network, no Azure login required. |

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,docs]"

foundry costs                 # prices the estimate live; fails over budget
foundry boundary --no-live    # validates the ownership boundary
pytest                        # unit tests
mkdocs serve                  # preview the site
```

`foundry costs` and `foundry scan` need no Azure credentials. `foundry discover`
and `foundry regions` need a signed-in Azure CLI:

```bash
az login
az account set --subscription "<your subscription>"
foundry regions               # eliminates and ranks candidate regions
```

## The three decisions

### Which region

`foundry regions` starts from every physical Azure region the subscription can
see and eliminates candidates that fail a hard requirement — residency,
reliability, latency, resource-type availability, published capability tables,
live model availability, live quota headroom — then ranks the survivors by
**cost first, capacity second, latency third**.

No region name appears in any decision logic. Changing a requirement in
`config/region-requirements.yaml` changes the answer.

→ [Region selection](https://ericchansen.github.io/microsoft-foundry/platform/regions/)

### What it costs

`foundry costs` prices every line item in `costs/v1-estimate.yaml` against the
[Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
and hard-fails above **$500/month**. An ambiguous meter is an error, not a guess.
A billable component with no published meter must reserve budget rather than
record zero. Copilot Studio bills in Copilot Credits, not Azure meters, so it is
tracked separately and does not consume the Azure ceiling.

→ [Cost model](https://ericchansen.github.io/microsoft-foundry/platform/costs/)

### What it may touch

Everything this project creates lives in **one resource group**. `foundry
boundary` asserts that every planned resource, identity, role assignment,
diagnostic setting and teardown target is scoped inside it, and refuses to
provision if not. Scopes are relative by design, so they cannot name another
subscription or resource group.

→ [Ownership boundary](https://ericchansen.github.io/microsoft-foundry/platform/boundaries/)

## Publishing safety

This is a public repository publishing a public site. Before deployment the
**generated HTML** is scanned — not just the Markdown — and the build fails on
tenant or subscription GUIDs, `onmicrosoft.com` domains, Azure resource IDs,
secrets, connection strings, live tenant endpoints, or non-synthetic personal
data.

Evidence that necessarily contains identifiers is written to `internal/`, which
is excluded from version control and sits outside the documentation source tree.
The scanner asserts both conditions rather than trusting them.

Findings are reported with redacted excerpts — a scanner that prints the secret it
found into a public build log has leaked the secret.

## Contributing

See [`AGENTS.md`](AGENTS.md) for repository conventions. It applies to human and
AI contributors equally.

## Disclaimer

Contoso is fictitious. This is a demonstration build and is not affiliated with
or endorsed by Microsoft. Components in public preview are labelled where they
appear; verify against the linked first-party source before relying on one.

## Licence

[MIT](LICENSE)
