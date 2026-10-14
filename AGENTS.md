# Repository conventions

## Private data

Never place any of the following in a tracked file:

- Tenant or subscription GUIDs
- `onmicrosoft.com` domains, or any tenant-specific domain
- Azure resource IDs, or subscription/tenant display names
- Secrets, keys, connection strings, tokens
- Live tenant-specific endpoints
- Real personal data

The subscription is supplied by the environment at run time. Configuration files
name resources **relatively** and never absolutely.

If evidence genuinely requires an identifier, it goes in `internal/`, which is
gitignored and outside the docs tree. Publish a sanitized summary instead.

Run `foundry scan site` before pushing anything that changes `docs/`.

## Evidence over assertion

If a claim can be checked by an API, check it by an API.

- **Never hard-code a conclusion a script should derive.** No region name appears
  in decision logic. Requirements go in `config/`; the answer is computed.
- **Never assume a price.** Look it up in the Retail Prices API. If a meter is
  ambiguous, fail — do not pick one. If a component is billable but unpriced,
  reserve budget; recording zero is banned.
- **Cite first-party sources.** Every product, behaviour or pricing claim in
  `docs/` needs a clickable Microsoft link. Add it to `docs/reference/sources.md`.
- **Label public previews** wherever they appear.
- **Never fabricate a result.** If something cannot be verified, say so and
  document the exact steps to verify it. An honest blocker is a deliverable; a
  fabricated success is a defect.

When Microsoft publishes a capability only as a documentation table, record it in
`config/capability-matrix.yaml` with a `source` URL and an `as_of` date. When it
has an API, query the API and delete the matrix entry.

## Layout

| Path | Purpose | Published | Committed |
| --- | --- | :---: | :---: |
| `src/contoso_foundry/` | The `foundry` CLI | no | yes |
| `config/` | Declarative inputs | no | yes |
| `costs/` | Cost estimate | no | yes |
| `docs/` | Public site source | **yes** | yes |
| `tests/` | Unit tests | no | yes |
| `internal/` | Identifier-bearing evidence | **never** | **never** |
| `reports/` | Sanitized generated summaries | no | no |
| `site/` | MkDocs build output | **yes** | no |

Generated output is never committed. A committed report can only ever be stale,
and a stale cost report is worse than no cost report.

## Code

- Python ≥ 3.11. Dependencies are deliberately minimal — `PyYAML` and `requests`.
  Adding one requires justification in the PR.
- Azure access goes through `azure_cli.py`, not an SDK, so scripts authenticate
  the same way a human does.
- Regexes for identifiers and secrets live **only** in `patterns.py`. The
  sanitizer and the scanner share them so they cannot drift apart — a sanitizer
  that misses what the scanner catches is a leak waiting to happen.
- Comment *why*, not *what*. Explain non-obvious decisions and rejected
  alternatives; do not narrate the code.
- Tests must not require network access or an Azure login. Mock the API.

```bash
ruff check .
pytest
```

## Documentation

`docs/` is the public site. Write for a reader who has not seen the repository.

- Prefer explaining a decision and its trade-off over listing a feature.
- One idea per page; link rather than repeat.
- Mermaid diagrams use a fenced ` ```mermaid ` block.
- The site is explanatory only. It deep-links to authenticated experiences; it
  never calls an agent and holds no credentials.
- `mkdocs build --strict` must pass — a broken internal link fails the build.

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
  `docs:`, `chore:`, `ci:`, `refactor:`, `test:`.
- Keep history linear; rebase rather than merge.
- Include the trailer:

  ```text
  Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>
  ```

Before pushing:

```bash
ruff check . && pytest
foundry boundary --no-live
foundry costs
mkdocs build --strict && foundry scan site
```

Never push to `main`. Never merge your own PR.
