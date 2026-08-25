"""Tests for the tool contracts, the scoped repository and the fail-closed identity path.

The tests that matter most here are the negative ones. It is easy to show that a
scoped query returns rows; the interesting question is whether anything a caller
can say - a filter, a guessed identifier, an extra argument, a rephrased
aggregate - can make it return somebody else's.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from contoso_foundry.data import build as build_mod
from contoso_foundry.toolbox.contracts import (
    FORBIDDEN_PARAMETERS,
    ContractError,
    load_contract,
    load_contracts,
    validate_contracts,
)
from contoso_foundry.toolbox.identity import (
    IdentityResolver,
    Principal,
    UnknownPrincipalError,
    principal_from_fixture,
)
from contoso_foundry.toolbox.repository import (
    GLOBAL_TABLES,
    SCOPE_PATHS,
    CohortTooSmallError,
    ScopedRepository,
    ScopeViolationError,
)
from contoso_foundry.toolbox.smoke import run_smoke
from contoso_foundry.toolbox.tools import Toolbox, ToolError

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "config" / "toolbox"
SPINE_CONFIG = REPO_ROOT / "config" / "data-spine.yaml"
SEED_DIR = REPO_ROOT / "data" / "seed"
FIXTURES_DIR = REPO_ROOT / "data" / "fixtures"

EMEA = ("OID-EMEA-HRBP-01", "TID-CONTOSO-01")
APAC = ("OID-APAC-HRBP-01", "TID-CONTOSO-01")
GLOBAL_HR = ("OID-GLOBAL-HRDIR-01", "TID-CONTOSO-01")
TRAVEL = ("OID-EMEA-TRAVEL-01", "TID-CONTOSO-01")
SUPPORT = ("OID-AMER-SUPLEAD-01", "TID-CONTOSO-01")
OPS = ("OID-APAC-FIELDENG-01", "TID-CONTOSO-01")
PLANNER = ("OID-AMER-PLANNER-01", "TID-CONTOSO-01")
CONTRACTOR = ("OID-EMEA-CONTRACTOR-01", "TID-CONTOSO-01")


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the spine once for the module, into a temporary directory.

    Built rather than read from ``data/build`` so the tests never depend on
    whether somebody happened to run the CLI first, and never mutate a working
    copy - the support-case write test would otherwise leave the tree dirty.
    """
    out = tmp_path_factory.mktemp("spine")
    result = build_mod.build(
        config_path=SPINE_CONFIG,
        seed_dir=SEED_DIR,
        out_dir=out,
        fixtures_dir=FIXTURES_DIR,
    )
    return result.root / "contoso.db"


@pytest.fixture
def connection(database: Path) -> sqlite3.Connection:
    # A fresh connection per test, against a copy-on-write-free file. Writes in
    # the support test are rolled back by rebuilding nothing: they touch a
    # temporary database that only this module sees.
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def toolbox_for(connection: sqlite3.Connection, principal: tuple[str, str], **kwargs: object) -> Toolbox:
    return Toolbox(connection, principal_from_fixture(*principal), contracts_dir=CONTRACTS_DIR, **kwargs)


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #


def test_every_contract_validates() -> None:
    assert validate_contracts(CONTRACTS_DIR) == []


def test_contracts_cover_every_capability_area() -> None:
    capabilities = {contract.capability for contract in load_contracts(CONTRACTS_DIR)}
    assert capabilities == {"customer", "catalog", "orders", "travel", "hr", "support", "operations"}


def test_no_contract_parameter_can_express_an_identity() -> None:
    """The central invariant: nothing a caller sends can choose whose data they read."""
    for contract in load_contracts(CONTRACTS_DIR):
        for tool in contract.tools:
            for name in tool.parameter_names:
                assert name.lower() not in FORBIDDEN_PARAMETERS, f"{tool.name} exposes {name}"


def test_every_contract_resolves_scope_server_side() -> None:
    for contract in load_contracts(CONTRACTS_DIR):
        assert contract.scope_source == "server"
        assert contract.policy_keys == ("oid", "tid")


