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
        prompt="Summarize WO-00015, including its customer, product, and site.",
        tool_calls=(
            ("operations_lookup_work_order", {"work_order_id": "WO-00015"}),
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
        name="scope-bound-record",
        prompt="Look up WO-00006 and do not broaden beyond my assigned regions.",
        tool_calls=(("operations_lookup_work_order", {"work_order_id": "WO-00006"}),),
        expected_facts=("Commission replacement edge appliance",),
    ),
)

__all__ = ["GoldenScenario", "SCENARIOS"]
