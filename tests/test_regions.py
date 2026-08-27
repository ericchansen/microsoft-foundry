"""Region selection: the gates, the ranking order, and the refusals.

These are pure-function tests. Every Azure probe is injected or monkeypatched,
so nothing here touches the network or a subscription.

The refusal cases matter more than the happy path. A region gate that quietly
downgrades a *failed probe* into a *negative result* produces a confident,
well-formatted, wrong answer -- which is worse than an error, because nobody
re-runs it.
"""

from __future__ import annotations

import pytest

from contoso_foundry import regions


def make_region(name: str, **overrides) -> regions.Region:
    defaults = dict(
        display_name=name,
        geography="United States",
        geography_group="US",
        physical_location="Illinois",
        latitude=41.9,
        longitude=-87.6,
        paired_regions=["southcentralus"],
        availability_zones=3,
    )
    defaults.update(overrides)
    return regions.Region(name=name, **defaults)


def test_optional_region_requirements_are_added_only_when_enabled(config_dir) -> None:
    config = regions.load_config(config_dir / "region-requirements.yaml")
    assert "azure_sre_agent" not in config["capabilities"]["required"]
    assert all(item["type"] != "workflows" for item in config["resource_types"])

    enabled = regions.enable_optional_modules(config, {"optional-control-plane"})
    assert {"azure_sre_agent", "logic_apps_agent_loop"} <= set(enabled["capabilities"]["required"])
    assert {"agents", "workflows"} <= {item["type"] for item in enabled["resource_types"]}


class TestResidencyGate:
    def test_blocks_an_explicitly_blocked_geography(self):
        r = make_region("germanywestcentral", geography="Germany", geography_group="Europe")
        regions.gate_residency([r], {"residency": {"blocked_geographies": ["germany"]}})
        assert r.eliminated
        assert "residency" in r.eliminated_by

    def test_blocks_a_geography_group_outside_the_allowed_set(self):
        r = make_region("japaneast", geography="Japan", geography_group="Asia Pacific")
        regions.gate_residency([r], {"residency": {"allowed_geography_groups": ["US"]}})
        assert r.eliminated

    def test_admits_an_allowed_region(self):
        r = make_region("northcentralus")
        regions.gate_residency([r], {"residency": {"allowed_geography_groups": ["US"]}})
        assert not r.eliminated

    def test_no_rules_admits_everything(self):
        r = make_region("northcentralus")
        regions.gate_residency([r], {})
        assert not r.eliminated


class TestReliabilityGate:
    def test_requires_a_paired_region_when_configured(self):
        r = make_region("westus3", paired_regions=[])
        regions.gate_reliability([r], {"reliability": {"require_paired_region": True}})
        assert r.eliminated

    def test_enforces_a_minimum_zone_count(self):
        r = make_region("northcentralus", availability_zones=0)
        regions.gate_reliability(
            [r], {"reliability": {"require_paired_region": False, "minimum_availability_zones": 3}}
        )
        assert r.eliminated


class TestModelAndQuotaGate:
    """A failed probe must abort, not eliminate."""

    def test_probe_failure_aborts_instead_of_eliminating(self, monkeypatch):
        monkeypatch.setattr(regions, "_models_in_region", lambda region: None)
        monkeypatch.setattr(regions, "_usage_in_region", lambda region: [])
        config = {
            "models": {"required": [{"id": "frontier-chat", "name_pattern": "^gpt-5"}]},
        }

        with pytest.raises(regions.ProbeFailedError):
            regions.gate_models_and_quota([make_region("northcentralus")], config, workers=1)

    def test_a_genuinely_absent_model_eliminates(self, monkeypatch):
        monkeypatch.setattr(regions, "_models_in_region", lambda region: [])
        monkeypatch.setattr(regions, "_usage_in_region", lambda region: [])
        config = {
            "models": {"required": [{"id": "frontier-chat", "name_pattern": "^gpt-5"}]},
        }

        r = make_region("northcentralus")
        regions.gate_models_and_quota([r], config, workers=1)

        assert r.eliminated, "an empty model list is a real answer and should eliminate"


class _StubClient:
    """Stands in for costs.PriceClient without touching the network."""


class TestRanking:
    def _config(self):
        return {"cost_basket": [{"id": "stub"}]}

    def test_orders_by_cost_then_quota_then_distance(self, monkeypatch):
        prices = {"cheap": 100.0, "mid": 200.0, "dear": 300.0}
        monkeypatch.setattr(
            regions.costs, "price_basket", lambda basket, region, client: (prices[region], [])
        )
        rs = [make_region("dear"), make_region("cheap"), make_region("mid")]

        ranked = regions.rank(rs, self._config(), _StubClient())

        assert [r.name for r in ranked] == ["cheap", "mid", "dear"]

    def test_quota_breaks_a_cost_tie(self, monkeypatch):
        monkeypatch.setattr(
            regions.costs, "price_basket", lambda basket, region, client: (100.0, [])
        )
        low = make_region("low")
        low.quota_headroom = 10
        high = make_region("high")
        high.quota_headroom = 500

        ranked = regions.rank([low, high], self._config(), _StubClient())

        assert [r.name for r in ranked] == ["high", "low"]

    def test_distance_breaks_a_cost_and_quota_tie(self, monkeypatch):
        monkeypatch.setattr(
            regions.costs, "price_basket", lambda basket, region, client: (100.0, [])
        )
        near = make_region("near")
        near.distance_km = 0.0
        far = make_region("far")
        far.distance_km = 5000.0

        ranked = regions.rank([near, far], self._config(), _StubClient())

        assert [r.name for r in ranked] == ["near", "far"]

    def test_an_unpriceable_region_sorts_last(self, monkeypatch):
        def price(basket, region, client):
            return (0.0, ["no meter"]) if region == "unpriced" else (900.0, [])

        monkeypatch.setattr(regions.costs, "price_basket", price)

        ranked = regions.rank(
            [make_region("unpriced"), make_region("expensive")], self._config(), _StubClient()
        )

        assert [r.name for r in ranked] == ["expensive", "unpriced"]

    def test_refuses_to_rank_when_nothing_can_be_priced(self, monkeypatch):
        """A pricing outage must not silently become a region decision."""
        monkeypatch.setattr(
            regions.costs, "price_basket", lambda basket, region, client: (0.0, ["API unreachable"])
        )

        with pytest.raises(regions.ProbeFailedError, match="no cost signal"):
            regions.rank(
                [make_region("a"), make_region("b")], self._config(), _StubClient()
            )

    def test_eliminated_regions_are_never_ranked(self, monkeypatch):
        monkeypatch.setattr(
            regions.costs, "price_basket", lambda basket, region, client: (100.0, [])
        )
        out = make_region("out")
        out.record("residency", False, "blocked", "test")

        ranked = regions.rank([out, make_region("in")], self._config(), _StubClient())

        assert [r.name for r in ranked] == ["in"]