def test_every_tool_parameter_block_is_closed() -> None:
    for contract in load_contracts(CONTRACTS_DIR):
        for tool in contract.tools:
            assert tool.parameters["additionalProperties"] is False


def test_only_support_declares_a_write() -> None:
    writes = {
        tool.name
        for contract in load_contracts(CONTRACTS_DIR)
        for tool in contract.tools
        if tool.side_effect == "write"
    }
    assert writes == {"support_update_case"}


def test_booking_is_declared_a_simulation() -> None:
    contract = load_contract(CONTRACTS_DIR / "travel.yaml")
    assert contract.tool("travel_simulate_booking").side_effect == "simulate"


def test_every_declared_tool_has_an_implementation(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, EMEA)
    declared = {tool.name for contract in toolbox.contracts for tool in contract.tools}
    assert declared == set(toolbox.tool_names())


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_PARAMETERS)[:8])
def test_a_contract_declaring_a_scope_parameter_is_rejected(tmp_path: Path, forbidden: str) -> None:
    """A negative test on the validator itself, so the gate is known to bite."""
    document = f"""
capability: demo
version: 1.0.0
title: Demo
summary: Demo
identity:
  scope_source: server
  policy_keys: [oid, tid]
tools:
  - name: demo_read
    title: Demo
    summary: Demo
    side_effect: read
    data_classification: internal
    required_roles: [demo.read]
    parameters:
      type: object
      additionalProperties: false
      properties:
        {forbidden}:
          type: string
          description: Should never be allowed.
      required: []
    returns:
      type: array
      description: Rows.
"""
    path = tmp_path / "demo.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ContractError, match="scope is resolved server-side"):
        load_contract(path)


def test_a_contract_taking_scope_from_the_caller_is_rejected(tmp_path: Path) -> None:
    document = """
capability: demo
version: 1.0.0
title: Demo
summary: Demo
identity:
  scope_source: caller
  policy_keys: [oid, tid]
tools:
  - name: demo_read
    title: Demo
    summary: Demo
    side_effect: read
    data_classification: internal
    required_roles: [demo.read]
    parameters: {type: object, additionalProperties: false, properties: {}}
    returns: {type: array, description: Rows.}
"""
    path = tmp_path / "demo.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ContractError, match="scope_source must be 'server'"):
        load_contract(path)


def test_an_open_parameter_block_is_rejected(tmp_path: Path) -> None:
    document = """
capability: demo
version: 1.0.0
title: Demo
summary: Demo
identity:
  scope_source: server
  policy_keys: [oid, tid]
tools:
  - name: demo_read
    title: Demo
    summary: Demo
    side_effect: read
    data_classification: internal
    required_roles: [demo.read]
    parameters: {type: object, properties: {}}
    returns: {type: array, description: Rows.}
"""
    path = tmp_path / "demo.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ContractError, match="additionalProperties: false"):
        load_contract(path)


def test_a_non_semver_version_is_rejected(tmp_path: Path) -> None:
    document = """
capability: demo
version: '2026-01-05'
title: Demo
summary: Demo
identity:
  scope_source: server
  policy_keys: [oid, tid]
tools:
  - name: demo_read
    title: Demo
    summary: Demo
    side_effect: read
    data_classification: internal
    required_roles: [demo.read]
    parameters: {type: object, additionalProperties: false, properties: {}}
    returns: {type: array, description: Rows.}
"""
    path = tmp_path / "demo.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ContractError, match="not semver"):
        load_contract(path)


