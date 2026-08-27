"""Referential and structural checks over a generated dataset.

These run against the in-memory dataset rather than against SQLite, deliberately.
SQLite's own ``PRAGMA foreign_key_check`` already runs during the build, but it
can only speak about the database that was successfully written. If a row is
malformed badly enough the insert fails first and the error names a constraint,
not the row. Checking the dataset directly produces a message a human can act on
and covers rules SQLite has no opinion about — identifier shape, roll-up
arithmetic, and date ordering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .model import SCHEMA, Table

Dataset = dict[str, list[dict[str, Any]]]

#: Canonical identifier shapes. Every value in a primary key column must match,
#: which is what stops an agent from inventing "customer 42" and getting a hit.
ID_PATTERNS = {
    "location_id": re.compile(r"^LOC-\d{3}$"),
    "supplier_id": re.compile(r"^SUP-\d{3}$"),
    "category_id": re.compile(r"^CAT-\d{2}$"),
    "product_id": re.compile(r"^PROD-\d{4}$"),
    "customer_id": re.compile(r"^CUST-\d{5}$"),
    "employee_id": re.compile(r"^EMP-\d{4}$"),
    "order_id": re.compile(r"^ORD-\d{6}$"),
    "order_line_id": re.compile(r"^OL-\d{6}-\d{2}$"),
    "invoice_id": re.compile(r"^INV-\d{6}$"),
    "case_id": re.compile(r"^CASE-\d{5}$"),
    "note_id": re.compile(r"^NOTE-\d{6}$"),
    "route_id": re.compile(r"^ROUTE-\d{4}$"),
    "fare_id": re.compile(r"^FARE-\d{5}$"),
    "policy_id": re.compile(r"^TPOL-(AMER|EMEA|APAC)-\d{2}$"),
    "hr_policy_id": re.compile(r"^HRP-(GLOBAL|AMER|EMEA|APAC)-\d{2}$"),
    "booking_id": re.compile(r"^TRIP-\d{5}$"),
    "work_order_id": re.compile(r"^WO-\d{5}$"),
    "principal_oid": re.compile(r"^OID-[A-Z0-9-]+$"),
}

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Money may drift by this much from the sum of its parts before it is a defect.
#: One cent per line, because each line is rounded independently.
ROLLUP_TOLERANCE = 0.05


@dataclass(frozen=True)
class Finding:
    table: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table}: {self.rule}: {self.detail}"


def _primary_key(table: Table) -> list[str]:
    return [column.name for column in table.columns if column.primary_key]


def check_primary_keys(dataset: Dataset) -> list[Finding]:
    findings: list[Finding] = []
    for table in SCHEMA:
        keys = _primary_key(table)
        if not keys:
            continue
        seen: set[tuple[str, ...]] = set()
        for row in dataset.get(table.name, []):
            value = tuple(str(row.get(key)) for key in keys)
            if any(part in ("None", "") for part in value):
                findings.append(Finding(table.name, "primary-key-null", f"{keys} -> {value}"))
            elif value in seen:
                findings.append(Finding(table.name, "primary-key-duplicate", f"{keys} -> {value}"))
            seen.add(value)
    return findings


def check_foreign_keys(dataset: Dataset) -> list[Finding]:
    findings: list[Finding] = []
    known: dict[str, set[str]] = {}

    for table in SCHEMA:
        for column in table.columns:
            if column.primary_key:
                known.setdefault(f"{table.name}.{column.name}", set()).update(
                    str(row.get(column.name)) for row in dataset.get(table.name, [])
                )

    for table in SCHEMA:
        for column in table.columns:
            if not column.references:
                continue
            targets = known.get(column.references)
            if targets is None:
                findings.append(
                    Finding(table.name, "foreign-key-target-missing",
                            f"{column.name} references {column.references}, which is not a primary key")
                )
                continue
            for row in dataset.get(table.name, []):
                value = row.get(column.name)
                if value is None:
                    if not column.nullable:
                        findings.append(
                            Finding(table.name, "foreign-key-null", f"{column.name} is NOT NULL but was empty")
                        )
                    continue
                if str(value) not in targets:
                    findings.append(
                        Finding(table.name, "foreign-key-dangling",
                                f"{column.name}={value!r} has no row in {column.references}")
                    )
    return findings


def check_not_null(dataset: Dataset) -> list[Finding]:
    findings: list[Finding] = []
    for table in SCHEMA:
        required = [c.name for c in table.columns if not c.nullable]
        for row in dataset.get(table.name, []):
            for name in required:
                if row.get(name) is None or row.get(name) == "":
                    findings.append(Finding(table.name, "not-null", f"{name} was empty"))
    return findings


def check_identifier_shapes(dataset: Dataset) -> list[Finding]:
    findings: list[Finding] = []
    for table in SCHEMA:
        for column in table.columns:
            pattern = ID_PATTERNS.get(column.name)
            if pattern is None:
                continue
            for row in dataset.get(table.name, []):
                value = row.get(column.name)
                if value is None:
                    continue
                if not pattern.fullmatch(str(value)):
                    findings.append(
                        Finding(table.name, "identifier-shape",
                                f"{column.name}={value!r} does not match {pattern.pattern}")
                    )
    return findings


def check_dates(dataset: Dataset) -> list[Finding]:
    """ISO shape everywhere, plus the orderings that carry meaning."""
    findings: list[Finding] = []
    from .model import DATE

    for table in SCHEMA:
        date_columns = [c.name for c in table.columns if c.classification == DATE]
        for row in dataset.get(table.name, []):
            for name in date_columns:
                value = row.get(name)
                if value is not None and not ISO_DATE.fullmatch(str(value)):
                    findings.append(Finding(table.name, "date-shape", f"{name}={value!r}"))

    for row in dataset.get("support_cases", []):
        if row.get("closed_on") and row["closed_on"] < row["opened_on"]:
            findings.append(Finding("support_cases", "date-order",
                                    f"{row['case_id']} closed before it opened"))
    for row in dataset.get("invoices", []):
        if row["due_on"] < row["issued_on"]:
            findings.append(Finding("invoices", "date-order", f"{row['invoice_id']} due before issue"))
    for row in dataset.get("travel_bookings", []):
        if row.get("return_on") and row["return_on"] < row["depart_on"]:
            findings.append(Finding("travel_bookings", "date-order",
                                    f"{row['booking_id']} returns before departure"))
    for row in dataset.get("work_orders", []):
        if row.get("completed_on") and row["completed_on"] < row["opened_on"]:
            findings.append(Finding("work_orders", "date-order",
                                    f"{row['work_order_id']} completed before it was raised"))
    return findings


def check_rollups(dataset: Dataset) -> list[Finding]:
    """Order totals must equal their lines, and payments must not exceed the bill.

    This is the check that catches a generator refactor most reliably. Foreign
    keys survive almost any reordering; arithmetic does not.
    """
    findings: list[Finding] = []

    totals: dict[str, float] = {}
    for line in dataset.get("order_lines", []):
        totals[line["order_id"]] = totals.get(line["order_id"], 0.0) + float(line["line_total"])
        expected = round(float(line["unit_price"]) * int(line["quantity"]), 2)
        if abs(expected - float(line["line_total"])) > 0.011:
            findings.append(Finding("order_lines", "line-total",
                                    f"{line['order_line_id']}: {line['line_total']} != {expected}"))

    for order in dataset.get("orders", []):
        summed = totals.get(order["order_id"])
        if summed is None:
            findings.append(Finding("orders", "orphan-order", f"{order['order_id']} has no lines"))
        elif abs(summed - float(order["order_total"])) > ROLLUP_TOLERANCE:
            findings.append(Finding("orders", "order-total",
                                    f"{order['order_id']}: {order['order_total']} != {round(summed, 2)}"))

    for invoice in dataset.get("invoices", []):
        if float(invoice["amount_paid"]) > float(invoice["amount_due"]) + 0.011:
            findings.append(Finding("invoices", "overpaid", invoice["invoice_id"]))

    cancelled = {o["order_id"] for o in dataset.get("orders", []) if o["status"] == "cancelled"}
    for invoice in dataset.get("invoices", []):
        if invoice["order_id"] in cancelled:
            findings.append(Finding("invoices", "invoiced-cancelled-order", invoice["invoice_id"]))
    return findings


def check_hierarchy(dataset: Dataset) -> list[Finding]:
    """Exactly one root, no cycles, and no manager outside the tree."""
    findings: list[Finding] = []
    employees = dataset.get("employees", [])
    managers = {row["employee_id"]: row.get("manager_id") for row in employees}

    roots = [eid for eid, manager in managers.items() if manager is None]
    if len(roots) != 1:
        findings.append(Finding("employees", "hierarchy-roots",
                                f"expected exactly one root, found {len(roots)}"))

    for employee_id in managers:
        seen: set[str] = set()
        cursor: str | None = employee_id
        while cursor is not None:
            if cursor in seen:
                findings.append(Finding("employees", "hierarchy-cycle", f"cycle reached from {employee_id}"))
                break
            seen.add(cursor)
            cursor = managers.get(cursor)
    return findings


def check_natural_keys(dataset: Dataset) -> list[Finding]:
    """Reject domain duplicates that synthetic surrogate IDs would otherwise hide."""
    findings: list[Finding] = []
    seen_routes: set[tuple[str, str, str]] = set()
    for row in dataset.get("travel_routes", []):
        key = (
            str(row.get("origin_location_id")),
            str(row.get("destination_location_id")),
            str(row.get("mode")),
        )
        if key in seen_routes:
            findings.append(Finding("travel_routes", "natural-key-duplicate", repr(key)))
        seen_routes.add(key)
    return findings


def check_scope_coverage(dataset: Dataset) -> list[Finding]:
    """Every scoped table must have rows in every region.

    Without this a scope-isolation test could pass simply because one region is
    empty, which proves nothing about the filter.
    """
    findings: list[Finding] = []
    regions = {"AMER", "EMEA", "APAC"}
    for table in SCHEMA:
        if not table.scope_column:
            continue
        present = {str(row.get(table.scope_column)) for row in dataset.get(table.name, [])}
        missing = regions - present
        if missing and table.name != "hr_policies":
            findings.append(Finding(table.name, "scope-coverage",
                                    f"no rows for {', '.join(sorted(missing))}"))
    return findings


def check_all(dataset: Dataset) -> list[Finding]:
    findings: list[Finding] = []
    for check in (
        check_primary_keys,
        check_foreign_keys,
        check_not_null,
        check_identifier_shapes,
        check_dates,
        check_rollups,
        check_hierarchy,
        check_natural_keys,
        check_scope_coverage,
    ):
        findings.extend(check(dataset))
    return findings


__all__ = ["Finding", "ID_PATTERNS", "check_all"]
