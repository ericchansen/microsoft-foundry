# Cost model

The platform has a hard ceiling of **$500/month** of incremental Azure spend.
That ceiling is enforced by a script in CI, not by a spreadsheet.

## How it works

`costs/v1-estimate.yaml` declares every component the platform will run, the
quantity it will consume, and the meter that bills it. `foundry costs` looks up
each meter live in the
[Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices),
applies the quantity, and fails if the Azure total exceeds the ceiling.

The [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/)
is a **human cross-check only**. It is not in the gate, because a number a person
typed into a web page is not evidence a build can verify.

## Rules the model enforces

These exist because each represents a way a cost model can quietly lie.

**An ambiguous meter is a hard failure.** If a meter specification matches more
than one meter, the model raises rather than picking one. API Management publishes
`Basic v2 Unit` and `Basic v2 Secondary Unit` at the *same* price in the same
region — filtering on SKU alone silently matches both, and would keep matching
both if their prices ever diverged.

**Tiers are applied marginally, in base units.** Azure returns `tierMinimumUnits`
in the meter's *billing* unit while quantities are declared in the meter's *base*
unit. A `10K` meter with a tier minimum of `1000` means "beyond ten million
calls", not "beyond one thousand". Comparing the two directly prices almost
everything into the cheapest band and understates the bill.

**Free grants are shown, not assumed.** Container Apps' monthly free grants are a
billing credit rather than a price tier, so they never appear in the API response.
Each affected line subtracts them in the declared quantity and shows the
arithmetic in its notes, so the number is auditable.

**Unit-of-measure drift is detected.** Each line declares the unit it expects. If
Microsoft re-denominates a meter, the run fails loudly instead of silently
changing the answer by a factor of a thousand.

**An unpriceable component reserves budget.** A billable component with no
published meter must declare a reserve. Recording it as zero is banned.

**Inference is priced separately from orchestration.** Logic Apps agent-loop
token meters bill the *orchestration runtime*; the Foundry model deployment bills
the *inference it serves*. They are different meters on different services, and
an estimate that lists only one of them understates the bill. Both appear as
explicit line items, and neither assumes cached-input or Batch discounts.

## Components with no published price

[Azure SRE Agent](https://learn.microsoft.com/azure/sre-agent/pricing-billing)
bills in Azure Agent Units — four AAUs per agent-hour for the always-on flow,
plus per-token AAUs for active flows. Microsoft publishes the AAU consumption
rates but not a USD-per-AAU conversion, and the Retail Prices API returns no SRE
Agent meter at all.

The model therefore holds an explicit monthly reserve against the ceiling and
reports it as a warning on every run. When a meter is published, the reserve is
replaced with a real lookup.

## Copilot Studio is not in the Azure budget

[Microsoft Copilot Studio](https://learn.microsoft.com/microsoft-copilot-studio/billing-licensing)
bills in **Copilot Credits**, not Azure meters. It is reported separately and does
not consume the $500 ceiling, because Azure does not meter it.

Published consumption rates:

| Interaction | Credits |
| --- | ---: |
| Classic answer | 1 |
| Generative answer | 2 |
| Agent action | 5 |
| Tenant graph grounding | 10 |

Source: [Copilot Studio message and credit management](https://learn.microsoft.com/microsoft-copilot-studio/requirements-messages-management).

!!! warning "Two things are unverified here"
    **The USD-per-credit rate is not published on Microsoft Learn.** It appears
    only in the licensing guide PDF. The figure in `costs/v1-estimate.yaml` is a
    planning placeholder, labelled as such, and must be confirmed against a real
    invoice before anyone relies on it.

    **Tenant capacity has not been confirmed.** Credit capacity must be verified
    in the Power Platform admin centre, and pay-as-you-go overage requires an
    explicit billing policy. Tenant-wide publication of the concierge agent is
    **blocked** until both are done. See
    [managing Copilot credits capacity](https://learn.microsoft.com/power-platform/admin/manage-copilot-studio-copilot-credits-capacity)
    and [overage enforcement](https://learn.microsoft.com/microsoft-copilot-studio/requirements-messages-management#overage-enforcement).

## Reading the result

`foundry costs` writes a full report showing every line item, its resolved meter,
the quantity, the monthly cost, and the reasoning behind each quantity. The
report is regenerated on every run rather than committed, so it can never drift
from current list prices.

The largest fixed cost is the API Management Basic v2 unit. It is also the line
most sensitive to region choice — its hourly rate varies by roughly 30% across
otherwise-qualifying regions, which is why it dominates the region comparison
basket.

## Re-running it

```bash
foundry costs                       # prices for the selected region
foundry costs --region <region>     # prices for a specific region
```

Requires network access to the pricing API. It does **not** require an Azure
login: retail prices are public.