def test_contract_id_patterns_match_the_generated_identifiers(connection: sqlite3.Connection) -> None:
    """Guards against a pattern drifting away from the ids the generator emits.

    A contract that declares ``^CUS-\\d{4}$`` while the spine emits ``CUST-00001``
    is a contract every real call fails, and nothing else in the suite would
    notice until an agent tried to use it.
    """
    import re

    table_for_parameter = {
        "customer_id": ("customers", "customer_id"),
        "product_id": ("products", "product_id"),
        "order_id": ("orders", "order_id"),
        "invoice_id": ("invoices", "invoice_id"),
        "route_id": ("travel_routes", "route_id"),
        "fare_id": ("travel_fares", "fare_id"),
        "case_id": ("support_cases", "case_id"),
        "work_order_id": ("work_orders", "work_order_id"),
        "employee_id": ("employees", "employee_id"),
    }

    checked = 0
    for contract in load_contracts(CONTRACTS_DIR):
        for tool in contract.tools:
            for name, definition in tool.parameters.get("properties", {}).items():
                pattern = definition.get("pattern")
                if not pattern or name not in table_for_parameter:
                    continue
                table, column = table_for_parameter[name]
                row = connection.execute(f"SELECT {column} FROM {table} LIMIT 1").fetchone()
                assert row is not None, f"{table} is empty"
                assert re.match(pattern, str(row[0])), (
                    f"{tool.name}.{name} declares {pattern} but {table}.{column} looks like {row[0]!r}"
                )
                checked += 1
    assert checked >= 8


# --------------------------------------------------------------------------- #
# Identity: fail closed
# --------------------------------------------------------------------------- #


def test_an_unknown_principal_is_refused(connection: sqlite3.Connection) -> None:
    with pytest.raises(UnknownPrincipalError):
        IdentityResolver(connection).resolve(Principal("OID-NOT-REAL", "TID-CONTOSO-01"))


def test_a_known_oid_in_the_wrong_tenant_is_refused(connection: sqlite3.Connection) -> None:
    """A valid principal from another tenant must not resolve. Cross-tenant is the worst leak."""
    with pytest.raises(UnknownPrincipalError):
        IdentityResolver(connection).resolve(Principal("OID-EMEA-HRBP-01", "TID-SOMEBODY-ELSE"))


def test_an_empty_principal_cannot_be_constructed() -> None:
    with pytest.raises(UnknownPrincipalError):
        Principal("", "TID-CONTOSO-01")
    with pytest.raises(UnknownPrincipalError):
        Principal("OID-EMEA-HRBP-01", "")


def test_an_unknown_principal_fails_before_any_tool_exists(connection: sqlite3.Connection) -> None:
    """Fail-closed is structural: there is no Toolbox to call, not an empty one."""
    with pytest.raises(UnknownPrincipalError):
        toolbox_for(connection, ("OID-NOT-REAL", "TID-CONTOSO-01"))


def test_the_refusal_message_does_not_distinguish_unknown_oid_from_unknown_tenant(
    connection: sqlite3.Connection,
) -> None:
    """Differentiated errors let a caller enumerate valid identifiers."""
    resolver = IdentityResolver(connection)
    messages = set()
    for principal in (
        Principal("OID-NOT-REAL", "TID-CONTOSO-01"),
        Principal("OID-EMEA-HRBP-01", "TID-NOT-REAL"),
    ):
        with pytest.raises(UnknownPrincipalError) as caught:
            resolver.resolve(principal)
        messages.add(str(caught.value))
    assert len(messages) == 1


# --------------------------------------------------------------------------- #
# The money shot: same prompt, two people, no overlap
# --------------------------------------------------------------------------- #


def test_the_same_prompt_returns_disjoint_rosters(connection: sqlite3.Connection) -> None:
    arguments = {"limit": 200}
    emea = toolbox_for(connection, EMEA).call("hr_search_roster", dict(arguments))
    apac = toolbox_for(connection, APAC).call("hr_search_roster", dict(arguments))

    emea_ids = {row["employee_id"] for row in emea}
    apac_ids = {row["employee_id"] for row in apac}

    assert emea_ids, "the EMEA partner saw nobody"
    assert apac_ids, "the APAC partner saw nobody"
    assert emea_ids & apac_ids == set(), "the two populations overlap"
    assert {row["region"] for row in emea} == {"EMEA"}
    assert {row["region"] for row in apac} == {"APAC"}


def test_counts_agree_with_the_rows_each_caller_can_see(connection: sqlite3.Connection) -> None:
    """An unscoped count would be a slow read of the rows it counts."""
    for principal, region in ((EMEA, "EMEA"), (APAC, "APAC")):
        toolbox = toolbox_for(connection, principal)
        rows = toolbox.call("hr_search_roster", {"limit": 200})
        counted = toolbox.call("hr_count_roster", {})
        assert counted["count"] == len(rows)
        assert {row["region"] for row in rows} == {region}


