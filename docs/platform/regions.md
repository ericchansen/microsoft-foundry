# Region selection

The deployment region is an **output**, not a preference. No region name is
hard-coded anywhere in this repository's logic. `foundry regions` starts from
every physical Azure region the subscription can see, removes candidates that
fail a hard requirement, and ranks the survivors.

## The process

```mermaid
flowchart TB
    all["All physical Azure regions<br/>(live, from ARM)"]
    res["Residency<br/>allowed geographies"]
    rel["Reliability<br/>paired region required"]
    lat["Latency<br/>distance from user population"]
    rt["Resource types<br/>live, per namespace"]
    cap["Capability matrix<br/>sourced doc tables"]
    mq["Models and quota<br/>live, per region"]
    rank["Rank: cost, then capacity, then latency"]
    out["Selected region"]

    all --> res --> rel --> lat --> rt --> cap --> mq --> rank --> out
```

Gates run cheapest-first so the expensive live probes only ever run against a
short list.

## What each gate tests

| Gate | Evidence | What it removes |
| --- | --- | --- |
| **Residency** | Live ARM region metadata | Regions outside the geographies the demo is permitted to store data in. |
| **Reliability** | Live ARM `pairedRegion` | Regions with no Azure paired region, so there is no documented geo-redundancy story. |
| **Latency** | Great-circle distance from the primary user population | Regions beyond the acceptable distance. |
| **Resource types** | Live `az provider show` per namespace | Regions where core Container Apps, Application Insights, Log Analytics, API Management, or Foundry accounts cannot be created. |
| **Capability matrix** | Sourced documentation tables | Regions lacking core [API Management Basic v2](https://learn.microsoft.com/azure/api-management/api-management-region-availability), [Foundry Agent Service](https://learn.microsoft.com/azure/foundry/agents/concepts/limits-quotas-regions), or [Power Platform co-residency](https://learn.microsoft.com/microsoft-copilot-studio/data-location). |
| **Models** | Live `az cognitiveservices model list` | Regions where the required frontier chat model is not deployable. |
| **Quota** | Live `az cognitiveservices usage list` | Regions without enough available quota to run the platform. |

Availability zones are recorded and reported but are **not** a hard gate.
API Management Basic v2 offers no zone redundancy and Container Apps consumption
workloads are single-region, so requiring zones would eliminate candidates for
resilience v1 cannot actually use. This is a deliberate, reversible choice
recorded in `config/region-requirements.yaml`; it should be raised to three zones
when the platform moves to a zone-redundant tier.

## Why the capability matrix exists

Most gates are answered by an API. Some capabilities Microsoft publishes only as
a documentation table — API Management v2 tier availability, SRE Agent regions,
Foundry Agent Service regions, Power Platform data locations. For those, and only
those, `config/capability-matrix.yaml` records the published list along with the
source URL and the date a human last checked it.

The alternative — trusting the resource provider's location list — is wrong in a
way that matters. `Microsoft.ApiManagement/service` reports many regions where
the **Basic v2 tier specifically** is unavailable, so a provider-only check would
select a region the platform cannot actually be built in.

Logic Apps agent loops are handled differently again: Microsoft publishes no
region table, and the documentation states the agent's model comes from Azure
OpenAI. The matrix therefore *derives* that capability from Foundry availability
rather than inventing a list.

Azure SRE Agent and Logic Apps Approvals are optional platform coverage, so they
do not eliminate regions in the default core run. Enable their requirements
explicitly before deploying those modules:

```bash
foundry regions --enable-module optional-control-plane
```

That run adds the SRE Agent and Logic Apps resource types plus their sourced
capability checks to the same elimination pipeline.

## Ranking

Survivors are ranked **cost first, capacity second, latency third**.

Cost is a live comparison basket priced per region against the
[Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices).
The basket contains only components whose unit price actually varies by region.
Model inference is billed on global meters and does not discriminate between
regions, so including it would add noise without changing the ordering.

Capacity is the remaining quota reported by Azure, so a region with less
available quota ranks below one with more. Latency breaks the remaining ties.

## Result

The decision is committed to
[`config/selected-region.yaml`](https://github.com/ericchansen/microsoft-foundry/blob/main/config/selected-region.yaml),
which `foundry regions` writes on every run. It is a **generated file that is
deliberately tracked**: committing it lets CI price the estimate without Azure
credentials, and re-running the command turns any drift into a reviewable diff
rather than a surprise at provisioning time. Do not edit it by hand — change
`config/region-requirements.yaml` or `config/capability-matrix.yaml` and re-run.

Each run writes the selected core region to configuration and keeps the full
per-region elimination trace, including subscription quota readings, under
`internal/`. Optional-module runs must regenerate the decision with their
additional requirements before those modules are enabled.

!!! note "Subscription-scoped numbers stay internal"
    Per-region quota limits and consumption are properties of a specific
    subscription. The full elimination trace including those numbers is written to
    `internal/region-selection.md`, which is excluded from this site and from
    version control. The published summary is sanitized.

## Re-running it

```bash
foundry regions
```

Requires a signed-in Azure CLI. See [Verification](../operations/verification.md).
