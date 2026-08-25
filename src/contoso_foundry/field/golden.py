"""Deterministic golden scenarios over the canonical Contoso field-service data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GoldenScenario:
    name: str
    prompt: str
    tool_calls: tuple[tuple[str, dict[str, Any]], ...]
    expected_facts: tuple[str, ...]


SCENARIOS = (
    GoldenScenario(
        name="linked-work-order-context",
        prompt="Summarize WO-00010, including its customer, product, and site.",
        tool_calls=(
            ("operations_lookup_work_order", {"work_order_id": "WO-00010"}),
            ("customer_lookup", {"customer_id": "CUST-00058"}),
            ("catalog_lookup_product", {"product_id": "PROD-0092"}),
            ("operations_list_locations", {"country": "Singapore", "limit": 25}),
        ),
        expected_facts=(
            "Install additional access point in loading bay",
            "Juniper Reach Industries",
            "Contoso Compact Cable Tray H301",
            "Contoso Singapore Office",
        ),
    ),
    GoldenScenario(
        name="scope-boundary",
        prompt="Look up WO-00001 and do not broaden beyond my assigned regions.",
        tool_calls=(("operations_lookup_work_order", {"work_order_id": "WO-00001"}),),
        expected_facts=("Quarterly rack thermal inspection",),
    ),
)

__all__ = ["GoldenScenario", "SCENARIOS"]