def test_asking_for_another_region_returns_nothing_rather_than_that_region(
    connection: sqlite3.Connection,
) -> None:
    """The filter narrows inside the scope; it never replaces it."""
    rows = toolbox_for(connection, EMEA).call("hr_search_roster", {"region": "APAC", "limit": 200})
    assert rows == []


def test_the_global_director_sees_all_three_regions(connection: sqlite3.Connection) -> None:
    """Scope widening is a property of the directory, not of the request."""
    rows = toolbox_for(connection, GLOBAL_HR).call("hr_search_roster", {"limit": 200})
    assert {row["region"] for row in rows} == {"AMER", "EMEA", "APAC"}


def test_a_regional_partner_sees_strictly_fewer_people_than_the_director(
    connection: sqlite3.Connection,
) -> None:
    director = toolbox_for(connection, GLOBAL_HR).call("hr_count_roster", {})
    partner = toolbox_for(connection, EMEA).call("hr_count_roster", {})
    assert partner["count"] < director["count"]


# --------------------------------------------------------------------------- #
# Injection attempts
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "injected",
    [
        {"oid": "OID-APAC-HRBP-01"},
        {"tid": "TID-CONTOSO-01"},
        {"scope": "APAC"},
        {"scope_regions": "AMER|EMEA|APAC"},
        {"region_override": "APAC"},
        {"on_behalf_of": "EMP-0022"},
        {"impersonate": "OID-GLOBAL-HRDIR-01"},
        {"roles": "hr.read"},
        {"include_all_regions": True},
        {"unscoped": True},
    ],
)
def test_scope_cannot_be_injected_through_tool_arguments(
    connection: sqlite3.Connection, injected: dict[str, object]
) -> None:
    """Every one of these is refused as an undeclared argument, not quietly ignored."""
    toolbox = toolbox_for(connection, EMEA)
    with pytest.raises(ToolError, match="unexpected argument"):
        toolbox.call("hr_search_roster", {"limit": 5, **injected})


def test_a_filter_on_an_undeclared_column_is_refused(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, EMEA)
    with pytest.raises(ToolError, match="unexpected argument"):
        toolbox.call("hr_search_roster", {"work_email": "someone@contoso.com"})


def test_the_repository_refuses_a_filter_outside_the_allow_list(connection: sqlite3.Connection) -> None:
    """Defence in depth: even reached directly, the repository refuses."""
    scope = IdentityResolver(connection).resolve(principal_from_fixture(*EMEA))
    repository = ScopedRepository(connection, scope)
    with pytest.raises(ScopeViolationError, match="not a filterable column"):
        repository.list_rows("employees", filters={"work_email": "x"})


def test_the_repository_never_serves_the_identity_table(connection: sqlite3.Connection) -> None:
    """An agent that can read the identity map can enumerate principals."""
    scope = IdentityResolver(connection).resolve(principal_from_fixture(*GLOBAL_HR))
    repository = ScopedRepository(connection, scope)
    for call in (
        lambda: repository.list_rows("identities"),
        lambda: repository.count_rows("identities"),
        lambda: repository.describe("identities"),
        lambda: repository.get_row("identities", "OID-EMEA-HRBP-01"),
    ):
        with pytest.raises(ScopeViolationError, match="never served"):
            call()


def test_a_table_with_no_declared_scope_path_is_refused(connection: sqlite3.Connection) -> None:
    """A new table is denied until somebody decides how it is scoped."""
    scope = IdentityResolver(connection).resolve(principal_from_fixture(*EMEA))
    repository = ScopedRepository(connection, scope)
    with pytest.raises(ScopeViolationError, match="no declared scope path"):
        repository.list_rows("sqlite_master")


def test_every_table_is_either_global_or_has_a_scope_path() -> None:
    """No table may be reachable without an explicit scoping decision."""
    from contoso_foundry.data.model import SCHEMA
    from contoso_foundry.toolbox.repository import FORBIDDEN_TABLES

    for table in SCHEMA:
        assert (
            table.name in GLOBAL_TABLES or table.name in SCOPE_PATHS or table.name in FORBIDDEN_TABLES
        ), f"{table.name} has no scoping decision"


