# Verification

Every claim on this site is reproducible. This page is how.

## Setup

```bash
git clone https://github.com/ericchansen/microsoft-foundry.git
cd microsoft-foundry
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev,docs]"
```

The Azure-facing commands use the Azure CLI, so they authenticate exactly the way
a human does:

```bash
az login
az account set --subscription "<your subscription>"
```

## The commands

| Command | Needs Azure login | What it does |
| --- | :---: | --- |
| `foundry discover` | yes | Read-only sweep of subscription, resource groups, resource providers and licensing. |
| `foundry regions` | yes | Eliminates and ranks candidate regions. |
| `foundry costs` | no | Prices the estimate live and enforces the budget ceiling. |
| `foundry boundary` | optional | Validates the ownership boundary. `--no-live` skips the Azure checks. |
| `foundry scan <path>` | no | Fails if publishable content contains an identifier or secret. |

`foundry costs` and `foundry scan` need no Azure credentials, so anyone can
verify the budget gate and the publishing gate.

## Where output goes

Output is split by whether it contains identifiers.

| Destination | Contents | Published? | Tracked in git? |
| --- | --- | :---: | :---: |
| `reports/` | Sanitized summaries | no | no |
| `internal/` | Full evidence including subscription-scoped numbers | **never** | **never** |

`internal/` is excluded from version control and sits outside the documentation
source tree, so it cannot reach this site by accident. The scanner asserts both
conditions on every run rather than trusting them.

Nothing generated is committed. Reports regenerate from live APIs, so a committed
copy could only ever be stale — and a stale cost report is worse than none.

## The publishing gate

Before this site deploys, the **generated HTML** is scanned — not the Markdown
source. Anything embedded by a theme, a plugin or a Mermaid diagram is checked
too. The build fails on:

- Tenant or subscription GUIDs
- `onmicrosoft.com` domains
- Azure resource IDs
- Secrets, keys and connection strings
- Live tenant-specific endpoints
- Non-synthetic personal data

Deployment is downstream of the scan, so a failing scan means nothing publishes.

Findings are reported with **redacted** excerpts. A scanner that prints the secret
it found in a public build log has leaked the secret.

## Continuous integration

Every push runs:

1. `ruff` lint
2. `pytest` — the scanner and redaction rules, the tier maths, the region gates
   and ranking, and the ownership-boundary rules.
   No test needs network access or an Azure login.
3. `foundry boundary --no-live` — the plan can never drift out of the boundary.
4. `foundry costs` — live pricing against the real API, hard-failing over budget.
5. `mkdocs build --strict` — a broken internal link fails the build.
6. `foundry scan site` — the publishing gate above.

The cost gate runs against live prices deliberately. If Microsoft raises a price
enough to push the platform over budget, the build should break — that is the
alert.

## Re-verifying documentation claims

Product capabilities that Microsoft publishes only as documentation tables are
recorded in `config/capability-matrix.yaml`, each with a source URL and the date a
human last checked it. [Sources](../reference/sources.md) lists every first-party
page this site relies on.
