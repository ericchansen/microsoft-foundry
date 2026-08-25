"""The budget gate.

The subtle failures a cost model can have are all here: an ambiguous meter that
silently resolves to the wrong price, tier minimums compared in the wrong unit, a
unit-of-measure change that moves the answer by a factor of a thousand, and a
component with no meter being recorded as free.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from contoso_foundry import costs


class FakeClient:
    """Stands in for the Retail Prices API. No network."""

    currency = "USD"

    def __init__(self, items: list[dict[str, Any]]):
        self.items = items
        self.filters: list[str] = []

    def query(self, odata_filter: str) -> list[dict[str, Any]]:
        self.filters.append(odata_filter)
        return self.items


def price_item(**overrides: Any) -> dict[str, Any]:
    item = {
        "meterId": "meter-1",
        "meterName": "Basic v2 Unit",
        "skuName": "Basic v2",
        "productName": "API Management",
        "unitOfMeasure": "1 Hour",
        "retailPrice": 0.20548,
        "tierMinimumUnits": 0,
        "effectiveStartDate": "2026-01-01T00:00:00Z",
    }
    item.update(overrides)
    return item


class TestUnitMultiplier:
    @pytest.mark.parametrize(
        ("unit", "expected"),
        [("1 Hour", 1.0), ("1 Second", 1.0), ("1 GiB Second", 1.0),
         ("10K", 10_000.0), ("1M", 1_000_000.0), ("100", 100.0), ("1 GB/Month", 1.0)],
    )
    def test_parses_azure_units(self, unit: str, expected: float):
        assert costs.unit_multiplier(unit) == expected

    def test_rejects_an_uninterpretable_unit(self):
        with pytest.raises(costs.CostModelError):
            costs.unit_multiplier("per widget")

    def test_rejects_a_zero_unit(self):
        """Dividing by this would produce infinity and pass any budget check."""
        with pytest.raises(costs.CostModelError):
            costs.unit_multiplier("0 Hour")


class TestResolveTiers:
    def test_resolves_a_single_meter(self):
        client = FakeClient([price_item()])
        tiers = costs.resolve_tiers(client, {"serviceName": "API Management"}, "northcentralus")
        assert len(tiers) == 1
        assert tiers[0].price_per_unit == pytest.approx(0.20548)

    def test_raises_when_nothing_matches(self):
        with pytest.raises(costs.CostModelError, match="no retail price matched"):
            costs.resolve_tiers(FakeClient([]), {"serviceName": "Nope"}, "northcentralus")

    def test_raises_on_an_ambiguous_meter(self):
        """API Management publishes 'Basic v2 Unit' and 'Basic v2 Secondary Unit'
        at the same price. Filtering on SKU alone matches both, and would keep
        matching both if the prices ever diverged."""
        client = FakeClient([
            price_item(),
            price_item(meterId="meter-2", meterName="Basic v2 Secondary Unit"),
        ])
        with pytest.raises(costs.CostModelError, match="ambiguous"):
            costs.resolve_tiers(client, {"skuName": "Basic v2"}, "northcentralus")

    def test_keeps_only_the_current_price(self):
        client = FakeClient([
            price_item(retailPrice=0.10, effectiveStartDate="2024-01-01T00:00:00Z"),
            price_item(retailPrice=0.25, effectiveStartDate="2026-01-01T00:00:00Z"),
        ])
        tiers = costs.resolve_tiers(client, {"serviceName": "API Management"}, "northcentralus")
        assert [t.unit_price for t in tiers] == [0.25]

    def test_ignores_reservation_prices_by_default(self):
        client = FakeClient([
            price_item(),
            price_item(meterId="meter-2", reservationTerm="1 Year", retailPrice=0.01),
        ])
        tiers = costs.resolve_tiers(client, {"serviceName": "API Management"}, "northcentralus")
        assert [t.unit_price for t in tiers] == [0.20548]

    def test_builds_a_region_scoped_filter(self):
        client = FakeClient([price_item()])
        costs.resolve_tiers(client, {"serviceName": "API Management"}, "northcentralus")
        assert "armRegionName eq 'northcentralus'" in client.filters[0]
        assert "priceType eq 'Consumption'" in client.filters[0]


class TestGraduatedTiers:
    def test_single_tier_is_linear(self):
        tiers = costs.resolve_tiers(FakeClient([price_item()]), {}, "r")
        assert costs.cost_for_quantity(tiers, 730) == pytest.approx(150.0004)

    def test_tier_minimums_are_converted_to_base_units(self):
        """Regression: Azure reports tierMinimumUnits in the meter's *billing*
        unit while quantities are declared in its *base* unit. For a '10K' meter,
        tierMinimumUnits=1000 means "beyond ten million calls", not "beyond one
        thousand". Comparing them directly prices everything into the free band.
        """
        tiers = costs.resolve_tiers(
            FakeClient([
                price_item(meterName="Basic v2 Calls", unitOfMeasure="10K",
                           retailPrice=0.0, tierMinimumUnits=0),
                price_item(meterName="Basic v2 Calls", unitOfMeasure="10K",
                           retailPrice=0.03, tierMinimumUnits=1000),
            ]),
            {}, "r",
        )

        # Two million calls sit inside the zero band.
        assert costs.cost_for_quantity(tiers, 2_000_000) == pytest.approx(0.0)
        # Twenty million: the first ten million free, the next ten million charged.
        assert costs.cost_for_quantity(tiers, 20_000_000) == pytest.approx(30.0)

    def test_tiers_are_marginal_not_cliff_edged(self):
        tiers = costs.resolve_tiers(
            FakeClient([
                price_item(unitOfMeasure="1", retailPrice=1.0, tierMinimumUnits=0),
                price_item(unitOfMeasure="1", retailPrice=0.5, tierMinimumUnits=100),
            ]),
            {}, "r",
        )
        # 100 at $1 plus 50 at $0.50 — not 150 at $0.50.
        assert costs.cost_for_quantity(tiers, 150) == pytest.approx(125.0)

    def test_zero_quantity_costs_nothing(self):
        tiers = costs.resolve_tiers(FakeClient([price_item()]), {}, "r")
        assert costs.cost_for_quantity(tiers, 0) == 0.0


class TestEvaluate:
    def azure_line(self, **overrides: Any) -> dict[str, Any]:
        item = {
            "id": "apim",
            "component": "API Management",
            "billing": "azure",
            "quantity": 730,
            "unit": "hours",
            "meter": {"serviceName": "API Management", "meterName": "Basic v2 Unit"},
        }
        item.update(overrides)
        return item

    def test_passes_under_the_ceiling(self):
        estimate = {"monthly_budget_usd": 500, "line_items": [self.azure_line()]}
        report = costs.evaluate(
            estimate,
            region="r",
            client=FakeClient([price_item()]),
            budget_usd=estimate["monthly_budget_usd"],
        )
        assert report.within_budget
        assert report.azure_monthly_total == pytest.approx(150.0004)

    def test_fails_over_the_ceiling(self):
        estimate = {"monthly_budget_usd": 100, "line_items": [self.azure_line()]}
        report = costs.evaluate(
            estimate,
            region="r",
            client=FakeClient([price_item()]),
            budget_usd=estimate["monthly_budget_usd"],
        )
        assert not report.within_budget

    def test_detects_unit_of_measure_drift(self):
        """If Microsoft re-denominates a meter, fail loudly rather than silently
        changing the answer by a factor of a thousand."""
        estimate = {
            "monthly_budget_usd": 500,
            "line_items": [self.azure_line(expect_unit_of_measure="10K")],
        }
        with pytest.raises(costs.CostModelError, match="unitOfMeasure"):
            costs.evaluate(
                estimate,
                region="r",
                client=FakeClient([price_item()]),
                budget_usd=estimate["monthly_budget_usd"],
            )

    def test_unpriced_component_reserves_budget_rather_than_assuming_zero(self):
        estimate = {
            "monthly_budget_usd": 500,
            "line_items": [{
                "id": "sre", "component": "Azure SRE Agent", "billing": "azure",
                "pricing_status": "unpublished", "budget_reserve_usd": 60,
                "quantity": 730, "unit": "agent-hours",
            }],
        }
        report = costs.evaluate(
            estimate,
            region="r",
            client=FakeClient([]),
            budget_usd=estimate["monthly_budget_usd"],
        )
        assert report.azure_monthly_total == pytest.approx(60.0)
        assert any("no published retail meter" in w for w in report.warnings)

    def test_external_billing_is_excluded_from_the_azure_ceiling(self):
        """Copilot Studio bills in Copilot Credits, not Azure meters. Counting it
        against the Azure ceiling would make the gate wrong in both directions."""
        estimate = {
            "monthly_budget_usd": 100,
            "line_items": [
                self.azure_line(),
                {"id": "cs", "component": "Copilot Studio", "billing": "external",
                 "quantity": 25000, "unit": "credits/month", "list_price_usd": 0.01},
            ],
        }
        report = costs.evaluate(
            estimate,
            region="r",
            client=FakeClient([price_item()]),
            budget_usd=estimate["monthly_budget_usd"],
        )
        assert report.azure_monthly_total == pytest.approx(150.0004)
        assert report.external_monthly_total == pytest.approx(250.0)


class TestShippedEstimate:
    """The committed estimate must stay structurally valid without pricing it."""

    @pytest.fixture(scope="class")
    def estimate(self, repo_root):
        path = repo_root / "costs" / "v1-estimate.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_declares_the_five_hundred_dollar_ceiling(self, estimate):
        assert estimate["monthly_budget_usd"] == 500

    def test_every_line_item_is_sourced(self, estimate):
        for item in estimate["line_items"]:
            assert item.get("source", "").startswith("https://"), item["id"]

    def test_every_azure_line_is_priceable_or_reserved(self, estimate):
        for item in estimate["line_items"]:
            if item["billing"] != "azure":
                continue
            has_meter = "meter" in item
            reserved = item.get("pricing_status") == "unpublished" and "budget_reserve_usd" in item
            assert has_meter or reserved, f"{item['id']} is neither priceable nor reserved"

    def test_copilot_studio_is_billed_externally(self, estimate):
        """Gate 7: non-Azure capacity is verified separately from the Azure ceiling."""
        studio = [i for i in estimate["line_items"] if "copilot-studio" in i["id"]]
        assert studio, "the estimate must account for Copilot Studio capacity"
        assert all(i["billing"] == "external" for i in studio)

    def test_line_item_ids_are_unique(self, estimate):
        ids = [i["id"] for i in estimate["line_items"]]
        assert len(ids) == len(set(ids))

    def test_support_uses_one_shared_registry_and_bounded_token_allocation(self, estimate):
        items = {item["id"]: item for item in estimate["line_items"]}
        assert items["shared-container-registry"]["meter"]["skuName"] == "Basic"
        assert items["support-model-input"]["quantity"] == 1_500_000
        assert items["support-model-output"]["quantity"] == 300_000
        assert "support-private-acr" not in items
        assert "support-acr-private-endpoint" not in items
        assert "support-acr-private-dns" not in items


class _AlwaysThrottled:
    """A session that never stops returning 429, like a sustained rate limit."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, timeout: float | None = None) -> Any:  # noqa: ARG002
        self.calls += 1

        class _Response:
            status_code = 429
            headers: dict[str, str] = {}

        return _Response()


class TestRetryExhaustion:
    """The budget gate must fail loudly when pricing is unavailable.

    A hang is worse than a failure here: CI reads it as flakiness, and the
    ceiling silently stops being enforced for that run.
    """

    def test_gives_up_and_raises_rather_than_retrying_forever(self):
        session = _AlwaysThrottled()
        client = costs.PriceClient(
            session=session,
            max_retries=3,
            backoff_seconds=0.0,
            request_timeout=0.1,
            deadline_seconds=5.0,
        )

        with pytest.raises(costs.CostModelError, match="did not respond successfully"):
            client.query("serviceName eq 'API Management'")

        assert session.calls == 3

    def test_a_deadline_stops_retrying_before_the_attempt_budget(self):
        session = _AlwaysThrottled()
        client = costs.PriceClient(
            session=session,
            max_retries=100,
            backoff_seconds=0.0,
            request_timeout=0.1,
            deadline_seconds=0.0,
        )

        with pytest.raises(costs.CostModelError):
            client.query("serviceName eq 'API Management'")

        assert session.calls == 0