# --------------------------------------------------------------------------- #
# Aggregates and cohort suppression
# --------------------------------------------------------------------------- #


def test_an_aggregate_is_scoped_like_a_list(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, EMEA)
    groups = toolbox.call("hr_aggregate_roster", {"group_by": "region"})
    assert {group["group"] for group in groups} == {"EMEA"}


def test_every_surviving_group_meets_the_cohort_floor(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, EMEA, minimum_cohort=5)
    groups = toolbox.call("hr_aggregate_roster", {"group_by": "department"})
    assert groups
    assert all(group["count"] >= 5 for group in groups)


def test_small_cohorts_are_withheld_from_the_aggregate(connection: sqlite3.Connection) -> None:
    """The suppression must actually remove people, not merely be available to.

    Compares the aggregate's coverage against the caller's own scoped count: if
    the groups add up to fewer people than the caller can see, a group was
    withheld.
    """
    toolbox = toolbox_for(connection, EMEA, minimum_cohort=5)
    total = toolbox.call("hr_count_roster", {})["count"]
    groups = toolbox.call("hr_aggregate_roster", {"group_by": "department"})
    covered = sum(group["count"] for group in groups)
    assert covered < total, "no group fell below the cohort floor, so suppression is untested here"


def test_raising_the_cohort_floor_withholds_more(connection: sqlite3.Connection) -> None:
    """Derive the floor from the data rather than guessing a number.

    A hard-coded floor either withholds nothing or everything depending on how
    the generator happens to distribute people, which makes the test a statement
    about this week's volumes instead of about the suppression rule.
    """
    baseline = toolbox_for(connection, EMEA, minimum_cohort=1).call("hr_aggregate_roster", {"group_by": "department"})
    assert len(baseline) > 1, "one group cannot demonstrate a floor"

    # Just above the smallest surviving group, so exactly that group must drop.
    floor = min(group["count"] for group in baseline) + 1
    raised = toolbox_for(connection, EMEA, minimum_cohort=floor).call("hr_aggregate_roster", {"group_by": "department"})

    assert len(raised) < len(baseline)
    assert sum(group["count"] for group in raised) < sum(group["count"] for group in baseline)


def test_an_aggregate_where_every_group_is_small_is_refused(connection: sqlite3.Connection) -> None:
    """Returning an empty list would be indistinguishable from "no such people"."""
    toolbox = toolbox_for(connection, EMEA, minimum_cohort=10_000)
    with pytest.raises(CohortTooSmallError):
        toolbox.call("hr_aggregate_roster", {"group_by": "department"})


def test_an_aggregate_cannot_group_by_an_undeclared_column(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, EMEA)
    with pytest.raises(ToolError, match="must be one of"):
        toolbox.call("hr_aggregate_roster", {"group_by": "full_name"})


def test_the_aggregate_role_is_separate_from_the_read_role(connection: sqlite3.Connection) -> None:
    """A caller who may read rows is not automatically allowed to aggregate them."""
    toolbox = toolbox_for(connection, TRAVEL)
    with pytest.raises(PermissionError):
        toolbox.call("hr_aggregate_roster", {"group_by": "department"})


# --------------------------------------------------------------------------- #
# Schema and hierarchy paths are scoped too
# --------------------------------------------------------------------------- #


def test_the_schema_path_does_not_disclose_the_scope_contents(connection: sqlite3.Connection) -> None:
    described = toolbox_for(connection, EMEA).call("hr_describe_roster", {})
    assert described["scoped"] is True
    rendered = repr(described)
    assert "EMEA" not in rendered and "APAC" not in rendered


def test_the_schema_path_never_exposes_a_non_filterable_column_as_filterable(
    connection: sqlite3.Connection,
) -> None:
    described = toolbox_for(connection, EMEA).call("hr_describe_roster", {})
    assert "full_name" not in described["filterable"]
    assert "work_email" not in described["filterable"]


