# Provenance and licensing

Every artifact in `data/` records where it came from, what licence governs it and
what was done to it. The machine-readable record is
[`data/manifest.yaml`](https://github.com/ericchansen/microsoft-foundry/blob/main/data/manifest.yaml);
`data/MANIFEST.md` is generated from it so the two cannot drift.

## What is recorded

| Field | Why |
| --- | --- |
| `source_url` | A clickable first-party link. "Adapted from a Microsoft sample" is not a provenance record. |
| `license` | The SPDX identifier the source is published under. |
| `version` | A commit SHA, release tag or dated snapshot — not "latest". |
| `transformation` | What was done. Schema-inspired, regenerated, fetched verbatim. |
| `destination` | The path it lands at. |
| `vendored` | Whether bytes from the source are in this repository at all. |

The provenance gate fails the build if any artifact is missing any of these, so a
new table cannot be added without someone stating where its shape came from.

## Nothing restricted is vendored

This is a public repository. That constrains what may be committed far more than
what may be *used*.

!!! danger "The distinction that matters"
    Reading a licence to learn a schema is not redistribution. Copying rows out
    of a share-alike dataset and committing them is, and it can relicense the
    repository around them.

Three categories, three treatments:

**Permissive samples — schema studied, data regenerated.** Microsoft's
[Wide World Importers](https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/wide-world-importers)
and [AdventureWorks](https://learn.microsoft.com/sql/samples/adventureworks-install-configure)
samples are MIT-licensed. Even so, no rows were copied: the entities and their
relationships informed the design, and every value is generated. That keeps the
dataset small, keeps it obviously synthetic, and removes any question about what
the licence covers.

**Share-alike or non-permissive corpora — indexed, never committed.** Google's
[Schema-Guided Dialogue](https://github.com/google-research-datasets/dstc8-schema-guided-dialogue)
is a useful evaluation corpus and is *not* redistributed here. Instead
`data/external/manifest.yaml` pins the repository, commit and licence. The
repository deliberately supplies no downloader. An operator who is authorized
under the source terms may stage that exact revision in the gitignored cache;
cloning this repository never acquires or redistributes it.

**Deliberately excluded.** MS MARCO's terms restrict redistribution, and GAIA is
gated behind an access agreement. Neither is fetched or referenced as data. They
are named here only so the exclusion is a recorded decision rather than an
oversight.

## The external manifest

```yaml
- id: schema-guided-dialogue
  source_url: https://github.com/google-research-datasets/dstc8-schema-guided-dialogue
  licence: CC-BY-SA-4.0
  version: <pinned commit>
  vendored: false
  destination: data/external/cache/schema-guided-dialogue
```

`vendored: false` is load-bearing. External destinations never count as coverage
for committed files, while every committed artifact destination must exist.
Adding corpus bytes outside the ignored cache therefore fails provenance rather
than inheriting a broad directory claim.

## Sources

| Artifact | Source | Licence |
| --- | --- | --- |
| Relational spine concepts | [Wide World Importers](https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/wide-world-importers) | MIT |
| Retail catalogue concepts | [AdventureWorks](https://learn.microsoft.com/sql/samples/adventureworks-install-configure) | MIT |
| Dialogue evaluation corpus | [Schema-Guided Dialogue](https://github.com/google-research-datasets/dstc8-schema-guided-dialogue) | CC-BY-SA-4.0, indexed not vendored |
| PII detection | [Microsoft Presidio](https://microsoft.github.io/presidio/) | MIT, optional dependency |

## Rebuilding

```bash
foundry data build      # regenerate from the seed
foundry data verify     # integrity, privacy, provenance and lock in one pass
```

`verify` prints a difference count per gate and exits non-zero if any is not
zero. It is the same command CI runs, so a green local run means a green pipeline
for the same reason rather than a similar one.