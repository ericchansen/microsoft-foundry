"""The budget gate.

The subtle failures a cost model can have are all here: an ambiguous meter that
silently resolves to the wrong price, tier minimums compared in the wrong unit, a
unit-of-measure change that moves the answer by a factor of a thousand, and a
component with no meter being recorded as free.
"""

from __future__ import annotations

import json
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

    def test_supports_a_narrow_meter_name_contains_filter(self):
        client = FakeClient([price_item(meterName="5.4 mini Inp Gl")])
        costs.resolve_tiers(
            client,
            {"serviceName": "Foundry Models", "meterNameContains": "5.4 mini Inp Gl"},
            "northcentralus",
        )
        assert "contains(meterName, '5.4 mini Inp Gl')" in client.filters[0]


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

    def test_disabled_optional_module_is_not_priced_or_counted(self):
        estimate = {
            "monthly_budget_usd": 100,
            "modules": {
                "core": {"enabled_by_default": True},
                "optional": {"enabled_by_default": False},
            },
            "line_items": [
                self.azure_line(module="optional"),
            ],
        }
        client = FakeClient([price_item()])
        report = costs.evaluate(estimate, region="r", client=client)
        assert report.azure_monthly_total == 0
        assert [item.module for item in report.excluded_items] == ["optional"]
        assert client.filters == []

    def test_enabling_optional_module_prices_and_gates_it(self):
        estimate = {
            "monthly_budget_usd": 100,
            "modules": {
                "core": {"enabled_by_default": True},
                "optional": {"enabled_by_default": False},
            },
            "line_items": [
                self.azure_line(module="optional"),
            ],
        }
        report = costs.evaluate(
            estimate,
            region="r",
            client=FakeClient([price_item()]),
            enable_modules={"optional"},
        )
        assert report.azure_monthly_total == pytest.approx(150.0004)
        assert not report.within_budget
        assert report.excluded_items == []

    def test_unknown_optional_module_is_rejected(self):
        estimate = {
            "monthly_budget_usd": 100,
            "modules": {"core": {"enabled_by_default": True}},
            "line_items": [],
        }
        with pytest.raises(costs.CostModelError, match="unknown optional"):
            costs.evaluate(
                estimate,
                region="r",
                client=FakeClient([]),
                enable_modules={"missing"},
            )