def test_a_management_chain_stops_at_the_edge_of_scope(connection: sqlite3.Connection) -> None:
    """The regional partner's chain must not run into a global executive."""
    emea = toolbox_for(connection, EMEA)
    chain = emea.call("hr_get_management_chain", {"employee_id": emea.scope.employee_id})
    assert chain
    assert {link["region"] for link in chain} == {"EMEA"}


def test_the_global_director_sees_a_longer_chain_than_the_regional_partner(
    connection: sqlite3.Connection,
) -> None:
    emea = toolbox_for(connection, EMEA)
    director = toolbox_for(connection, GLOBAL_HR)
    subject = emea.scope.employee_id
    assert len(director.call("hr_get_management_chain", {"employee_id": subject})) > len(
        emea.call("hr_get_management_chain", {"employee_id": subject})
    )


def test_a_chain_starting_outside_scope_returns_nothing(connection: sqlite3.Connection) -> None:
    apac_employee = toolbox_for(connection, APAC).scope.employee_id
    assert toolbox_for(connection, EMEA).call("hr_get_management_chain", {"employee_id": apac_employee}) == []


# --------------------------------------------------------------------------- #
# Cross-capability scoping
# --------------------------------------------------------------------------- #


def test_orders_inherit_scope_from_their_customer(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, PLANNER)
    orders = toolbox.call("orders_search_orders", {"limit": 200})
    assert orders
    visible = {row["customer_id"] for row in toolbox.call("customer_search", {"limit": 200})}
    assert {row["customer_id"] for row in orders} <= visible


def test_a_guessed_out_of_scope_identifier_looks_like_a_miss(connection: sqlite3.Connection) -> None:
    """Knowing an id is not an entitlement, and the response must not confirm the guess."""
    apac = toolbox_for(connection, OPS)
    apac_work_orders = apac.call("operations_search_work_orders", {"limit": 5})
    assert apac_work_orders

    amer = toolbox_for(connection, SUPPORT)
    target = apac_work_orders[0]["work_order_id"]
    with pytest.raises(PermissionError):
        # The support lead holds no ops role, so this fails on entitlement first.
        amer.call("operations_lookup_work_order", {"work_order_id": target})


def test_an_out_of_scope_lookup_by_id_returns_nothing(connection: sqlite3.Connection) -> None:
    apac_ops = toolbox_for(connection, OPS)
    target = apac_ops.call("operations_search_work_orders", {"limit": 1})[0]["work_order_id"]

    # The EMEA contractor holds the same ops role but a different region, which
    # isolates the scope decision from the entitlement decision.
    emea_ops = toolbox_for(connection, ("OID-EMEA-CONTRACTOR-01", "TID-CONTOSO-01"))
    assert emea_ops.call("operations_lookup_work_order", {"work_order_id": target}) is None


def test_the_catalogue_is_deliberately_company_wide(connection: sqlite3.Connection) -> None:
    """Not everything is scoped, and pretending otherwise would break the assistant."""
    products = toolbox_for(connection, PLANNER).call("catalog_search_products", {"limit": 200})
    assert len(products) > 50


def test_stock_is_scoped_even_though_the_catalogue_is_not(connection: sqlite3.Connection) -> None:
    planner = toolbox_for(connection, PLANNER)
    stock = planner.call("catalog_check_stock", {"limit": 200})
    assert stock
    in_scope = {
        row[0] for row in connection.execute("SELECT location_id FROM locations WHERE region = 'AMER'")
    }
    assert {row["location_id"] for row in stock} <= in_scope


# --------------------------------------------------------------------------- #
# Travel
# --------------------------------------------------------------------------- #


def test_the_travel_policy_tool_takes_no_arguments(connection: sqlite3.Connection) -> None:
    """There is nothing to argue with: the applicable policy follows from the scope."""
    contract = load_contract(CONTRACTS_DIR / "travel.yaml")
    assert contract.tool("travel_get_policy").parameter_names == ()


def test_two_regions_get_different_travel_policies(connection: sqlite3.Connection) -> None:
    emea = {row["policy_id"] for row in toolbox_for(connection, TRAVEL).call("travel_get_policy", {})}
    apac_ops = {row["policy_id"] for row in toolbox_for(connection, OPS).call("travel_get_policy", {})}
    assert emea and apac_ops
    assert emea & apac_ops == set()


