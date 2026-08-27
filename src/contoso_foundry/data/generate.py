"""Deterministic generator for the Contoso spine.

Three properties matter more than realism:

1. **Reproducible.** The only inputs are ``config/data-spine.yaml``, the seed
   tables under ``data/seed/`` and this module. No clock, no network, no
   environment. Running it twice on different machines produces identical rows.
2. **Referentially sound.** Every foreign key is resolved from a parent that has
   already been generated, so the dataset cannot be internally inconsistent even
   before the integrity checks run.
3. **Provably synthetic.** Names come from closed vocabularies, addresses from a
   reserved documentation domain and telephone numbers from the fiction block.
   The privacy gate re-derives all three rather than trusting this file.

Randomness is drawn from a single seeded generator consumed in a fixed order.
That makes the output stable but also brittle in a useful way: reordering the
code changes the dataset, which shows up immediately as a lock-file mismatch
rather than as a mystery three months later.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .model import TRAVEL_EXCEPTION_DEPARTMENTS

REGIONS = ("AMER", "EMEA", "APAC")

#: Sites that hold stock. An office does not have a stockroom.
STOCKING_KINDS = frozenset({"headquarters", "distribution", "service_depot"})

#: Departments whose members are plausible order-takers.
ORDER_TAKING_DEPARTMENTS = ("Retail Operations", "Supply Chain", "Customer Support")

ORDER_STATUSES = ("placed", "picked", "shipped", "delivered", "cancelled")
CASE_SEVERITIES = ("sev1", "sev2", "sev3", "sev4")
CASE_STATUSES = ("new", "in_progress", "waiting_customer", "resolved", "closed")
CASE_CHANNELS = ("phone", "email", "portal", "field")
WORK_ORDER_STATUSES = ("raised", "scheduled", "in_progress", "completed", "cancelled")
WORK_ORDER_PRIORITIES = ("routine", "elevated", "priority")
EMPLOYMENT_TYPES = ("permanent", "permanent", "permanent", "fixed_term", "contractor")
UNITS_OF_MEASURE = ("each", "box", "metre", "kit")

#: Rough list price band per category, in USD. Deliberately coarse; the point is
#: that a networking switch costs more than a warning sign, not that either
#: number is right.
CATEGORY_PRICE_BANDS = {
    "CAT-01": (180.0, 4200.0),
    "CAT-02": (900.0, 12500.0),
    "CAT-03": (25.0, 780.0),
    "CAT-04": (8.0, 1900.0),
    "CAT-05": (6.0, 140.0),
    "CAT-06": (3.0, 90.0),
    "CAT-07": (1.0, 60.0),
    "CAT-08": (120.0, 1600.0),
}

Row = dict[str, Any]
Dataset = dict[str, list[Row]]


class GenerationError(RuntimeError):
    """Raised when the seed inputs cannot produce a coherent dataset."""


@dataclass(frozen=True)
class SeedInputs:
    config: dict[str, Any]
    reference: dict[str, Any]
    identities: dict[str, Any]

    @property
    def as_of(self) -> dt.date:
        return dt.date.fromisoformat(str(self.config["as_of"]))

    @property
    def currency(self) -> str:
        return str(self.config["currency"])

    @property
    def formats(self) -> dict[str, str]:
        return dict(self.config["identifiers"])


def load_seed_inputs(config_path: Path, seed_dir: Path) -> SeedInputs:
    def read(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise GenerationError(f"missing seed input: {path}")
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    return SeedInputs(
        config=read(config_path),
        reference=read(seed_dir / "reference.yaml"),
        identities=read(seed_dir / "identities.yaml"),
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _iso(value: dt.date) -> str:
    return value.isoformat()


def _money(value: float) -> float:
    # Two decimal places, and never a negative zero, so the CSV round-trips
    # identically on every platform.
    return round(value + 0.0, 2)


def _phone(rng: random.Random, area_codes: list[int]) -> str:
    """A number in the 555-0100 to 555-0199 block reserved for fiction.

    Rendered in North American format regardless of the country the row belongs
    to. That is deliberate: a single provably fictional format is easier to
    verify than eleven plausible national ones, and nothing in this dataset
    should ever be dialled.
    """
    return f"+1 {rng.choice(area_codes)}-555-{100 + rng.randrange(100):04d}"


def _slug(value: str) -> str:
    keep = [c.lower() if c.isalnum() else "." for c in value]
    collapsed = "".join(keep)
    while ".." in collapsed:
        collapsed = collapsed.replace("..", ".")
    return collapsed.strip(".")


# --------------------------------------------------------------------------- #
# Layer A — the relational spine
# --------------------------------------------------------------------------- #


def _locations(seed: SeedInputs) -> list[Row]:
    return [dict(row) for row in seed.reference["locations"]]


def _suppliers(seed: SeedInputs, rng: random.Random) -> list[Row]:
    domain = seed.config["privacy"]["email_domain"]
    area_codes = list(seed.config["privacy"]["phone_area_codes"])
    rows: list[Row] = []
    for entry in seed.reference["suppliers"]:
        row = dict(entry)
        # The mailbox is Contoso's own supplier-management alias, not the
        # supplier's real address: this dataset must not imply a contactable
        # third party even a fictitious one.
        row["contact_email"] = f"supplier.{_slug(row['supplier_id'])}@{domain}"
        row["contact_phone"] = _phone(rng, area_codes)
        rows.append(row)
    return rows


def _product_categories(seed: SeedInputs) -> list[Row]:
    return [dict(row) for row in seed.reference["product_categories"]]


def _products(seed: SeedInputs, rng: random.Random, categories: list[Row], suppliers: list[Row]) -> list[Row]:
    fmt = seed.formats["product"]
    words = seed.reference["product_words"]
    lines = seed.reference["product_lines"]
    rows: list[Row] = []

    for index in range(int(seed.config["volumes"]["products"])):
        category = categories[index % len(categories)]
        category_id = category["category_id"]
        supplier = suppliers[rng.randrange(len(suppliers))]
        word = rng.choice(words[category_id])
        line = rng.choice(lines)
        model = f"{chr(65 + rng.randrange(26))}{rng.randrange(100, 999)}"
        low, high = CATEGORY_PRICE_BANDS[category_id]

        rows.append(
            {
                "product_id": fmt.format(index + 1),
                "name": f"{line} {word} {model}",
                "category_id": category_id,
                "supplier_id": supplier["supplier_id"],
                "unit_price": _money(rng.uniform(low, high)),
                "currency": seed.currency,
                "unit_of_measure": rng.choice(UNITS_OF_MEASURE),
                # A tenth of the catalogue is withdrawn, so "is this still
                # available" is a question with two possible answers.
                "is_discontinued": 1 if rng.random() < 0.10 else 0,
            }
        )
    return rows


def _stock_levels(rng: random.Random, products: list[Row], locations: list[Row]) -> list[Row]:
    stocking = [loc for loc in locations if loc["kind"] in STOCKING_KINDS]
    if not stocking:
        raise GenerationError("no stocking locations in the seed reference data")

    rows: list[Row] = []
    for product in products:
        held_at = rng.sample(stocking, k=rng.randrange(2, min(4, len(stocking)) + 1))
        for location in sorted(held_at, key=lambda loc: loc["location_id"]):
            reorder = rng.randrange(5, 40)
            rows.append(
                {
                    "product_id": product["product_id"],
                    "location_id": location["location_id"],
                    # A quarter of holdings sit at or below the reorder level, so
                    # a replenishment answer is sometimes "no, and here is why".
                    "quantity_on_hand": rng.randrange(0, reorder) if rng.random() < 0.25
                    else rng.randrange(reorder, reorder * 12),
                    "reorder_level": reorder,
                }
            )
    return rows


def _customers(seed: SeedInputs, rng: random.Random, locations: list[Row]) -> list[Row]:
    fmt = seed.formats["customer"]
    domain = seed.config["privacy"]["email_domain"]
    area_codes = list(seed.config["privacy"]["phone_area_codes"])
    prefixes = seed.reference["customer_prefixes"]
    suffixes = seed.reference["customer_suffixes"]
    segments = seed.reference["customer_segments"]
    as_of = seed.as_of

    rows: list[Row] = []
    used_names: set[str] = set()

    for index in range(int(seed.config["volumes"]["customers"])):
        # Walk the cross product deterministically rather than sampling, so the
        # names stay unique without a retry loop that would consume a variable
        # number of random draws and break reproducibility.
        prefix = prefixes[index % len(prefixes)]
        suffix = suffixes[(index // len(prefixes)) % len(suffixes)]
        name = f"{prefix} {suffix}"
        if name in used_names:
            name = f"{name} {index // (len(prefixes) * len(suffixes)) + 2}"
        used_names.add(name)

        location = locations[rng.randrange(len(locations))]
        customer_id = fmt.format(index + 1)
        rows.append(
            {
                "customer_id": customer_id,
                "name": name,
                "segment": rng.choice(segments),
                "region": location["region"],
                "primary_location_id": location["location_id"],
                # Contoso's own account-management alias for the customer. Same
                # reasoning as suppliers: no contactable address, even fictional.
                "contact_email": f"account.{_slug(customer_id)}@{domain}",
                "contact_phone": _phone(rng, area_codes),
                "credit_limit": _money(rng.choice([5_000, 10_000, 25_000, 50_000, 100_000, 250_000])),
                "opened_on": _iso(as_of - dt.timedelta(days=rng.randrange(60, 3600))),
            }
        )
    return rows


def _employees(seed: SeedInputs, rng: random.Random, locations: list[Row]) -> list[Row]:
    """The roster, as a four-level tree.

    One global lead, a director per region, a lead per department per region,
    then individual contributors. The shape matters because the HR hierarchy
    tool has to walk it under a scope filter, and a flat list would let a broken
    filter look correct.
    """
    fmt = seed.formats["employee"]
    domain = seed.config["privacy"]["email_domain"]
    area_codes = list(seed.config["privacy"]["phone_area_codes"])
    given = seed.reference["given_names"]
    family = seed.reference["family_names"]
    departments = list(seed.reference["departments"])
    titles = list(seed.reference["job_titles"])
    leadership_dept = seed.reference["leadership_department"]
    leadership_titles = seed.reference["leadership_titles"]
    as_of = seed.as_of

    total = int(seed.config["volumes"]["employees"])
    if total < len(REGIONS) * len(departments) + len(REGIONS) + 1:
        raise GenerationError(
            "employees volume is too small to give every region a lead in every department"
        )

    by_region: dict[str, list[Row]] = {region: [] for region in REGIONS}
    for location in locations:
        by_region[location["region"]].append(location)

    rows: list[Row] = []

    def make(index: int, region: str, department: str, title: str, manager_id: str | None) -> Row:
        capacity = len(given) * len(family)
        slot = index % capacity
        base = f"{given[slot % len(given)]} {family[slot // len(given)]}"
        cycle = index // capacity
        candidate = base if cycle == 0 else f"{base} {cycle + 1}"

        sites = by_region[region]
        location = sites[index % len(sites)]
        employee_id = fmt.format(index + 1)
        return {
            "employee_id": employee_id,
            "full_name": candidate,
            "work_email": f"{_slug(candidate)}@{domain}",
            "work_phone": _phone(rng, area_codes),
            "job_title": title,
            "department": department,
            "region": region,
            "location_id": location["location_id"],
            "manager_id": manager_id,
            "hired_on": _iso(as_of - dt.timedelta(days=rng.randrange(90, 4000))),
            "employment_type": rng.choice(EMPLOYMENT_TYPES),
            "cost_centre": f"CC-{region}-{department[:3].upper()}",
        }

    index = 0
    global_lead = make(index, "AMER", leadership_dept, leadership_titles["global"], None)
    rows.append(global_lead)
    index += 1

    regional_leads: dict[str, str] = {}
    for region in REGIONS:
        row = make(index, region, leadership_dept, leadership_titles["regional"], global_lead["employee_id"])
        rows.append(row)
        regional_leads[region] = row["employee_id"]
        index += 1

    department_leads: dict[tuple[str, str], str] = {}
    for department in departments:
        for region in REGIONS:
            row = make(index, region, department, leadership_titles["department"], regional_leads[region])
            rows.append(row)
            department_leads[(region, department)] = row["employee_id"]
            index += 1

    while index < total:
        offset = index - (1 + len(REGIONS) + len(REGIONS) * len(departments))
        region = REGIONS[offset % len(REGIONS)]
        department = departments[(offset // len(REGIONS)) % len(departments)]
        rows.append(
            make(index, region, department, rng.choice(titles), department_leads[(region, department)])
        )
        index += 1

    return rows


def _index_employees(employees: list[Row]) -> dict[tuple[str, str], list[Row]]:
    index: dict[tuple[str, str], list[Row]] = {}
    for row in employees:
        index.setdefault((row["region"], row["department"]), []).append(row)
    for bucket in index.values():
        bucket.sort(key=lambda r: r["employee_id"])
    return index


def _pick_employee(
    rng: random.Random,
    index: dict[tuple[str, str], list[Row]],
    region: str,
    departments: tuple[str, ...],
    fallback: list[Row],
) -> Row:
    candidates = [row for department in departments for row in index.get((region, department), [])]
    pool = candidates or fallback
    return pool[rng.randrange(len(pool))]


def _orders_and_invoices(
    seed: SeedInputs,
    rng: random.Random,
    customers: list[Row],
    products: list[Row],
    employees: list[Row],
) -> tuple[list[Row], list[Row], list[Row]]:
    order_fmt = seed.formats["order"]
    line_fmt = seed.formats["order_line"]
    invoice_fmt = seed.formats["invoice"]
    as_of = seed.as_of
    by_department = _index_employees(employees)
    sellable = [p for p in products if not p["is_discontinued"]] or products

    orders: list[Row] = []
    lines: list[Row] = []
    invoices: list[Row] = []
    invoice_index = 0

    for index in range(int(seed.config["volumes"]["orders"])):
        customer = customers[rng.randrange(len(customers))]
        clerk = _pick_employee(rng, by_department, customer["region"], ORDER_TAKING_DEPARTMENTS, employees)
        ordered_on = as_of - dt.timedelta(days=rng.randrange(1, 540))
        status = rng.choice(ORDER_STATUSES)
        order_id = order_fmt.format(index + 1)

        chosen = rng.sample(sellable, k=rng.randrange(1, 5))
        total = 0.0
        for line_number, product in enumerate(chosen, start=1):
            quantity = rng.randrange(1, 25)
            unit_price = _money(product["unit_price"] * rng.uniform(0.9, 1.0))
            line_total = _money(unit_price * quantity)
            total += line_total
            lines.append(
                {
                    "order_line_id": line_fmt.format(index + 1, line_number),
                    "order_id": order_id,
                    "product_id": product["product_id"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "line_total": line_total,
                }
            )

        orders.append(
            {
                "order_id": order_id,
                "customer_id": customer["customer_id"],
                "location_id": customer["primary_location_id"],
                "placed_by_employee_id": clerk["employee_id"],
                "ordered_on": _iso(ordered_on),
                "status": status,
                "currency": seed.currency,
                "order_total": _money(total),
            }
        )

        # A cancelled order is never invoiced. That asymmetry is the point: it
        # gives the finance tools a case where the obvious join returns nothing
        # and the honest answer is "there is no invoice".
        if status == "cancelled":
            continue

        invoice_index += 1
        issued_on = ordered_on + dt.timedelta(days=rng.randrange(1, 6))
        due_on = issued_on + dt.timedelta(days=30)
        amount_due = _money(total)

        if status == "delivered":
            invoice_status, amount_paid = "paid", amount_due
        elif status == "shipped":
            invoice_status, amount_paid = "part_paid", _money(amount_due * rng.uniform(0.2, 0.8))
        elif due_on < as_of:
            invoice_status, amount_paid = "overdue", 0.0
        else:
            invoice_status, amount_paid = "issued", 0.0

        invoices.append(
            {
                "invoice_id": invoice_fmt.format(invoice_index),
                "order_id": order_id,
                "customer_id": customer["customer_id"],
                "issued_on": _iso(issued_on),
                "due_on": _iso(due_on),
                "amount_due": amount_due,
                "amount_paid": amount_paid,
                "status": invoice_status,
                "currency": seed.currency,
            }
        )

    return orders, lines, invoices


# --------------------------------------------------------------------------- #
# Layer B — domain data
# --------------------------------------------------------------------------- #


def _support(
    seed: SeedInputs,
    rng: random.Random,
    customers: list[Row],
    products: list[Row],
    employees: list[Row],
) -> tuple[list[Row], list[Row]]:
    case_fmt = seed.formats["support_case"]
    note_fmt = seed.formats["support_note"]
    subjects = seed.reference["support_subjects"]
    bodies = seed.reference["support_note_bodies"]
    by_department = _index_employees(employees)
    as_of = seed.as_of

    cases: list[Row] = []
    notes: list[Row] = []
    note_index = 0

    for index in range(int(seed.config["volumes"]["support_cases"])):
        customer = customers[rng.randrange(len(customers))]
        owner = _pick_employee(rng, by_department, customer["region"], ("Customer Support",), employees)
        opened_on = as_of - dt.timedelta(days=rng.randrange(1, 400))
        status = rng.choice(CASE_STATUSES)
        closed_on = (
            _iso(opened_on + dt.timedelta(days=rng.randrange(1, 30)))
            if status in {"resolved", "closed"}
            else None
        )
        case_id = case_fmt.format(index + 1)

        cases.append(
            {
                "case_id": case_id,
                "customer_id": customer["customer_id"],
                "product_id": products[rng.randrange(len(products))]["product_id"] if rng.random() < 0.8 else None,
                "assigned_employee_id": owner["employee_id"],
                "opened_on": _iso(opened_on),
                "closed_on": closed_on,
                "severity": rng.choice(CASE_SEVERITIES),
                "status": status,
                "channel": rng.choice(CASE_CHANNELS),
                "subject": rng.choice(subjects),
            }
        )

        for offset in range(rng.randrange(1, 4)):
            note_index += 1
            visibility = "internal" if rng.random() < 0.4 else "customer"
            notes.append(
                {
                    "note_id": note_fmt.format(note_index),
                    "case_id": case_id,
                    "author_employee_id": owner["employee_id"],
                    "noted_on": _iso(opened_on + dt.timedelta(days=offset)),
                    "visibility": visibility,
                    "body": rng.choice(bodies[visibility]),
                }
            )

    return cases, notes


def _travel_network(seed: SeedInputs, rng: random.Random, locations: list[Row]) -> tuple[list[Row], list[Row]]:
    """Hub-and-spoke routes: everything inside a region, plus hub to hub's world.

    A full mesh of twelve sites would be 132 routes of no additional value. The
    hub model is both smaller and closer to how a corporate travel desk actually
    thinks about a network.
    """
    route_fmt = seed.formats["travel_route"]
    fare_fmt = seed.formats["travel_fare"]

    by_region: dict[str, list[Row]] = {region: [] for region in REGIONS}
    for location in locations:
        by_region[location["region"]].append(location)
    hubs = {region: sorted(sites, key=lambda s: s["location_id"])[0] for region, sites in by_region.items()}

    pairs: list[tuple[Row, Row]] = []
    seen_pairs: set[tuple[str, str]] = set()

    def add_pair(origin: Row, destination: Row) -> None:
        key = (str(origin["location_id"]), str(destination["location_id"]))
        if key not in seen_pairs:
            seen_pairs.add(key)
            pairs.append((origin, destination))

    for region in REGIONS:
        sites = sorted(by_region[region], key=lambda s: s["location_id"])
        for origin in sites:
            for destination in sites:
                if origin["location_id"] != destination["location_id"]:
                    add_pair(origin, destination)

    for region in REGIONS:
        hub = hubs[region]
        for other in REGIONS:
            if other == region:
                continue
            for destination in sorted(by_region[other], key=lambda s: s["location_id"]):
                add_pair(hub, destination)
                add_pair(destination, hub)

    routes: list[Row] = []
    fares: list[Row] = []
    fare_index = 0

    for index, (origin, destination) in enumerate(pairs, start=1):
        same_region = origin["region"] == destination["region"]
        duration = rng.randrange(55, 260) if same_region else rng.randrange(420, 960)
        # Rail only where Contoso's European sites are genuinely connected by it.
        mode = "rail" if same_region and origin["region"] == "EMEA" and duration < 300 else "air"
        route_id = route_fmt.format(index)
        long_haul = 1 if duration > 480 else 0

        routes.append(
            {
                "route_id": route_id,
                "origin_location_id": origin["location_id"],
                "destination_location_id": destination["location_id"],
                "mode": mode,
                "duration_minutes": duration,
                "is_long_haul": long_haul,
            }
        )

        classes = ["economy", "economy_plus"] + (["business"] if long_haul else [])
        for multiplier, fare_class in zip((1.0, 1.6, 3.4), classes, strict=False):
            fare_index += 1
            fares.append(
                {
                    "fare_id": fare_fmt.format(fare_index),
                    "route_id": route_id,
                    "fare_class": fare_class,
                    "base_price": _money(duration * rng.uniform(0.9, 1.6) * multiplier),
                    "currency": seed.currency,
                    "refundable": 1 if fare_class != "economy" else int(rng.random() < 0.25),
                    "seats_available": rng.randrange(0, 40),
                }
            )

    return routes, fares


def _travel_policies(seed: SeedInputs) -> list[Row]:
    rows: list[Row] = []
    for entry in seed.reference["travel_policies"]:
        row = dict(entry)
        row["requires_approval_over"] = _money(float(row["requires_approval_over"]))
        row["currency"] = seed.currency
        row["summary"] = " ".join(str(row["summary"]).split())
        rows.append(row)
    return rows


def _travel_bookings(
    seed: SeedInputs,
    rng: random.Random,
    employees: list[Row],
    routes: list[Row],
    fares: list[Row],
    policies: list[Row],
) -> list[Row]:
    booking_fmt = seed.formats["travel_booking"]
    as_of = seed.as_of

    fares_by_route: dict[str, list[Row]] = {}
    for fare in fares:
        fares_by_route.setdefault(fare["route_id"], []).append(fare)

    routes_from: dict[str, list[Row]] = {}
    for route in routes:
        routes_from.setdefault(route["origin_location_id"], []).append(route)

    policies_by_region: dict[str, list[Row]] = {}
    for policy in policies:
        policies_by_region.setdefault(policy["region"], []).append(policy)

    rows: list[Row] = []
    for index in range(int(seed.config["volumes"]["travel_bookings"])):
        traveller = employees[rng.randrange(len(employees))]
        available = routes_from.get(traveller["location_id"])
        if not available:
            # A traveller based at a site with no departing route books from the
            # region hub instead. Better than skipping: the count stays exact.
            available = [r for r in routes if r["origin_location_id"].startswith("LOC-")]
        route = available[rng.randrange(len(available))]
        fare = fares_by_route[route["route_id"]][rng.randrange(len(fares_by_route[route["route_id"]]))]

        candidates = policies_by_region[traveller["region"]]
        # Field and depot staff travel under the exception policy; everyone else
        # under the regional standard.
        wants_exception = traveller["department"] in TRAVEL_EXCEPTION_DEPARTMENTS
        policy = candidates[-1] if wants_exception and len(candidates) > 1 else candidates[0]

        booked_on = as_of - dt.timedelta(days=rng.randrange(5, 220))
        lead_days = rng.randrange(1, 45)
        depart_on = booked_on + dt.timedelta(days=lead_days)
        return_on = (
            _iso(depart_on + dt.timedelta(days=rng.randrange(1, 9))) if rng.random() < 0.75 else None
        )
        total = _money(float(fare["base_price"]) * (2 if return_on else 1) * rng.uniform(1.05, 1.35))

        breached_value = total > float(policy["requires_approval_over"])
        breached_notice = lead_days < int(policy["advance_purchase_days"])

        rows.append(
            {
                "booking_id": booking_fmt.format(index + 1),
                "traveller_employee_id": traveller["employee_id"],
                "route_id": route["route_id"],
                "fare_id": fare["fare_id"],
                "policy_id": policy["policy_id"],
                "depart_on": _iso(depart_on),
                "return_on": return_on,
                "booked_on": _iso(booked_on),
                "status": rng.choice(("held", "ticketed", "ticketed", "flown", "cancelled")),
                "total_price": total,
                "currency": seed.currency,
                "requires_approval": int(breached_value or breached_notice),
                "cost_centre": traveller["cost_centre"],
            }
        )
    return rows


def _hr_policies(seed: SeedInputs) -> list[Row]:
    rows: list[Row] = []
    for entry in seed.reference["hr_policies"]:
        row = dict(entry)
        row["summary"] = " ".join(str(row["summary"]).split())
        rows.append(row)
    return rows


def _work_orders(
    seed: SeedInputs,
    rng: random.Random,
    locations: list[Row],
    customers: list[Row],
    products: list[Row],
    employees: list[Row],
    cases: list[Row],
) -> list[Row]:
    fmt = seed.formats["work_order"]
    tasks = seed.reference["work_order_tasks"]
    by_department = _index_employees(employees)
    customers_by_id = {row["customer_id"]: row for row in customers}
    as_of = seed.as_of

    rows: list[Row] = []
    for index in range(int(seed.config["volumes"]["work_orders"])):
        # Roughly a third of field work originates from a support case. Where it
        # does, the customer and product are inherited rather than re-rolled, so
        # the two records agree about what broke.
        source_case = cases[rng.randrange(len(cases))] if rng.random() < 0.35 else None
        if source_case is not None:
            customer = customers_by_id[source_case["customer_id"]]
            location_id = customer["primary_location_id"]
            region = customer["region"]
            product_id = source_case["product_id"]
        else:
            location = locations[rng.randrange(len(locations))]
            location_id = location["location_id"]
            region = location["region"]
            customer = customers[rng.randrange(len(customers))] if rng.random() < 0.6 else None
            product_id = products[rng.randrange(len(products))]["product_id"] if rng.random() < 0.7 else None

        engineer = _pick_employee(rng, by_department, region, ("Field Operations",), employees)
        opened_on = as_of - dt.timedelta(days=rng.randrange(1, 300))
        scheduled_for = opened_on + dt.timedelta(days=rng.randrange(1, 21))
        status = rng.choice(WORK_ORDER_STATUSES)

        rows.append(
            {
                "work_order_id": fmt.format(index + 1),
                "location_id": location_id,
                "customer_id": customer["customer_id"] if customer else None,
                "product_id": product_id,
                "assigned_employee_id": engineer["employee_id"],
                "case_id": source_case["case_id"] if source_case else None,
                "opened_on": _iso(opened_on),
                "scheduled_for": _iso(scheduled_for),
                "completed_on": _iso(scheduled_for + dt.timedelta(days=rng.randrange(0, 4)))
                if status == "completed"
                else None,
                "status": status,
                "priority": rng.choice(WORK_ORDER_PRIORITIES),
                "task": rng.choice(tasks),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Control plane
# --------------------------------------------------------------------------- #


def _identities(seed: SeedInputs, employees: list[Row]) -> list[Row]:
    """Resolve each persona to a real employee.

    The selector is ``(region, department, rank)`` rather than a pinned employee
    ID, because a pinned ID silently points at somebody else the first time a
    volume changes, and a silently wrong scope fixture is worse than no fixture.
    """
    tenant_key = seed.identities["tenant_key"]
    by_department = _index_employees(employees)
    rows: list[Row] = []

    for entry in seed.identities["principals"]:
        selector = entry["select"]
        key = (selector["region"], selector["department"])
        bucket = by_department.get(key, [])
        rank = int(selector["rank"])
        if len(bucket) <= rank:
            raise GenerationError(
                f"persona {entry['persona']!r} wants rank {rank} of {key}, "
                f"but only {len(bucket)} employee(s) exist there"
            )
        employee = bucket[rank]

        if employee["region"] not in entry["scope_regions"]:
            raise GenerationError(
                f"persona {entry['persona']!r} resolves to an employee in "
                f"{employee['region']} but is scoped to {entry['scope_regions']}"
            )

        rows.append(
            {
                "principal_oid": entry["principal_oid"],
                "tenant_key": tenant_key,
                "employee_id": employee["employee_id"],
                "roles": "|".join(entry["roles"]),
                "scope_regions": "|".join(entry["scope_regions"]),
                "persona": entry["persona"],
            }
        )
    return rows


# --------------------------------------------------------------------------- #


def generate(seed: SeedInputs) -> Dataset:
    """Build the whole dataset. Pure function of ``seed``."""
    rng = random.Random(int(seed.config["seed"]))

    locations = _locations(seed)
    suppliers = _suppliers(seed, rng)
    categories = _product_categories(seed)
    products = _products(seed, rng, categories, suppliers)
    stock = _stock_levels(rng, products, locations)
    customers = _customers(seed, rng, locations)
    employees = _employees(seed, rng, locations)
    orders, order_lines, invoices = _orders_and_invoices(seed, rng, customers, products, employees)
    cases, notes = _support(seed, rng, customers, products, employees)
    routes, fares = _travel_network(seed, rng, locations)
    travel_policies = _travel_policies(seed)
    bookings = _travel_bookings(seed, rng, employees, routes, fares, travel_policies)
    hr_policies = _hr_policies(seed)
    work_orders = _work_orders(seed, rng, locations, customers, products, employees, cases)
    identities = _identities(seed, employees)

    return {
        "locations": locations,
        "suppliers": suppliers,
        "product_categories": categories,
        "products": products,
        "stock_levels": stock,
        "customers": customers,
        "employees": employees,
        "orders": orders,
        "order_lines": order_lines,
        "invoices": invoices,
        "support_cases": cases,
        "support_case_notes": notes,
        "travel_routes": routes,
        "travel_fares": fares,
        "travel_policies": travel_policies,
        "travel_bookings": bookings,
        "hr_policies": hr_policies,
        "work_orders": work_orders,
        "identities": identities,
    }