class TestShippedEstimate:
    """The committed estimate must stay structurally valid without pricing it."""

    @pytest.fixture(scope="class")
    def estimate(self, repo_root):
        path = repo_root / "costs" / "v1-estimate.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_declares_a_positive_sample_policy_ceiling(self, estimate):
        assert estimate["monthly_budget_usd"] > 0

    def test_optional_modules_are_disabled_by_default(self, estimate):
        assert estimate["modules"]["core"]["enabled_by_default"] is True
        assert estimate["modules"]["approvals"]["enabled_by_default"] is False
        assert estimate["modules"]["sre"]["enabled_by_default"] is False

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

    def test_research_uses_only_its_estate_token_allocation(self, estimate):
        by_id = {item["id"]: item for item in estimate["line_items"]}

        assert by_id["contoso-research-input"]["quantity"] == 3_000_000
        assert by_id["contoso-research-output"]["quantity"] == 600_000
        assert by_id["contoso-research-hosted-compute"]["budget_reserve_usd"] == 30
        assert not any(
            "research" in item["id"] and "registry" in item["id"]
            for item in estimate["line_items"]
        )

    def test_line_item_ids_are_unique(self, estimate):
        ids = [i["id"] for i in estimate["line_items"]]
        assert len(ids) == len(set(ids))

    def test_shared_fixed_resources_are_counted_once(self, estimate):
        items = estimate["line_items"]
        assert sum(item["id"] == "apim-basic-v2-unit" for item in items) == 1
        assert sum("Container Registry" in item["component"] for item in items) == 1
        assert sum(item["id"] == "log-analytics-ingestion" for item in items) == 1
        assert {item["id"] for item in items if item["id"].startswith("aca-")} == {
            "aca-memory",
            "aca-requests",
            "aca-vcpu",
        }

    def test_logic_apps_inference_is_separate_from_orchestration(self, estimate):
        items = {item["id"]: item for item in estimate["line_items"]}
        assert all(
            items[item_id]["module"] == "approvals"
            for item_id in (
                "foundry-gpt5-input",
                "foundry-gpt5-output",
                "logic-apps-agent-input",
                "logic-apps-agent-output",
                "logic-apps-connector-actions",
            )
        )
        assert items["foundry-gpt5-input"]["quantity"] == 20_000_000
        assert items["foundry-gpt5-output"]["quantity"] == 2_500_000
        assert items["logic-apps-agent-input"]["quantity"] == 10_000_000
        assert items["logic-apps-agent-output"]["quantity"] == 2_000_000
        assert items["sre-agent"]["module"] == "sre"

    def test_travel_model_is_budgeted_without_shared_registry(self, estimate):
        ids = {item["id"] for item in estimate["line_items"]}
        assert {"travel-gpt-5-4-mini-input", "travel-gpt-5-4-mini-output"} <= ids
        assert "acr-basic" not in ids
        quantities = {item["id"]: item["quantity"] for item in estimate["line_items"]}
        assert quantities["travel-gpt-5-4-mini-input"] == 6_000_000
        assert quantities["travel-gpt-5-4-mini-output"] == 1_500_000

    def test_support_reuses_platform_registry_and_has_bounded_token_allocation(self, estimate):
        items = {item["id"]: item for item in estimate["line_items"]}
        assert "shared-container-registry" not in items
        assert items["support-model-input"]["quantity"] == 1_500_000
        assert items["support-model-output"]["quantity"] == 300_000
        assert "support-private-acr" not in items
        assert "support-acr-private-endpoint" not in items
        assert "support-acr-private-dns" not in items

    def test_field_uses_its_sparse_estate_share_without_duplicate_aca(self, estimate):
        items = {item["id"]: item for item in estimate["line_items"]}
        assert items["field-model-input"]["quantity"] == 1_000_000
        assert items["field-model-output"]["quantity"] == 200_000
        assert "4 conversations" in items["field-model-input"]["notes"]
        assert not any(item_id.startswith("field-aca") for item_id in items)


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


class _CountingSession:
    """Records how many live calls escape the cache."""

    def __init__(self, items):
        self.items = items
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return _Response(200, {"Items": self.items, "NextPageLink": None})


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class TestCacheIsOfflineOnly:
    """A cached price presented as a live price is a silent staleness bug."""

    def _cache(self, tmp_path):
        path = tmp_path / "price-cache.json"
        path.write_text(
            json.dumps(
                {"USD|serviceName eq 'API Management'": [price_item(retailPrice=999.0)]}
            ),
            encoding="utf-8",
        )
        return path

    def test_a_live_run_ignores_the_cache_on_disk(self, tmp_path):
        session = _CountingSession([price_item(retailPrice=1.0)])
        client = costs.PriceClient(
            session=session, cache_path=self._cache(tmp_path), offline=False
        )

        items = client.query("serviceName eq 'API Management'")

        assert session.calls == 1, "a live run must query the API, not replay the cache"
        assert items[0]["retailPrice"] == 1.0

    def test_an_offline_run_uses_the_cache(self, tmp_path):
        session = _CountingSession([])
        client = costs.PriceClient(
            session=session, cache_path=self._cache(tmp_path), offline=True
        )

        items = client.query("serviceName eq 'API Management'")

        assert session.calls == 0
        assert items[0]["retailPrice"] == 999.0


class TestBillingValidation:
    """An unrecognised billing tag must not route spend around the ceiling."""

    def test_a_typo_is_rejected_rather_than_treated_as_external(self):
        estimate = {
            "monthly_budget_usd": 500,
            "line_items": [
                {
                    "id": "apim",
                    "component": "API Management",
                    "billing": "azuer",
                    "quantity": 730,
                    "unit": "hours",
                    "meter": {"serviceName": "API Management", "meterName": "Basic v2 Unit"},
                }
            ],
        }

        with pytest.raises(costs.CostModelError, match="billing"):
            costs.evaluate(estimate, region="r", client=FakeClient([price_item()]), budget_usd=500)