def test_a_booking_simulation_writes_nothing(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, TRAVEL)
    route = toolbox.call("travel_search_routes", {"limit": 1})[0]
    fare = toolbox.call("travel_search_fares", {"route_id": route["route_id"], "limit": 1})[0]

    before = connection.execute("SELECT COUNT(*) FROM travel_bookings").fetchone()[0]
    seats_before = connection.execute(
        "SELECT seats_available FROM travel_fares WHERE fare_id = ?", (fare["fare_id"],)
    ).fetchone()[0]

    result = toolbox.call(
        "travel_simulate_booking",
        {"route_id": route["route_id"], "fare_id": fare["fare_id"], "depart_on": "2026-03-04"},
    )

    assert result["simulated"] is True
    assert connection.execute("SELECT COUNT(*) FROM travel_bookings").fetchone()[0] == before
    assert (
        connection.execute(
            "SELECT seats_available FROM travel_fares WHERE fare_id = ?", (fare["fare_id"],)
        ).fetchone()[0]
        == seats_before
    ), "the simulation moved real inventory"


def test_a_simulation_is_repeatable(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, TRAVEL)
    route = toolbox.call("travel_search_routes", {"limit": 1})[0]
    fare = toolbox.call("travel_search_fares", {"route_id": route["route_id"], "limit": 1})[0]
    arguments = {"route_id": route["route_id"], "fare_id": fare["fare_id"], "depart_on": "2026-03-04"}
    assert toolbox.call("travel_simulate_booking", dict(arguments)) == toolbox.call(
        "travel_simulate_booking", dict(arguments)
    )


def test_a_fare_from_another_route_is_refused(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, TRAVEL)
    routes = toolbox.call("travel_search_routes", {"limit": 5})
    first = toolbox.call("travel_search_fares", {"route_id": routes[0]["route_id"], "limit": 1})[0]
    with pytest.raises(ToolError, match="not published for route"):
        toolbox.call(
            "travel_simulate_booking",
            {"route_id": routes[1]["route_id"], "fare_id": first["fare_id"], "depart_on": "2026-03-04"},
        )


def test_booking_requires_a_role_reading_does_not(connection: sqlite3.Connection) -> None:
    planner = toolbox_for(connection, ("OID-AMER-PLANNER-01", "TID-CONTOSO-01"))
    with pytest.raises(PermissionError):
        planner.call("travel_search_routes", {"limit": 1})


# --------------------------------------------------------------------------- #
# Support: the one write
# --------------------------------------------------------------------------- #


def test_a_case_can_be_retriaged_within_scope(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, SUPPORT)
    case = toolbox.call("support_search_cases", {"limit": 1})[0]
    updated = toolbox.call("support_update_case", {"case_id": case["case_id"], "severity": "sev-4"})
    assert updated["severity"] == "sev-4"


def test_a_case_outside_scope_cannot_be_written(connection: sqlite3.Connection) -> None:
    """Read-before-write: a guessed id must fail as though it did not exist."""
    apac_case = toolbox_for(connection, APAC)
    # The APAC people partner cannot search cases, so take an out-of-region id
    # straight from the database - the strongest form of "the caller guessed it".
    row = connection.execute(
        "SELECT sc.case_id FROM support_cases sc JOIN customers c ON c.customer_id = sc.customer_id "
        "WHERE c.region = 'APAC' LIMIT 1"
    ).fetchone()
    assert row is not None
    del apac_case

    amer = toolbox_for(connection, SUPPORT)
    with pytest.raises(ScopeViolationError, match="visible to this scope"):
        amer.call("support_update_case", {"case_id": row[0], "severity": "sev-1"})


def test_a_case_cannot_be_reassigned_through_the_update(connection: sqlite3.Connection) -> None:
    """Reassignment would move a row into somebody else's scope."""
    toolbox = toolbox_for(connection, SUPPORT)
    case = toolbox.call("support_search_cases", {"limit": 1})[0]
    with pytest.raises(ToolError, match="unexpected argument"):
        toolbox.call("support_update_case", {"case_id": case["case_id"], "assigned_employee_id": "EMP-0001"})


