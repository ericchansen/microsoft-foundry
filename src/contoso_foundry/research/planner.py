"""Deterministic planning for the bounded Contoso research questions."""

from __future__ import annotations

import re
from typing import Any


class ResearchPlanError(ValueError):
    """Raised when a question cannot be mapped to a safe Toolbox plan."""


def _step(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments}


def plan_question(question: str) -> list[dict[str, Any]]:
    """Turn a natural-language question into explicit, reviewable tool calls."""
    text = " ".join(question.lower().split())
    if not text:
        raise ResearchPlanError("the research question is empty")

    customer_id = re.search(r"\bCUST-\d{5}\b", question, re.IGNORECASE)
    order_id = re.search(r"\bORD-\d{6}\b", question, re.IGNORECASE)
    invoice_id = re.search(r"\bINV-\d{6}\b", question, re.IGNORECASE)
    product_id = re.search(r"\bPROD-\d{5}\b", question, re.IGNORECASE)

    if invoice_id:
        return [_step("orders_lookup_invoice", invoice_id=invoice_id.group(0).upper())]
    if order_id:
        return [_step("orders_lookup_order", order_id=order_id.group(0).upper(), include_lines=True)]
    if product_id:
        return [
            _step("catalog_lookup_product", product_id=product_id.group(0).upper()),
            _step("catalog_check_stock", product_id=product_id.group(0).upper(), limit=50),
        ]

    filters: dict[str, Any] = {}
    if customer_id:
        filters["customer_id"] = customer_id.group(0).upper()

    if any(term in text for term in ("invoice", "receivable", "outstanding", "overdue")):
        statuses = ["overdue"] if "overdue" in text else ["issued", "part_paid", "overdue"]
        return [_step("orders_search_invoices", **filters, status=status, limit=50) for status in statuses]

    if any(term in text for term in ("stock", "inventory", "availability")):
        return [_step("catalog_check_stock", limit=100)]

    if any(term in text for term in ("order", "fulfilment", "fulfillment")):
        for status in ("cancelled", "placed", "picked", "shipped", "delivered"):
            if status in text:
                filters["status"] = status
                break
        return [_step("orders_search_orders", **filters, limit=50)]

    if any(term in text for term in ("customer", "account", "segment")):
        for segment in ("enterprise", "public sector", "mid-market", "small business"):
            if segment in text:
                filters["segment"] = segment.title()
                break
        return [_step("customer_search", **filters, limit=50)]

    raise ResearchPlanError(
        "the question is outside the supported Contoso research domains: "
        "customers, orders, invoices, products, and stock"
    )
