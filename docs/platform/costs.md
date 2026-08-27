# Cost observability and policy

Cost is an operational signal, not a permanent architectural constraint. The
platform makes consumption observable and attributable, prevents accidental
unbounded usage, and lets operators set an environment-specific policy ceiling
in versioned configuration.

## How the estimate works

The [checked-in estimate](https://github.com/ericchansen/microsoft-foundry/blob/main/costs/v1-estimate.yaml)
records workload assumptions, billing classifications, meter lookup terms,
unpublished-meter reserves, and a sample environment policy. The
[`foundry costs`](https://github.com/ericchansen/microsoft-foundry/blob/main/src/contoso_foundry/costs.py)
command:

1. loads and validates every declared cost line;
2. queries the
   [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
   for Azure-metered services;
3. fails when a lookup is missing or ambiguous rather than choosing a convenient
   meter;
4. multiplies the selected unit price by the checked-in quantity;
5. adds explicit non-zero reserves for billable components without a published,
   unambiguous meter;
6. attributes shared and workload-specific costs separately; and
7. compares Azure-metered spend with the configured environment ceiling.

The checked-in ceiling is sample policy-as-code for the reference environment,
not an agent-selection rule. Operators must review the workload quantities,
reserves, and ceiling for each environment.

## Attribution rules

Every line is classified so the model can answer both "what does the platform
cost?" and "which workload caused this consumption?"

| Classification | Treatment |
| --- | --- |
| Shared fixed | Counted once for the estate, then attributed to the platform rather than copied into every agent |
| Shared variable | Aggregated once from a platform-wide workload assumption |
| Workload variable | Assigned to the specialist or workflow that generates the consumption |
| Reserved unpublished meter | Held as a visible non-zero amount until an authoritative meter is available |
| External licensing currency | Reported separately from Azure-metered spend |

This prevents two common errors: duplicating shared infrastructure for every
agent and treating an unpriced billable capability as free.

## Workload assumptions

Quantities in `costs/v1-estimate.yaml` are explicit planning inputs, such as
token volume, API Management capacity, container consumption, workflow actions,
and telemetry ingestion. They describe a sample workload shape, not live usage.

Shared services appear once. Specialist token allocations appear as separate
lines so they can be traced to an owner. Logic Apps orchestration, model inference, and connector actions remain distinct
because they use different meters even when they participate in one workflow.
Approvals and SRE lines are retained as disabled-by-default optional modules, so
the default gate prices only the core deployment.

Run the optional estimates explicitly:

```bash
foundry costs --enable-module approvals
foundry costs --enable-module sre
foundry costs --enable-module approvals --enable-module sre
```

Enabling a module prices its complete incremental workload and includes it in the
same environment policy gate; the tool never records disabled consumption as
zero.

Before relying on an estimate, replace sample quantities with measurements or
approved forecasts for the target environment.

## Live pricing and deterministic offline evidence

Retail prices change. A connected run resolves prices from Microsoft's public
API each time. The committed input file therefore contains lookup criteria, not
assumed Azure prices.

For deterministic tests and offline review, a pricing cache can be supplied
explicitly. Normal connected runs do not silently reuse the disk cache. A stale
cache is evidence for reproduction, not proof of the current price.

## Unpublished meters and reserves

Some documented billable capabilities do not expose a unique meter through the
Retail Prices API. Examples include hosted-agent compute dimensions and Azure
SRE Agent's Azure Agent Unit billing.

Those lines retain explicit non-zero reserves and emit warnings. When Microsoft
publishes an authoritative meter, replace the reserve with a live lookup. Never
record zero merely because the price is unavailable, and never infer a price
from an unrelated meter.

## Copilot Credits

[Microsoft Copilot Studio](https://learn.microsoft.com/microsoft-copilot-studio/billing-licensing)
uses **Copilot Credits**, which are a separate licensing currency from
Azure-metered consumption. The estimate reports them separately and labels any
unverified currency conversion as a planning placeholder.

Operators must confirm licensing rates, tenant capacity, and administrative
controls through authorized Microsoft sources before publication. Copilot
Credits are not added to the Azure-metered total or used to disguise an Azure
policy breach.

## Configuring an environment

To resize the policy for a target environment:

1. measure or forecast token, workflow, container, telemetry, and request
   volumes;
2. update the checked-in quantities and document their workload basis;
3. review every unpublished-meter reserve;
4. set the environment's Azure ceiling in configuration;
5. run `foundry costs` with live pricing; and
6. investigate each policy breach by owner and meter rather than silently reducing
   quantities.

If a gate fails, distinguish duplicated shared resources from legitimate
workload consumption. Remove only genuine duplication. Any reduction to a
production-representative workload must be coordinated with its owner and
recorded as an explicit assumption.

## Reading the output

A successful run proves that all cost lines resolved or retained an explicit
reserve and that the modeled Azure spend is within the configured policy. It
does not prove future invoices, tenant licensing capacity, or production usage.
A failing run is useful evidence: it preserves the declared workload and shows
which lines must be corrected or approved.

The source links for pricing, metering, and licensing claims are maintained in
the [source register](../reference/sources.md).