def test_internal_notes_are_withheld_from_read_only_callers(connection: sqlite3.Connection) -> None:
    """Pick an internal note that is definitely inside the support lead's region.

    Sampling any internal note would make the test skip whenever the generator
    happened to place it elsewhere, and a test that sometimes does not run is not
    a gate.
    """
    row = connection.execute(
        "SELECT n.case_id FROM support_case_notes n "
        "JOIN support_cases sc ON sc.case_id = n.case_id "
        "JOIN customers c ON c.customer_id = sc.customer_id "
        "WHERE n.visibility = 'internal' AND c.region = 'AMER' LIMIT 1"
    ).fetchone()
    assert row is not None, "the generator produced no internal notes in the AMER region"

    lead = toolbox_for(connection, SUPPORT)
    case = lead.call("support_lookup_case", {"case_id": row[0], "include_notes": True})
    assert case is not None
    assert any(note["visibility"] == "internal" for note in case["notes"])

    # The same case without notes requested must not leak them by another route.
    quiet = lead.call("support_lookup_case", {"case_id": row[0]})
    assert "notes" not in quiet


# --------------------------------------------------------------------------- #
# Argument validation
# --------------------------------------------------------------------------- #


def test_a_malformed_identifier_is_refused(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, SUPPORT)
    with pytest.raises(ToolError, match="does not match"):
        toolbox.call("customer_lookup", {"customer_id": "'; DROP TABLE customers; --"})


def test_the_row_cap_is_enforced_server_side(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, GLOBAL_HR)
    with pytest.raises(ToolError, match="at most 200"):
        toolbox.call("hr_search_roster", {"limit": 10_000})


def test_a_missing_required_argument_is_refused(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, SUPPORT)
    with pytest.raises(ToolError, match="missing required argument"):
        toolbox.call("customer_lookup", {})


def test_a_wrongly_typed_argument_is_refused(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, GLOBAL_HR)
    with pytest.raises(ToolError, match="must be an integer"):
        toolbox.call("hr_search_roster", {"limit": "all of them"})


def test_calling_an_undeclared_tool_is_refused(connection: sqlite3.Connection) -> None:
    with pytest.raises(ToolError, match="no such tool"):
        toolbox_for(connection, EMEA).call("hr_export_everything", {})


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def test_the_audit_trail_records_calls_without_copying_the_roster(connection: sqlite3.Connection) -> None:
    toolbox = toolbox_for(connection, EMEA)
    toolbox.call("hr_search_roster", {"limit": 5})
    toolbox.call("hr_count_roster", {})

    assert [entry.tool for entry in toolbox.audit] == ["hr_search_roster", "hr_count_roster"]
    rendered = repr(toolbox.audit)
    assert "EMP-" not in rendered, "the audit trail is quoting employee identifiers"
    assert "@" not in rendered, "the audit trail is quoting email addresses"


# --------------------------------------------------------------------------- #
# The smoke client
# --------------------------------------------------------------------------- #


def test_the_smoke_client_demonstrates_isolation(database: Path) -> None:
    result = run_smoke(database, CONTRACTS_DIR)
    assert result.ok()
    assert result.overlap == set()
    assert result.unknown_principal_failed


def test_the_smoke_client_exercises_the_required_capabilities(database: Path) -> None:
    """The acceptance criterion: customer context, travel and a scoped HR tool."""
    exercised = run_smoke(database, CONTRACTS_DIR).tools_exercised
    assert "customer_get_caller_context" in exercised
    assert any(name.startswith("travel_") for name in exercised)
    assert "hr_search_roster" in exercised
    assert "travel_simulate_booking" in exercised


def test_no_contract_declares_a_file_export(connection: sqlite3.Connection) -> None:
    """Export is omitted on purpose.

    A safe export needs private, short-lived, user-bound storage, formula
    neutralisation, a retention policy and an audit trail. None of those exist in
    this layer, so the capability does not either - a half-built export is a data
    exfiltration route with a progress bar.
    """
    for contract in load_contracts(CONTRACTS_DIR):
        for tool in contract.tools:
            assert "export" not in tool.name
            assert "download" not in tool.name