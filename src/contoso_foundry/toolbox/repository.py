"""One scoped repository, through which every read path goes.

The reason this is a single class rather than a helper each tool calls when it
remembers to is that row-level security fails at the seams. A system with a
carefully scoped ``list`` and an unscoped ``count`` leaks the same information
more slowly; one with a scoped ``list`` and an unscoped ``schema`` leaks the
shape of what it is hiding. So list, count, describe, hierarchy and aggregate
all build their SQL here, and all of them get the scope predicate from the same
place.

The scope predicate is not optional and takes no argument. It is appended by
``_scoped_from`` to every query this class emits, and the only way to reach the
database without it is to not use this class - which is what the tests check.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from contoso_foundry.data.model import PERSONAL_CLASSIFICATIONS, table_by_name
from contoso_foundry.toolbox.identity import RequestScope


class CohortTooSmallError(Exception):
    """Raised when an aggregate would describe too few people to be safe.

    Aggregates are the quiet way to defeat row-level security: if a caller
    cannot read a row but can ask for the average salary of a group of one, they
    have read the row. Suppression is therefore part of the query, not a
    presentation concern.
    """


class ScopeViolationError(Exception):
    """Raised when a caller asks the repository for something outside its remit.

    Distinct from a permission error: this signals a programming mistake - a
    filter on a column that is not exposed, or a request for a table the
    repository refuses to serve - rather than an entitlement decision.
    """


# Tables whose rows belong to the whole company rather than to a region. A
# product catalogue and a route network are the same for everyone; treating them
# as scoped would mean an EMEA agent could not name a product made in APAC,
# which is not a security property, just a broken assistant.
GLOBAL_TABLES = frozenset({"product_categories", "products", "travel_routes", "travel_fares"})

# Tables the repository will never serve, at any scope. The identity table maps
# personas to policy keys; an agent that can read it can enumerate principals.
FORBIDDEN_TABLES = frozenset({"identities"})


@dataclass(frozen=True)
class ScopePath:
    """How a table's rows are attributed to a region.

    Some tables carry a region column. Most do not, and inherit their region
    from whatever they belong to - an order from its customer, a work order from
    its location, a travel booking from its traveller. Encoding that here rather
    than in each tool means a new tool cannot invent a different, weaker answer
    to the same question.
    """

    alias: str
    from_sql: str
    region_expr: str


SCOPE_PATHS: dict[str, ScopePath] = {
    # Region lives on the row itself.
    "locations": ScopePath("t", "locations t", "t.region"),
    "suppliers": ScopePath("t", "suppliers t", "t.region"),
    "customers": ScopePath("t", "customers t", "t.region"),
    "employees": ScopePath("t", "employees t", "t.region"),
    "travel_policies": ScopePath("t", "travel_policies t", "t.region"),
    "hr_policies": ScopePath("t", "hr_policies t", "t.region"),
    # Region is inherited from the owning customer.
    "orders": ScopePath("t", "orders t JOIN customers sc ON sc.customer_id = t.customer_id", "sc.region"),
    "invoices": ScopePath("t", "invoices t JOIN customers sc ON sc.customer_id = t.customer_id", "sc.region"),
    "support_cases": ScopePath(
        "t", "support_cases t JOIN customers sc ON sc.customer_id = t.customer_id", "sc.region"
    ),
    "order_lines": ScopePath(
        "t",
        "order_lines t JOIN orders so ON so.order_id = t.order_id "
        "JOIN customers sc ON sc.customer_id = so.customer_id",
        "sc.region",
    ),
    "support_case_notes": ScopePath(
        "t",
        "support_case_notes t JOIN support_cases scs ON scs.case_id = t.case_id "
        "JOIN customers sc ON sc.customer_id = scs.customer_id",
        "sc.region",
    ),
    # Region is inherited from the location the row sits at.
    "stock_levels": ScopePath("t", "stock_levels t JOIN locations sl ON sl.location_id = t.location_id", "sl.region"),
    "work_orders": ScopePath("t", "work_orders t JOIN locations sl ON sl.location_id = t.location_id", "sl.region"),
    # Region is inherited from the employee the row is about.
    "travel_bookings": ScopePath(
        "t",
        "travel_bookings t JOIN employees se ON se.employee_id = t.traveller_employee_id",
        "se.region",
    ),
}


class ScopedRepository:
    """Every read an agent can perform, with the scope predicate already in it.

    Constructed per request from a resolved ``RequestScope``. It holds no setter
    for that scope, so a tool implementation that receives a repository cannot
    broaden what it can see - the only way to change scope is to resolve a
    different principal, which requires a different authenticated request.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        scope: RequestScope,
        *,
        minimum_cohort: int = 5,
    ) -> None:
        self._connection = connection
        self._scope = scope
        self._minimum_cohort = minimum_cohort

    @property
    def scope(self) -> RequestScope:
        return self._scope

    @property
    def minimum_cohort(self) -> int:
        return self._minimum_cohort

    # ------------------------------------------------------------------
    # Query construction
    # ------------------------------------------------------------------

    def _scoped_from(self, table: str) -> tuple[str, str, list[Any]]:
        """Return the FROM clause, the scope predicate and its parameters.

        Every public method calls this. A method that does not is a bug, and the
        tests assert that the set of methods issuing SQL is exactly the set that
        routes through here.
        """
        if table in FORBIDDEN_TABLES:
            raise ScopeViolationError(f"the table {table!r} is never served through the repository")

        if table in GLOBAL_TABLES:
            # "1 = 1" rather than an empty string so that callers can always
            # concatenate " AND ..." without a special case, and so that a
            # missing predicate is visible in a logged query rather than absent.
            return f"{table} t", "1 = 1", []

        path = SCOPE_PATHS.get(table)
        if path is None:
            raise ScopeViolationError(f"the table {table!r} has no declared scope path")

        regions = self._scope.region_list()
        placeholders = ", ".join("?" for _ in regions)
        return path.from_sql, f"{path.region_expr} IN ({placeholders})", list(regions)

    def _filter_clause(self, table: str, filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        """Translate business filters into SQL, refusing anything not declared filterable.

        The allow-list is the ``filterable`` tuple on the table definition, which
        is also what the contract's parameter schema is generated from. One
        source, so a filter that is accepted here is a filter an agent could have
        legitimately learned about.
        """
        if not filters:
            return "", []

        definition = table_by_name(table)
        allowed = set(definition.filterable)
        clauses: list[str] = []
        params: list[Any] = []

        for key in sorted(filters):
            if key not in allowed:
                raise ScopeViolationError(f"{key!r} is not a filterable column of {table!r}")
            value = filters[key]
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                values = sorted(str(item) for item in value)
                if not values:
                    continue
                placeholders = ", ".join("?" for _ in values)
                clauses.append(f"t.{key} IN ({placeholders})")
                params.extend(values)
            else:
                clauses.append(f"t.{key} = ?")
                params.append(value)

        if not clauses:
            return "", []
        return " AND " + " AND ".join(clauses), params

    def _selectable_columns(self, table: str, *, include_personal: bool) -> list[str]:
        definition = table_by_name(table)
        return [
            column.name
            for column in definition.columns
            if include_personal or column.classification not in PERSONAL_CLASSIFICATIONS
        ]

    # ------------------------------------------------------------------
    # Public read paths
    # ------------------------------------------------------------------

    def list_rows(
        self,
        table: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        include_personal: bool = True,
    ) -> list[dict[str, Any]]:
        """List rows visible to this scope."""
        from_sql, scope_sql, scope_params = self._scoped_from(table)
        filter_sql, filter_params = self._filter_clause(table, filters)
        columns = self._selectable_columns(table, include_personal=include_personal)
        projection = ", ".join(f"t.{name}" for name in columns)
        primary = table_by_name(table).columns[0].name

        # Bounded and ordered: an unbounded tool result is a denial-of-service
        # vector against the model's context, and an unordered one makes the
        # golden fixtures flap.
        limit = max(1, min(int(limit), 200))
        sql = (
            f"SELECT {projection} FROM {from_sql} WHERE {scope_sql}{filter_sql} "
            f"ORDER BY t.{primary} LIMIT {limit}"
        )
        rows = self._connection.execute(sql, [*scope_params, *filter_params]).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def count_rows(self, table: str, *, filters: dict[str, Any] | None = None) -> int:
        """Count rows visible to this scope.

        Shares ``_scoped_from`` with ``list_rows`` for the reason given at the
        top of the module: an unscoped count is a slow read.
        """
        from_sql, scope_sql, scope_params = self._scoped_from(table)
        filter_sql, filter_params = self._filter_clause(table, filters)
        sql = f"SELECT COUNT(*) FROM {from_sql} WHERE {scope_sql}{filter_sql}"
        row = self._connection.execute(sql, [*scope_params, *filter_params]).fetchone()
        return int(row[0])

    def describe(self, table: str) -> dict[str, Any]:
        """Describe a table's agent-visible shape.

        Deliberately reports the filterable columns and the fact that a scope
        applies, without reporting the scope's contents. Telling an agent that
        results are region-scoped helps it explain itself to a user; telling it
        *which* regions invites it to put that in a prompt, and a scope that
        appears in a prompt is a scope that can be argued with.
        """
        # Routed through _scoped_from so that asking for the schema of a
        # forbidden or undeclared table fails exactly as reading it would.
        self._scoped_from(table)
        definition = table_by_name(table)
        return {
            "table": definition.name,
            "layer": definition.layer,
            "grain": definition.grain,
            "description": definition.description,
            "scoped": table not in GLOBAL_TABLES,
            "filterable": list(definition.filterable),
            "columns": [
                {
                    "name": column.name,
                    "type": column.sql_type,
                    "classification": column.classification,
                    "nullable": column.nullable,
                    "references": column.references,
                }
                for column in definition.columns
            ],
        }

    def _fetch_by_key(self, table: str, key_value: str, *, include_personal: bool = True) -> dict[str, Any] | None:
        """Fetch one row by its primary key, still inside scope.

        A primary key is not a business filter, so it does not go through
        ``_filter_clause``; but it very much still goes through ``_scoped_from``,
        because "I already know the id" is not an entitlement.
        """
        from_sql, scope_sql, scope_params = self._scoped_from(table)
        definition = table_by_name(table)
        primary = definition.columns[0].name
        columns = self._selectable_columns(table, include_personal=include_personal)
        projection = ", ".join(f"t.{name}" for name in columns)
        row = self._connection.execute(
            f"SELECT {projection} FROM {from_sql} WHERE {scope_sql} AND t.{primary} = ?",
            [*scope_params, key_value],
        ).fetchone()
        if row is None:
            return None
        return dict(zip(columns, row, strict=True))

    def get_row(self, table: str, key_value: str, *, include_personal: bool = True) -> dict[str, Any] | None:
        """Public single-row lookup by primary key, scoped."""
        return self._fetch_by_key(table, key_value, include_personal=include_personal)

    def hierarchy(self, employee_id: str, *, max_depth: int = 8) -> list[dict[str, Any]]:
        """Walk the management chain upward, stopping at the edge of scope.

        Truncation is the point. A regional partner who follows a chain into a
        global executive should see the chain stop, not see the executive - and
        should not be able to tell whether it stopped because the chain ended or
        because their scope did.

        ``seen`` guards against a cycle in the manager column. The generator will
        not produce one, but a hierarchy walk that can loop forever on bad data
        is a liveness bug waiting for the first data correction.
        """
        chain: list[dict[str, Any]] = []
        current = employee_id
        seen: set[str] = set()

        while current and current not in seen and len(chain) < max_depth:
            seen.add(current)
            record = self._fetch_by_key("employees", current, include_personal=True)
            if record is None:
                break
            chain.append(record)
            current = str(record.get("manager_id") or "")

        return chain

    def aggregate(
        self,
        table: str,
        *,
        group_by: str,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Group and count within scope, suppressing groups below the threshold.

        Returns only groups at or above ``minimum_cohort``. Small groups are
        dropped rather than reported as "suppressed" with their true size,
        because a suppression notice that carries a count is not a suppression.
        """
        from_sql, scope_sql, scope_params = self._scoped_from(table)
        definition = table_by_name(table)
        if group_by not in definition.filterable:
            raise ScopeViolationError(f"{group_by!r} is not a groupable column of {table!r}")
        filter_sql, filter_params = self._filter_clause(table, filters)

        sql = (
            f"SELECT t.{group_by}, COUNT(*) FROM {from_sql} WHERE {scope_sql}{filter_sql} "
            f"GROUP BY t.{group_by} ORDER BY t.{group_by}"
        )
        rows = self._connection.execute(sql, [*scope_params, *filter_params]).fetchall()

        results = [{"group": row[0], "count": int(row[1])} for row in rows if int(row[1]) >= self._minimum_cohort]
        if not results and rows:
            raise CohortTooSmallError(
                f"every group in this aggregate is smaller than the minimum cohort of {self._minimum_cohort}"
            )
        return results

    # ------------------------------------------------------------------
    # The one write path
    # ------------------------------------------------------------------

    # Support cases are the only mutable surface an agent gets, and only these
    # columns. Status and severity are triage decisions an assistant can
    # reasonably make; customer, product and assignment are not, because
    # reassigning a case is a way to move a row into someone else's scope.
    UPDATABLE_SUPPORT_CASE_COLUMNS = frozenset({"status", "severity"})

    def update_support_case(self, case_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Update a support case, but only one already visible to this scope.

        The read-before-write is the security control. Issuing ``UPDATE ...
        WHERE case_id = ?`` would let a caller who guessed an id modify a row
        they could never have read, and the row count would even tell them the
        guess was right.
        """
        existing = self._fetch_by_key("support_cases", case_id)
        if existing is None:
            # Same wording as a genuine miss: a caller must not be able to tell
            # "does not exist" from "exists, but not for you".
            raise ScopeViolationError(f"no support case {case_id!r} is visible to this scope")

        unknown = sorted(set(changes) - self.UPDATABLE_SUPPORT_CASE_COLUMNS)
        if unknown:
            raise ScopeViolationError(f"support cases cannot be updated through these fields: {', '.join(unknown)}")
        if not changes:
            return existing

        assignments = ", ".join(f"{name} = ?" for name in sorted(changes))
        params = [changes[name] for name in sorted(changes)]
        self._connection.execute(
            f"UPDATE support_cases SET {assignments} WHERE case_id = ?",
            [*params, case_id],
        )
        self._connection.commit()

        updated = self._fetch_by_key("support_cases", case_id)
        assert updated is not None  # the row was visible a moment ago and scope is immutable
        return updated