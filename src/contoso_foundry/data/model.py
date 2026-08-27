"""Schema for the Contoso spine.

The schema is data, not DDL text. Every consumer — the SQLite loader, the CSV
writer, the referential-integrity checker, the PII gate and the docs page —
reads *this* structure, so a column cannot be added in one place and forgotten
in another.

Table shapes follow the concepts in Microsoft's MIT-licensed
`WideWorldImporters <https://github.com/microsoft/sql-server-samples>`_ sample:
a customer/supplier/stock-item core with orders and invoices hanging off it. The
concepts are borrowed; none of the data is. Contoso's version is deliberately
small enough to hold in your head and runs on SQLite, so nobody needs SQL Server
to work on an agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Column classifications.
#
# These drive the privacy gate. A column declared PERSON_NAME must contain a
# value from the checked-in name vocabulary; a column declared EMAIL must be on
# a provably synthetic domain. Anything not declared is treated as free text and
# is passed through the same scanner that guards the public site.
# --------------------------------------------------------------------------- #

IDENTIFIER = "identifier"
PERSON_NAME = "person_name"
ORG_NAME = "org_name"
EMAIL = "email"
PHONE = "phone"
FREE_TEXT = "free_text"
MONEY = "money"
DATE = "date"
ENUM = "enum"
NUMBER = "number"

#: Classifications whose values must survive the person-shaped-data checks in
#: :mod:`contoso_foundry.data.pii`.
PERSONAL_CLASSIFICATIONS = frozenset({PERSON_NAME, EMAIL, PHONE})

# A caller's exception policy is derived from the server-resolved employee
# record, matching the same rule used to generate historical bookings.
TRAVEL_EXCEPTION_DEPARTMENTS = frozenset({"Field Operations", "Customer Support"})


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    description: str
    nullable: bool = False
    primary_key: bool = False
    #: ``"table.column"`` of the referenced key, or ``None``.
    references: str | None = None
    classification: str = IDENTIFIER

    @property
    def referenced_table(self) -> str | None:
        return self.references.split(".", 1)[0] if self.references else None

    @property
    def referenced_column(self) -> str | None:
        return self.references.split(".", 1)[1] if self.references else None


@dataclass(frozen=True)
class Table:
    name: str
    layer: str
    grain: str
    description: str
    columns: tuple[Column, ...]
    #: Column carrying the region a row belongs to, where one exists. The scoped
    #: repository refuses to serve a table that claims to be scope-bearing but
    #: cannot say which column decides it.
    scope_column: str | None = None
    #: Columns a caller may filter on through a tool contract. Anything outside
    #: this list is not addressable from a prompt, which keeps the attack surface
    #: of "let the model write a filter" bounded.
    filterable: tuple[str, ...] = field(default_factory=tuple)

    @property
    def primary_key(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.primary_key)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> Column:
        for candidate in self.columns:
            if candidate.name == name:
                return candidate
        raise KeyError(f"{self.name} has no column {name!r}")

    @property
    def foreign_keys(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.references)


# --------------------------------------------------------------------------- #
# Layer A — the relational spine
# --------------------------------------------------------------------------- #

LOCATIONS = Table(
    name="locations",
    layer="spine",
    grain="One row per Contoso site.",
    description="Every place Contoso operates. Carries the region every other scope decision hangs off.",
    scope_column="region",
    filterable=("region", "country", "kind"),
    columns=(
        Column("location_id", "TEXT", "Canonical location key, LOC-000.", primary_key=True),
        Column("name", "TEXT", "Site name.", classification=ORG_NAME),
        Column("city", "TEXT", "City.", classification=FREE_TEXT),
        Column("country", "TEXT", "Country.", classification=FREE_TEXT),
        Column("region", "TEXT", "AMER, EMEA or APAC.", classification=ENUM),
        Column("timezone", "TEXT", "IANA time zone.", classification=FREE_TEXT),
        Column("kind", "TEXT", "headquarters, office, distribution, service_depot or support_centre.",
               classification=ENUM),
        Column("opened_on", "TEXT", "ISO date the site opened.", classification=DATE),
    ),
)

SUPPLIERS = Table(
    name="suppliers",
    layer="spine",
    grain="One row per supplier.",
    description="Fictitious companies Contoso buys from. Names are Microsoft's reserved sample companies.",
    scope_column="region",
    filterable=("region", "country"),
    columns=(
        Column("supplier_id", "TEXT", "Canonical supplier key, SUP-000.", primary_key=True),
        Column("name", "TEXT", "Trading name.", classification=ORG_NAME),
        Column("country", "TEXT", "Country of the supplying entity.", classification=FREE_TEXT),
        Column("region", "TEXT", "AMER, EMEA or APAC.", classification=ENUM),
        Column("contact_email", "TEXT", "Synthetic mailbox on contoso.com.", classification=EMAIL),
        Column("contact_phone", "TEXT", "Fictional 555-01xx number.", classification=PHONE),
        Column("agreement_ref", "TEXT", "Commercial agreement reference."),
    ),
)

PRODUCT_CATEGORIES = Table(
    name="product_categories",
    layer="spine",
    grain="One row per catalogue category.",
    description="The eight buckets the catalogue is organised into.",
    filterable=("category_id",),
    columns=(
        Column("category_id", "TEXT", "Canonical category key, CAT-00.", primary_key=True),
        Column("name", "TEXT", "Category name.", classification=FREE_TEXT),
        Column("description", "TEXT", "What belongs in it.", classification=FREE_TEXT),
    ),
)

PRODUCTS = Table(
    name="products",
    layer="spine",
    grain="One row per sellable product.",
    description="The product catalogue. Referenced by orders, support cases and work orders.",
    filterable=("category_id", "supplier_id", "is_discontinued"),
    columns=(
        Column("product_id", "TEXT", "Canonical product key, PROD-0000.", primary_key=True),
        Column("name", "TEXT", "Product name.", classification=FREE_TEXT),
        Column("category_id", "TEXT", "Owning category.", references="product_categories.category_id"),
        Column("supplier_id", "TEXT", "Supplying company.", references="suppliers.supplier_id"),
        Column("unit_price", "REAL", "List price per unit.", classification=MONEY),
        Column("currency", "TEXT", "ISO 4217 code.", classification=ENUM),
        Column("unit_of_measure", "TEXT", "each, box, metre or kit.", classification=ENUM),
        Column("is_discontinued", "INTEGER", "1 when withdrawn from sale.", classification=NUMBER),
    ),
)

STOCK_LEVELS = Table(
    name="stock_levels",
    layer="domain",
    grain="One row per product per stocking location.",
    description="Retail and depot inventory. The join that lets an agent answer 'can I get one today'.",
    filterable=("product_id", "location_id"),
    columns=(
        Column("product_id", "TEXT", "Product held.", primary_key=True, references="products.product_id"),
        Column("location_id", "TEXT", "Where it is held.", primary_key=True, references="locations.location_id"),
        Column("quantity_on_hand", "INTEGER", "Units physically present.", classification=NUMBER),
        Column("reorder_level", "INTEGER", "Units at which replenishment triggers.", classification=NUMBER),
    ),
)

CUSTOMERS = Table(
    name="customers",
    layer="spine",
    grain="One row per customer organisation.",
    description="Buying organisations. Invented trading names; no real company appears here.",
    scope_column="region",
    filterable=("region", "segment", "primary_location_id"),
    columns=(
        Column("customer_id", "TEXT", "Canonical customer key, CUST-00000.", primary_key=True),
        Column("name", "TEXT", "Trading name.", classification=ORG_NAME),
        Column("segment", "TEXT", "enterprise, midmarket, public_sector or small_business.", classification=ENUM),
        Column("region", "TEXT", "AMER, EMEA or APAC.", classification=ENUM),
        Column("primary_location_id", "TEXT", "Contoso site that serves them.", references="locations.location_id"),
        Column("contact_email", "TEXT", "Synthetic mailbox on contoso.com.", classification=EMAIL),
        Column("contact_phone", "TEXT", "Fictional 555-01xx number.", classification=PHONE),
        Column("credit_limit", "REAL", "Agreed credit ceiling.", classification=MONEY),
        Column("opened_on", "TEXT", "ISO date the account opened.", classification=DATE),
    ),
)

EMPLOYEES = Table(
    name="employees",
    layer="spine",
    grain="One row per employee.",
    description=(
        "The roster. This is the scope-bearing table the row-level security work is built around: "
        "every HR read is filtered by the caller's server-resolved region before a row is considered."
    ),
    scope_column="region",
    filterable=("region", "department", "job_title", "employment_type", "location_id", "manager_id"),
    columns=(
        Column("employee_id", "TEXT", "Canonical employee key, EMP-0000.", primary_key=True),
        Column("full_name", "TEXT", "Given and family name from the closed vocabulary.",
               classification=PERSON_NAME),
        Column("work_email", "TEXT", "Synthetic mailbox on contoso.com.", classification=EMAIL),
        Column("work_phone", "TEXT", "Fictional 555-01xx number.", classification=PHONE),
        Column("job_title", "TEXT", "Role.", classification=ENUM),
        Column("department", "TEXT", "Owning department.", classification=ENUM),
        Column("region", "TEXT", "AMER, EMEA or APAC. The scope key.", classification=ENUM),
        Column("location_id", "TEXT", "Home site.", references="locations.location_id"),
        Column("manager_id", "TEXT", "Line manager, NULL for the top of the tree.", nullable=True,
               references="employees.employee_id"),
        Column("hired_on", "TEXT", "ISO hire date.", classification=DATE),
        Column("employment_type", "TEXT", "permanent, fixed_term or contractor.", classification=ENUM),
        Column("cost_centre", "TEXT", "Finance cost centre."),
    ),
)

ORDERS = Table(
    name="orders",
    layer="spine",
    grain="One row per customer order header.",
    description="Order headers. The parent of order lines and the thing invoices settle.",
    filterable=("customer_id", "status", "location_id"),
    columns=(
        Column("order_id", "TEXT", "Canonical order key, ORD-000000.", primary_key=True),
        Column("customer_id", "TEXT", "Ordering customer.", references="customers.customer_id"),
        Column("location_id", "TEXT", "Fulfilling site.", references="locations.location_id"),
        Column("placed_by_employee_id", "TEXT", "Employee who keyed it.", references="employees.employee_id"),
        Column("ordered_on", "TEXT", "ISO order date.", classification=DATE),
        Column("status", "TEXT", "placed, picked, shipped, delivered or cancelled.", classification=ENUM),
        Column("currency", "TEXT", "ISO 4217 code.", classification=ENUM),
        Column("order_total", "REAL", "Sum of the line totals.", classification=MONEY),
    ),
)

ORDER_LINES = Table(
    name="order_lines",
    layer="spine",
    grain="One row per product on an order.",
    description="Order detail. Line totals roll up to the order header, which the tests assert.",
    filterable=("order_id", "product_id"),
    columns=(
        Column("order_line_id", "TEXT", "Canonical line key, OL-000000-00.", primary_key=True),
        Column("order_id", "TEXT", "Parent order.", references="orders.order_id"),
        Column("product_id", "TEXT", "Product ordered.", references="products.product_id"),
        Column("quantity", "INTEGER", "Units ordered.", classification=NUMBER),
        Column("unit_price", "REAL", "Price charged per unit.", classification=MONEY),
        Column("line_total", "REAL", "quantity multiplied by unit_price.", classification=MONEY),
    ),
)

INVOICES = Table(
    name="invoices",
    layer="spine",
    grain="One row per invoice.",
    description="Settlement. Every invoice points at exactly one order and that order's customer.",
    filterable=("customer_id", "order_id", "status"),
    columns=(
        Column("invoice_id", "TEXT", "Canonical invoice key, INV-000000.", primary_key=True),
        Column("order_id", "TEXT", "Order being settled.", references="orders.order_id"),
        Column("customer_id", "TEXT", "Billed customer.", references="customers.customer_id"),
        Column("issued_on", "TEXT", "ISO issue date.", classification=DATE),
        Column("due_on", "TEXT", "ISO due date.", classification=DATE),
        Column("amount_due", "REAL", "Invoiced amount.", classification=MONEY),
        Column("amount_paid", "REAL", "Amount received to date.", classification=MONEY),
        Column("status", "TEXT", "draft, issued, part_paid, paid or overdue.", classification=ENUM),
        Column("currency", "TEXT", "ISO 4217 code.", classification=ENUM),
    ),
)

# --------------------------------------------------------------------------- #
# Layer B — domain data
# --------------------------------------------------------------------------- #

SUPPORT_CASES = Table(
    name="support_cases",
    layer="domain",
    grain="One row per support case.",
    description="Customer support cases against the nonproduction dataset. The only tool-writable table.",
    filterable=("customer_id", "status", "severity", "assigned_employee_id", "product_id"),
    columns=(
        Column("case_id", "TEXT", "Canonical case key, CASE-00000.", primary_key=True),
        Column("customer_id", "TEXT", "Reporting customer.", references="customers.customer_id"),
        Column("product_id", "TEXT", "Product involved, if any.", nullable=True, references="products.product_id"),
        Column("assigned_employee_id", "TEXT", "Owning agent.", references="employees.employee_id"),
        Column("opened_on", "TEXT", "ISO open date.", classification=DATE),
        Column("closed_on", "TEXT", "ISO close date, NULL while open.", nullable=True, classification=DATE),
        Column("severity", "TEXT", "sev1 to sev4.", classification=ENUM),
        Column("status", "TEXT", "new, in_progress, waiting_customer, resolved or closed.", classification=ENUM),
        Column("channel", "TEXT", "phone, email, portal or field.", classification=ENUM),
        Column("subject", "TEXT", "One-line summary.", classification=FREE_TEXT),
    ),
)

SUPPORT_CASE_NOTES = Table(
    name="support_case_notes",
    layer="domain",
    grain="One row per note on a case.",
    description="Case history. Notes marked internal are never returned to a customer-facing caller.",
    filterable=("case_id", "visibility"),
    columns=(
        Column("note_id", "TEXT", "Canonical note key, NOTE-000000.", primary_key=True),
        Column("case_id", "TEXT", "Parent case.", references="support_cases.case_id"),
        Column("author_employee_id", "TEXT", "Author.", references="employees.employee_id"),
        Column("noted_on", "TEXT", "ISO date.", classification=DATE),
        Column("visibility", "TEXT", "customer or internal.", classification=ENUM),
        Column("body", "TEXT", "Note text.", classification=FREE_TEXT),
    ),
)

TRAVEL_ROUTES = Table(
    name="travel_routes",
    layer="domain",
    grain="One row per origin, destination and mode.",
    description="The travel network between Contoso sites. Routes exist independently of fares.",
    filterable=("origin_location_id", "destination_location_id", "mode"),
    columns=(
        Column("route_id", "TEXT", "Canonical route key, ROUTE-0000.", primary_key=True),
        Column("origin_location_id", "TEXT", "Departure site.", references="locations.location_id"),
        Column("destination_location_id", "TEXT", "Arrival site.", references="locations.location_id"),
        Column("mode", "TEXT", "air or rail.", classification=ENUM),
        Column("duration_minutes", "INTEGER", "Scheduled journey time.", classification=NUMBER),
        Column("is_long_haul", "INTEGER", "1 when the sector exceeds eight hours.", classification=NUMBER),
    ),
)

TRAVEL_FARES = Table(
    name="travel_fares",
    layer="domain",
    grain="One row per route and fare class.",
    description="Bookable inventory. A booking must reference a fare that belongs to its route.",
    filterable=("route_id", "fare_class", "refundable"),
    columns=(
        Column("fare_id", "TEXT", "Canonical fare key, FARE-00000.", primary_key=True),
        Column("route_id", "TEXT", "Route priced.", references="travel_routes.route_id"),
        Column("fare_class", "TEXT", "economy, economy_plus or business.", classification=ENUM),
        Column("base_price", "REAL", "Price before taxes.", classification=MONEY),
        Column("currency", "TEXT", "ISO 4217 code.", classification=ENUM),
        Column("refundable", "INTEGER", "1 when the fare is refundable.", classification=NUMBER),
        Column("seats_available", "INTEGER", "Simulated remaining inventory.", classification=NUMBER),
    ),
)

TRAVEL_POLICIES = Table(
    name="travel_policies",
    layer="domain",
    grain="One row per travel policy.",
    description="What a traveller in a region is allowed to book. Curated, not generated.",
    scope_column="region",
    filterable=("region", "policy_id"),
    columns=(
        Column("policy_id", "TEXT", "Canonical policy key.", primary_key=True),
        Column("region", "TEXT", "AMER, EMEA or APAC.", classification=ENUM),
        Column("title", "TEXT", "Policy title.", classification=FREE_TEXT),
        Column("max_fare_class", "TEXT", "Highest permitted fare class.", classification=ENUM),
        Column("advance_purchase_days", "INTEGER", "Minimum notice before departure.", classification=NUMBER),
        Column("requires_approval_over", "REAL", "Ticket value above which approval is needed.",
               classification=MONEY),
        Column("currency", "TEXT", "ISO 4217 code.", classification=ENUM),
        Column("summary", "TEXT", "Plain-language summary.", classification=FREE_TEXT),
    ),
)

TRAVEL_BOOKINGS = Table(
    name="travel_bookings",
    layer="domain",
    grain="One row per trip.",
    description="Simulated bookings held against a traveller, a fare and the policy that governed it.",
    filterable=("traveller_employee_id", "route_id", "status"),
    columns=(
        Column("booking_id", "TEXT", "Canonical booking key, TRIP-00000.", primary_key=True),
        Column("traveller_employee_id", "TEXT", "Traveller.", references="employees.employee_id"),
        Column("route_id", "TEXT", "Route flown or ridden.", references="travel_routes.route_id"),
        Column("fare_id", "TEXT", "Fare purchased.", references="travel_fares.fare_id"),
        Column("policy_id", "TEXT", "Policy in force.", references="travel_policies.policy_id"),
        Column("depart_on", "TEXT", "ISO outbound date.", classification=DATE),
        Column("return_on", "TEXT", "ISO return date, NULL for one way.", nullable=True, classification=DATE),
        Column("booked_on", "TEXT", "ISO date the booking was made.", classification=DATE),
        Column("status", "TEXT", "held, ticketed, cancelled or flown.", classification=ENUM),
        Column("total_price", "REAL", "Amount charged.", classification=MONEY),
        Column("currency", "TEXT", "ISO 4217 code.", classification=ENUM),
        Column("requires_approval", "INTEGER", "1 when the trip breached a policy threshold.",
               classification=NUMBER),
        Column("cost_centre", "TEXT", "Charged cost centre."),
    ),
)

HR_POLICIES = Table(
    name="hr_policies",
    layer="domain",
    grain="One row per HR policy.",
    description="HR policy library. GLOBAL policies are readable by everyone; regional ones are scoped.",
    scope_column="region",
    filterable=("region", "category", "audience"),
    columns=(
        Column("hr_policy_id", "TEXT", "Canonical policy key.", primary_key=True),
        Column("region", "TEXT", "AMER, EMEA, APAC or GLOBAL.", classification=ENUM),
        Column("title", "TEXT", "Policy title.", classification=FREE_TEXT),
        Column("category", "TEXT", "Policy category.", classification=ENUM),
        Column("audience", "TEXT", "all_employees or people_managers.", classification=ENUM),
        Column("effective_on", "TEXT", "ISO effective date.", classification=DATE),
        Column("summary", "TEXT", "Plain-language summary.", classification=FREE_TEXT),
    ),
)

WORK_ORDERS = Table(
    name="work_orders",
    layer="domain",
    grain="One row per field-service work order.",
    description="Field and depot work. Ties a location, an engineer and usually a customer or product together.",
    filterable=("location_id", "assigned_employee_id", "status", "priority", "customer_id"),
    columns=(
        Column("work_order_id", "TEXT", "Canonical work-order key, WO-00000.", primary_key=True),
        Column("location_id", "TEXT", "Where the work happens.", references="locations.location_id"),
        Column("customer_id", "TEXT", "Customer affected, if any.", nullable=True,
               references="customers.customer_id"),
        Column("product_id", "TEXT", "Product involved, if any.", nullable=True, references="products.product_id"),
        Column("assigned_employee_id", "TEXT", "Assigned engineer.", references="employees.employee_id"),
        Column("case_id", "TEXT", "Originating support case, if any.", nullable=True,
               references="support_cases.case_id"),
        Column("opened_on", "TEXT", "ISO raise date.", classification=DATE),
        Column("scheduled_for", "TEXT", "ISO scheduled date.", classification=DATE),
        Column("completed_on", "TEXT", "ISO completion date, NULL while open.", nullable=True, classification=DATE),
        Column("status", "TEXT", "raised, scheduled, in_progress, completed or cancelled.", classification=ENUM),
        Column("priority", "TEXT", "routine, elevated or priority.", classification=ENUM),
        Column("task", "TEXT", "What has to be done.", classification=FREE_TEXT),
    ),
)

# --------------------------------------------------------------------------- #
# Control plane
# --------------------------------------------------------------------------- #

IDENTITIES = Table(
    name="identities",
    layer="control",
    grain="One row per signed-in principal.",
    description=(
        "Maps an immutable directory principal to the employee it is and the regions it may read. "
        "This table is the only place a scope comes from. Nothing a caller says can add a row to it, "
        "and a principal that is absent is denied rather than defaulted."
    ),
    filterable=(),
    columns=(
        Column("principal_oid", "TEXT", "Immutable directory object ID. Opaque in fixtures, a GUID in Entra.",
               primary_key=True),
        Column("tenant_key", "TEXT", "Immutable tenant ID. Opaque in fixtures, a GUID in Entra."),
        Column("employee_id", "TEXT", "Employee the principal is.", references="employees.employee_id"),
        Column("roles", "TEXT", "Pipe-separated role grants.", classification=ENUM),
        Column("scope_regions", "TEXT", "Pipe-separated regions the principal may read.", classification=ENUM),
        Column("persona", "TEXT", "Human label used in fixtures and docs.", classification=FREE_TEXT),
    ),
)

SCHEMA: tuple[Table, ...] = (
    # Order matters: SQLite creates and loads in this sequence, so a parent is
    # always present before a child references it.
    LOCATIONS,
    SUPPLIERS,
    PRODUCT_CATEGORIES,
    PRODUCTS,
    STOCK_LEVELS,
    CUSTOMERS,
    EMPLOYEES,
    ORDERS,
    ORDER_LINES,
    INVOICES,
    SUPPORT_CASES,
    SUPPORT_CASE_NOTES,
    TRAVEL_ROUTES,
    TRAVEL_FARES,
    TRAVEL_POLICIES,
    TRAVEL_BOOKINGS,
    HR_POLICIES,
    WORK_ORDERS,
    IDENTITIES,
)

TABLES_BY_NAME = {table.name: table for table in SCHEMA}


def table_by_name(name: str) -> Table:
    try:
        return TABLES_BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown table {name!r}") from None


def ddl(table: Table) -> str:
    """Render ``CREATE TABLE`` for one table.

    Foreign keys are declared even though SQLite does not enforce them unless
    asked: the loader turns ``PRAGMA foreign_keys`` on, and declaring them means
    the database itself documents the same relationships the tests check.
    """
    lines = [f"CREATE TABLE {table.name} ("]
    body: list[str] = []

    for column in table.columns:
        null = "" if column.nullable else " NOT NULL"
        body.append(f"    {column.name} {column.sql_type}{null}")

    pk = table.primary_key
    if pk:
        body.append(f"    PRIMARY KEY ({', '.join(pk)})")

    for column in table.foreign_keys:
        # A self-reference (employees.manager_id) is legal and intentional.
        body.append(
            f"    FOREIGN KEY ({column.name}) "
            f"REFERENCES {column.referenced_table} ({column.referenced_column})"
        )

    lines.append(",\n".join(body))
    lines.append(");")
    return "\n".join(lines)


def schema_sql() -> str:
    """The whole schema as one deterministic script."""
    return "\n\n".join(ddl(table) for table in SCHEMA) + "\n"
