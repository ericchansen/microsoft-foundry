"""Machine-checkable cost model priced against the Azure Retail Prices API.

The estimate in ``costs/v1-estimate.yaml`` is the source of truth for what we
intend to run. This module turns it into a dollar figure using live list prices
and fails the build if the Azure portion exceeds the budget ceiling. The Azure
Pricing Calculator is a human cross-check only — it is not in the gate.

API reference:
https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import yaml

RETAIL_PRICES_ENDPOINT = "https://prices.azure.com/api/retail/prices"

#: Pinned so a server-side default change cannot silently move our numbers.
RETAIL_PRICES_API_VERSION = "2023-01-01-preview"

DEFAULT_BUDGET_USD = 500.0

#: Statuses worth retrying: the API is unauthenticated and aggressively throttled.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

#: Recognised values for a line item's `billing` field. Anything else is a typo,
#: and a typo must not be able to move an Azure meter out of the Azure ceiling.
VALID_BILLING = frozenset({"azure", "external"})

_UNIT_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KM])?\b")


class CostModelError(RuntimeError):
    """Raised when the estimate cannot be priced with confidence."""


def unit_multiplier(unit_of_measure: str) -> float:
    """How many billable units one API ``unitPrice`` covers.

    >>> unit_multiplier("1 Hour")
    1.0
    >>> unit_multiplier("10K")
    10000.0
    >>> unit_multiplier("1M")
    1000000.0
    """
    match = _UNIT_RE.match(unit_of_measure or "")
    if not match:
        raise CostModelError(f"cannot interpret unitOfMeasure {unit_of_measure!r}")
    value = float(match.group(1))
    suffix = match.group(2)
    if suffix == "K":
        value *= 1_000
    elif suffix == "M":
        value *= 1_000_000
    if value <= 0:
        raise CostModelError(f"non-positive unitOfMeasure {unit_of_measure!r}")
    return value


@dataclass
class PriceTier:
    minimum_units: float
    unit_price: float
    unit_of_measure: str
    meter_id: str
    meter_name: str
    sku_name: str
    product_name: str
    effective_start: str

    @property
    def price_per_unit(self) -> float:
        return self.unit_price / unit_multiplier(self.unit_of_measure)


class PriceClient:
    """Queries the unauthenticated retail price catalogue, with an on-disk cache.

    The cache is a *replay* mechanism, not an accelerator. It is read only in
    `--offline` mode, so a live run always reflects today's catalogue: a budget
    gate that silently reports month-old prices as current is worse than one
    that fails loudly when the API is unreachable. Live runs still write the
    cache, so `--offline` can replay the last good snapshot deliberately.
    """

    def __init__(
        self,
        *,
        currency: str = "USD",
        cache_path: Path | None = None,
        offline: bool = False,
        session: requests.Session | None = None,
        max_retries: int = 6,
        backoff_seconds: float = 1.5,
        request_timeout: float = 20.0,
        deadline_seconds: float = 120.0,
    ) -> None:
        self.currency = currency
        self.cache_path = cache_path
        self.offline = offline
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.request_timeout = request_timeout
        self.deadline_seconds = deadline_seconds
        self._session = session or requests.Session()
        #: Doubles as an in-run memo so one filter is fetched at most once.
        self._cache: dict[str, list[dict[str, Any]]] = {}
        if offline and cache_path and cache_path.exists():
            self._cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def query(self, odata_filter: str) -> list[dict[str, Any]]:
        key = f"{self.currency}|{odata_filter}"
        if key in self._cache:
            return self._cache[key]
        if self.offline:
            raise CostModelError(f"offline mode and no cached price for filter: {odata_filter}")

        items: list[dict[str, Any]] = []
        params = {
            "api-version": RETAIL_PRICES_API_VERSION,
            "currencyCode": f"'{self.currency}'",
            "$filter": odata_filter,
        }
        url = f"{RETAIL_PRICES_ENDPOINT}?{urllib.parse.urlencode(params)}"
        seen_pages = 0
        while url and seen_pages < 20:
            payload = self._get(url)
            items.extend(payload.get("Items", []))
            url = payload.get("NextPageLink")
            seen_pages += 1

        self._cache[key] = items
        return items

    def _get(self, url: str) -> dict[str, Any]:
        """GET with bounded backoff.

        The Retail Prices API is unauthenticated and rate limited, and a full
        cost run issues one request per line item. A 429 is an expected part of
        normal operation, not an error, so it is retried rather than surfaced.

        Retries are bounded by a wall-clock deadline as well as an attempt count.
        Without it, a sustained throttle multiplies per-request timeouts by
        retries by line items, and the budget gate stops being a gate: it hangs
        until the CI runner kills it, which reads as infrastructure flakiness
        rather than the clear failure it is.
        """
        last_error: Exception | None = None
        started = time.monotonic()

        def remaining() -> float:
            return self.deadline_seconds - (time.monotonic() - started)

        for attempt in range(self.max_retries):
            if remaining() <= 0:
                break
            try:
                response = self._session.get(url, timeout=min(self.request_timeout, max(remaining(), 1.0)))
            except requests.RequestException as exc:  # transient network fault
                last_error = exc
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    response.raise_for_status()
                    return response.json()
                last_error = CostModelError(f"HTTP {response.status_code} from the Retail Prices API")
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    delay = min(float(retry_after), 60.0, max(remaining(), 0.0))
                    if delay > 0:
                        time.sleep(delay)
                    continue
            delay = min(self.backoff_seconds * (2**attempt), 30.0, max(remaining(), 0.0))
            if delay > 0:
                time.sleep(delay)

        raise CostModelError(
            f"the Azure Retail Prices API did not respond successfully within "
            f"{self.deadline_seconds:.0f}s / {self.max_retries} attempts: {last_error}"
        )

    def save_cache(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_filter(meter: dict[str, Any], region: str) -> str:
    """Translate a meter spec from YAML into an OData filter."""
    clauses = [f"armRegionName eq {_quote(region)}"]
    for field_name in (
        "serviceName",
        "serviceFamily",
        "productName",
        "skuName",
        "meterName",
        "armSkuName",
        "priceType",
    ):
        if meter.get(field_name):
            clauses.append(f"{field_name} eq {_quote(str(meter[field_name]))}")
    if not meter.get("priceType"):
        clauses.append("priceType eq 'Consumption'")
    return " and ".join(clauses)


def resolve_tiers(client: PriceClient, meter: dict[str, Any], region: str) -> list[PriceTier]:
    """Return the price tiers for a meter, newest effective date only.

    Raises when nothing matches, or when the spec is loose enough to match more
    than one distinct meter — an ambiguous match is a modelling bug, not a
    rounding error, so it fails loudly with the candidates listed.
    """
    items = client.query(build_filter(meter, region))
    if not items:
        raise CostModelError(
            f"no retail price matched meter {meter!r} in region {region!r}. "
            "Check serviceName/meterName spelling against the Retail Prices API."
        )

    contains = meter.get("meterNameContains")
    if contains:
        items = [i for i in items if contains.lower() in str(i.get("meterName", "")).lower()]
        if not items:
            raise CostModelError(f"meterNameContains={contains!r} eliminated every match in {region!r}")

    if meter.get("reservationTerm") is None:
        items = [i for i in items if not i.get("reservationTerm")]

    distinct_meters = {i.get("meterId") for i in items}
    if len(distinct_meters) > 1:
        candidates = sorted({f"{i.get('meterName')} / {i.get('skuName')}" for i in items})
        raise CostModelError(
            f"meter spec {meter!r} is ambiguous in {region!r}; it matched "
            f"{len(distinct_meters)} meters: {candidates}. Narrow it with skuName or meterName."
        )

    latest = max(str(i.get("effectiveStartDate") or "") for i in items)
    tiers = [
        PriceTier(
            minimum_units=float(i.get("tierMinimumUnits") or 0),
            unit_price=float(i.get("retailPrice") or 0),
            unit_of_measure=str(i.get("unitOfMeasure") or ""),
            meter_id=str(i.get("meterId") or ""),
            meter_name=str(i.get("meterName") or ""),
            sku_name=str(i.get("skuName") or ""),
            product_name=str(i.get("productName") or ""),
            effective_start=str(i.get("effectiveStartDate") or ""),
        )
        for i in items
        if str(i.get("effectiveStartDate") or "") == latest
    ]
    return sorted(tiers, key=lambda t: t.minimum_units)


def cost_for_quantity(tiers: list[PriceTier], quantity: float) -> float:
    """Apply Azure's graduated tiers marginally.

    ``quantity`` is expressed in the meter's *base* unit (individual calls,
    individual tokens, seconds). ``tierMinimumUnits`` from the API is expressed in
    the meter's *billing* unit, so a "10K" meter with ``tierMinimumUnits: 1000``
    means "beyond 10,000,000 calls". Both are converted to base units before they
    are compared, otherwise a tiered meter silently prices almost everything into
    its cheapest band.

    A ``tierMinimumUnits`` of N means "units beyond N are priced at this rate", so
    the first tier covers 0..next_minimum, and so on. A single-tier meter reduces
    to ``quantity * price_per_unit``.
    """
    if quantity <= 0:
        return 0.0

    def floor_in_base_units(tier: PriceTier) -> float:
        return tier.minimum_units * unit_multiplier(tier.unit_of_measure)

    total = 0.0
    for index, tier in enumerate(tiers):
        lower = floor_in_base_units(tier)
        upper = floor_in_base_units(tiers[index + 1]) if index + 1 < len(tiers) else float("inf")
        if quantity <= lower:
            break
        billable = min(quantity, upper) - lower
        total += billable * tier.price_per_unit
    return total


@dataclass
class LineItemResult:
    id: str
    component: str
    billing: str
    region: str
    quantity: float
    unit: str
    monthly_cost: float
    meter_name: str
    sku_name: str
    unit_of_measure: str
    effective_start: str
    source: str
    notes: str = ""


@dataclass
class CostReport:
    generated_at: str
    currency: str
    region: str
    budget_usd: float
    azure_items: list[LineItemResult] = field(default_factory=list)
    external_items: list[LineItemResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def azure_monthly_total(self) -> float:
        return sum(i.monthly_cost for i in self.azure_items)

    @property
    def external_monthly_total(self) -> float:
        return sum(i.monthly_cost for i in self.external_items)

    @property
    def within_budget(self) -> bool:
        return self.azure_monthly_total <= self.budget_usd


def load_estimate(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "line_items" not in data:
        raise CostModelError(f"{path} must be a mapping containing 'line_items'")
    return data


def evaluate(
    estimate: dict[str, Any],
    *,
    region: str,
    client: PriceClient,
    budget_usd: float | None = None,
) -> CostReport:
    """Price every line item and report the total against the budget.

    The ceiling comes from the estimate itself unless a caller overrides it, so
    the number a reviewer reads in `costs/v1-estimate.yaml` is the number that
    is actually enforced.
    """
    if budget_usd is None:
        budget_usd = float(estimate.get("monthly_budget_usd", DEFAULT_BUDGET_USD))

    # The ceiling is denominated in the estimate's currency. Comparing a EUR
    # total against a USD ceiling would understate the spend by roughly the
    # exchange rate, so the mismatch is fatal rather than a warning.
    declared_currency = str(estimate.get("currency", "USD")).upper()
    if client.currency.upper() != declared_currency:
        raise CostModelError(
            f"estimate is denominated in {declared_currency} but prices were queried in "
            f"{client.currency.upper()}. Refusing to compare a total to a ceiling in another currency."
        )

    report = CostReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        currency=client.currency,
        region=region,
        budget_usd=budget_usd,
    )

    for raw in estimate["line_items"]:
        item_region = raw.get("region", region)
        quantity = float(raw["quantity"])
        billing = raw.get("billing", "azure")
        if billing not in VALID_BILLING:
            # A typo such as `azuer` used to fall through to the external branch,
            # quietly moving a billable Azure meter outside the ceiling it is
            # supposed to be tested against.
            raise CostModelError(
                f"line item {raw['id']!r}: billing must be one of "
                f"{sorted(VALID_BILLING)}, got {billing!r}"
            )

        if billing != "azure":
            # Priced from a published list price recorded in the YAML, because it
            # is not an Azure meter and therefore not in the Retail Prices API.
            unit_price = float(raw["list_price_usd"])
            report.external_items.append(
                LineItemResult(
                    id=raw["id"],
                    component=raw["component"],
                    billing=billing,
                    region="n/a",
                    quantity=quantity,
                    unit=raw.get("unit", ""),
                    monthly_cost=unit_price * quantity,
                    meter_name="(not an Azure meter)",
                    sku_name=raw.get("sku", ""),
                    unit_of_measure=raw.get("unit", ""),
                    effective_start=raw.get("price_as_of", ""),
                    source=raw.get("source", ""),
                    notes=raw.get("notes", ""),
                )
            )
            continue

        if raw.get("pricing_status") == "unpublished":
            # Some services are billable but have no meter in the retail catalogue
            # yet. Pretending that costs $0 would quietly weaken the gate, so the
            # estimate must reserve budget for it instead, and we say so loudly.
            reserve = raw.get("budget_reserve_usd")
            if reserve is None:
                raise CostModelError(
                    f"line item {raw['id']!r} is marked pricing_status: unpublished but declares no "
                    "budget_reserve_usd. An unpriceable component must reserve budget, not cost nothing."
                )
            report.warnings.append(
                f"{raw['component']}: no published retail meter; holding a "
                f"${float(reserve):,.2f}/month reserve against the ceiling."
            )
            report.azure_items.append(
                LineItemResult(
                    id=raw["id"],
                    component=raw["component"],
                    billing="azure",
                    region=item_region,
                    quantity=quantity,
                    unit=raw.get("unit", ""),
                    monthly_cost=float(reserve),
                    meter_name="(no published meter — budget reserve)",
                    sku_name=raw.get("sku", ""),
                    unit_of_measure=raw.get("unit", ""),
                    effective_start="",
                    source=raw.get("source", ""),
                    notes=raw.get("notes", ""),
                )
            )
            continue

        tiers = resolve_tiers(client, raw["meter"], item_region)
        expected = raw.get("expect_unit_of_measure")
        actual = tiers[0].unit_of_measure
        if expected and expected != actual:
            raise CostModelError(
                f"line item {raw['id']!r}: expected unitOfMeasure {expected!r} but the meter "
                f"now reports {actual!r}. The meter changed shape — re-check the quantity."
            )

        report.azure_items.append(
            LineItemResult(
                id=raw["id"],
                component=raw["component"],
                billing="azure",
                region=item_region,
                quantity=quantity,
                unit=raw.get("unit", actual),
                monthly_cost=cost_for_quantity(tiers, quantity),
                meter_name=tiers[0].meter_name,
                sku_name=tiers[0].sku_name,
                unit_of_measure=actual,
                effective_start=tiers[0].effective_start,
                source=raw.get("source", ""),
                notes=raw.get("notes", ""),
            )
        )

    return report


def price_basket(
    basket: list[dict[str, Any]],
    *,
    region: str,
    client: PriceClient,
) -> tuple[float, list[str]]:
    """Price a small comparison basket used to rank regions by cost.

    Returns the monthly total and any per-meter problems. A region that cannot be
    priced is not eliminated here — that is the capability gates' job — but it
    sorts last so it never wins on a missing number.
    """
    total = 0.0
    problems: list[str] = []
    for entry in basket:
        try:
            tiers = resolve_tiers(client, entry["meter"], region)
            total += cost_for_quantity(tiers, float(entry["quantity"]))
        except (CostModelError, requests.RequestException) as exc:
            problems.append(f"{entry.get('id', '?')}: {exc}")
    return total, problems


def render_markdown(report: CostReport) -> str:
    verdict = "PASS" if report.within_budget else "FAIL"
    lines = [
        "# v1 cost model",
        "",
        f"Generated: `{report.generated_at}`  ",
        f"Region: `{report.region}`  ",
        f"Currency: `{report.currency}`  ",
        f"Prices: live from the [Azure Retail Prices API]({RETAIL_PRICES_ENDPOINT}) "
        f"(`api-version={RETAIL_PRICES_API_VERSION}`)",
        "",
        f"## Azure incremental spend — **{verdict}**",
        "",
        f"**${report.azure_monthly_total:,.2f} / month** against a ceiling of "
        f"**${report.budget_usd:,.2f} / month**.",
        "",
        "| Component | Meter | Quantity | Unit | Monthly |",
        "| --- | --- | ---: | --- | ---: |",
    ]
    for item in sorted(report.azure_items, key=lambda i: -i.monthly_cost):
        lines.append(
            f"| {item.component} | `{item.meter_name}` | {item.quantity:,.0f} | "
            f"{item.unit_of_measure} | ${item.monthly_cost:,.2f} |"
        )
    lines.append(
        f"| **Total** | | | | **${report.azure_monthly_total:,.2f}** |"
    )

    lines += [
        "",
        "## Non-Azure capacity (verified separately, outside the Azure ceiling)",
        "",
        "| Component | Quantity | Unit | Monthly | Source |",
        "| --- | ---: | --- | ---: | --- |",
    ]
    if report.external_items:
        for item in report.external_items:
            lines.append(
                f"| {item.component} | {item.quantity:,.0f} | {item.unit} | "
                f"${item.monthly_cost:,.2f} | {item.source} |"
            )
        lines.append(f"| **Total** | | | **${report.external_monthly_total:,.2f}** | |")
    else:
        lines.append("| _none_ | | | | |")

    if report.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in report.warnings]

    lines += [
        "",
        "## Notes",
        "",
    ]
    for item in report.azure_items + report.external_items:
        if item.notes:
            lines.append(f"- **{item.component}** — {item.notes}")
    return "\n".join(lines) + "\n"


def budget_from_env(default: float | None = None) -> float | None:
    """Read a budget override from the environment.

    Returns ``None`` when unset so that `evaluate` falls back to the ceiling
    declared in the estimate, rather than a constant that silently overrides it.
    """
    raw = os.environ.get("FOUNDRY_MONTHLY_BUDGET_USD")
    return float(raw) if raw else default
