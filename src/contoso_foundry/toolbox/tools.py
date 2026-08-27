"""Adapters that bind each declared tool to the scoped repository.

The contracts say what may be asked. This module answers, and it is deliberately
thin: an adapter validates its arguments against the declared schema, checks the
caller's roles, and hands the rest to ``ScopedRepository``. Business logic that
grew here would be business logic outside the scope predicate.

There is no framework in this file. A ``Toolbox`` is a dictionary of callables
keyed by contract name; publishing it to Foundry, exposing it over MCP or calling
it from a test are all the same operation with different glue.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contoso_foundry.data.model import TRAVEL_EXCEPTION_DEPARTMENTS
from contoso_foundry.data.runtime import load_runtime_policy
from contoso_foundry.toolbox.contracts import ToolboxContract, load_contracts
from contoso_foundry.toolbox.identity import IdentityResolver, Principal, RequestScope
from contoso_foundry.toolbox.repository import ScopedRepository, ScopeViolationError


class ToolError(Exception):
    """Raised when a tool is called with arguments its contract forbids."""


@dataclass(frozen=True)
class ToolCall:
    """One invocation, recorded for the audit trail.

    Every call is logged with the persona rather than with anything
    reconstructible into a person, so the trail is useful for debugging a scope
    decision without becoming a second copy of the roster.
    """

    tool: str
    persona: str
    argument_names: tuple[str, ...]
    result_rows: int
    outcome: str


def _validate_arguments(contract_tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Check arguments against the declared parameter schema.

    A deliberately small JSON Schema subset - the keywords the contracts actually
    use. Pulling in a full validator would add a dependency to a project that
    justifies each one in review, and would validate keywords no contract is
    allowed to contain anyway.
    """
    schema = contract_tool.parameters
    properties: dict[str, Any] = schema.get("properties", {})

    unexpected = sorted(set(arguments) - set(properties))
    if unexpected:
        # This is the injection guard as much as a typo guard: the forbidden
        # scope parameter names are rejected here even if a caller invents one,
        # because anything undeclared is refused.
        raise ToolError(f"{contract_tool.name}: unexpected argument(s): {', '.join(unexpected)}")

    missing = sorted(set(schema.get("required", [])) - set(arguments))
    if missing:
        raise ToolError(f"{contract_tool.name}: missing required argument(s): {', '.join(missing)}")

    cleaned: dict[str, Any] = {}
    for name, value in arguments.items():
        if value is None:
            if name in schema.get("required", []):
                raise ToolError(f"{contract_tool.name}: required argument {name!r} cannot be null")
            continue
        definition = properties[name]
        declared = definition.get("type")

        if declared == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"{contract_tool.name}: {name!r} must be an integer")
        elif declared == "boolean":
            if not isinstance(value, bool):
                raise ToolError(f"{contract_tool.name}: {name!r} must be a boolean")
        elif declared == "string":
            if not isinstance(value, str):
                raise ToolError(f"{contract_tool.name}: {name!r} must be a string")
            pattern = definition.get("pattern")
            if pattern and not re.match(pattern, value):
                raise ToolError(f"{contract_tool.name}: {name!r} does not match {pattern}")

        choices = definition.get("enum")
        if choices is not None and value not in choices:
            raise ToolError(f"{contract_tool.name}: {name!r} must be one of {choices}")

        minimum = definition.get("minimum")
        maximum = definition.get("maximum")
        if isinstance(value, int) and not isinstance(value, bool):
            if minimum is not None and value < minimum:
                raise ToolError(f"{contract_tool.name}: {name!r} must be at least {minimum}")
            if maximum is not None and value > maximum:
                raise ToolError(f"{contract_tool.name}: {name!r} must be at most {maximum}")

        cleaned[name] = value

    return cleaned


def _validate_result(contract_tool: Any, result: Any) -> None:
    declared = contract_tool.returns.get("type")
    choices = [declared] if isinstance(declared, str) else list(declared or [])
    predicates = {
        "array": lambda value: isinstance(value, list),
        "boolean": lambda value: isinstance(value, bool),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "null": lambda value: value is None,
        "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
        "object": lambda value: isinstance(value, dict),
        "string": lambda value: isinstance(value, str),
    }
    if not choices or any(kind in predicates and predicates[kind](result) for kind in choices):
        return
    raise ToolError(f"{contract_tool.name}: implementation returned a value outside its declared schema {choices}")


class Toolbox:
    """The callable surface an agent sees, bound to one authenticated principal.

    Constructed per request. The scope is resolved once at construction and held
    immutable for the lifetime of the object, so a sequence of tool calls within
    one request cannot end up straddling two scopes.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        principal: Principal,
        *,
        contracts_dir: Path,
        minimum_cohort: int | None = None,
        as_of: dt.date | None = None,
        spine_config: Path | None = None,
    ) -> None:
        if minimum_cohort is None or as_of is None:
            runtime = load_runtime_policy(spine_config or contracts_dir.parent / "data-spine.yaml")
            minimum_cohort = runtime.minimum_cohort if minimum_cohort is None else minimum_cohort
            as_of = runtime.as_of if as_of is None else as_of
        self._connection = connection
        # Resolution happens here, which means an unknown principal fails before
        # a single tool exists to be called - the fail-closed path is structural
        # rather than a check each adapter has to remember.
        self._scope: RequestScope = IdentityResolver(connection).resolve(principal)
        self._repository = ScopedRepository(connection, self._scope, minimum_cohort=minimum_cohort)
        self._minimum_cohort = minimum_cohort
        self._as_of = as_of
        self._contracts: list[ToolboxContract] = load_contracts(contracts_dir)
        self._audit: list[ToolCall] = []
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = self._build_handlers()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def scope(self) -> RequestScope:
        return self._scope

    @property
    def audit(self) -> list[ToolCall]:
        return list(self._audit)

    @property
    def minimum_cohort(self) -> int:
        return self._minimum_cohort

    @property
    def contracts(self) -> list[ToolboxContract]:
        return list(self._contracts)

    def tool_names(self) -> list[str]:
        return sorted(self._handlers)

    def _find_tool(self, name: str) -> Any:
        for contract in self._contracts:
            for tool in contract.tools:
                if tool.name == name:
                    return tool
        raise ToolError(f"no such tool: {name}")

    # ------------------------------------------------------------------
    # Invocation
    # ------------------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke a tool by contract name.

        The order is not incidental: arguments are validated before roles are
        checked, so a malformed call fails the same way whether or not the caller
        was entitled to make a well-formed one.
        """
        argument_names: tuple[str, ...] = ()
        try:
            raw_arguments = dict(arguments or {})
            argument_names = tuple(sorted(str(key) for key in raw_arguments))
            tool = self._find_tool(name)
            cleaned = _validate_arguments(tool, raw_arguments)

            # required_roles is an any-of. A tool listing several roles is one that
            # serves several personas, not one demanding every role at once.
            if not any(self._scope.has_role(role) for role in tool.required_roles):
                raise PermissionError(
                    f"{name}: the caller holds none of the roles this tool requires: {', '.join(tool.required_roles)}"
                )

            handler = self._handlers.get(name)
            if handler is None:
                raise ToolError(f"{name}: the contract declares a tool with no implementation")

            result = handler(cleaned)
            _validate_result(tool, result)
        except Exception as error:
            self._audit.append(
                ToolCall(
                    tool=name,
                    persona=self._scope.persona,
                    argument_names=argument_names,
                    result_rows=0,
                    outcome=f"error:{type(error).__name__}",
                )
            )
            raise
        self._audit.append(
            ToolCall(
                tool=name,
                persona=self._scope.persona,
                argument_names=argument_names,
                result_rows=len(result) if isinstance(result, list) else (0 if result is None else 1),
                outcome="success",
            )
        )
        return result

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _build_handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "customer_get_caller_context": self._customer_get_caller_context,
            "customer_lookup": self._customer_lookup,
            "customer_search": self._customer_search,
            "catalog_lookup_product": self._catalog_lookup_product,
            "catalog_search_products": self._catalog_search_products,
            "catalog_check_stock": self._catalog_check_stock,
            "orders_lookup_order": self._orders_lookup_order,
            "orders_search_orders": self._orders_search_orders,
            "orders_lookup_invoice": self._orders_lookup_invoice,
            "orders_search_invoices": self._orders_search_invoices,
            "travel_search_routes": self._travel_search_routes,
            "travel_search_fares": self._travel_search_fares,
            "travel_get_policy": self._travel_get_policy,
            "travel_simulate_booking": self._travel_simulate_booking,
            "travel_list_my_bookings": self._travel_list_my_bookings,
            "hr_get_policies": self._hr_get_policies,
            "hr_search_roster": self._hr_search_roster,
            "hr_count_roster": self._hr_count_roster,
            "hr_describe_roster": self._hr_describe_roster,
            "hr_get_management_chain": self._hr_get_management_chain,
            "hr_aggregate_roster": self._hr_aggregate_roster,
            "support_lookup_case": self._support_lookup_case,
            "support_search_cases": self._support_search_cases,
            "support_update_case": self._support_update_case,
            "operations_lookup_work_order": self._operations_lookup_work_order,
            "operations_search_work_orders": self._operations_search_work_orders,
            "operations_list_locations": self._operations_list_locations,
        }

    @staticmethod
    def _split_limit(arguments: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Separate the row cap from the business filters.

        ``limit`` is the one declared parameter that is not a column, so it is
        pulled out here rather than being special-cased inside the repository's
        filter allow-list.
        """
        filters = {key: value for key, value in arguments.items() if key != "limit"}
        return filters, int(arguments.get("limit", 25))

    # -- customer ------------------------------------------------------

    def _customer_get_caller_context(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "persona": self._scope.persona,
            "employee_id": self._scope.employee_id,
            "roles": sorted(self._scope.roles),
            "regions": self._scope.region_list(),
        }

    def _customer_lookup(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return self._repository.get_row("customers", arguments["customer_id"])

    def _customer_search(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("customers", filters=filters, limit=limit)

    # -- catalog -------------------------------------------------------

    def _catalog_lookup_product(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return self._repository.get_row("products", arguments["product_id"])

    def _catalog_search_products(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("products", filters=filters, limit=limit)

    def _catalog_check_stock(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("stock_levels", filters=filters, limit=limit)

    # -- orders --------------------------------------------------------

    def _orders_lookup_order(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        order = self._repository.get_row("orders", arguments["order_id"])
        if order is None:
            return None
        if arguments.get("include_lines", True):
            # The lines are fetched through the repository too, so a caller who
            # somehow reached an out-of-scope header would still get no lines.
            order = dict(order)
            order["lines"] = self._repository.list_rows(
                "order_lines", filters={"order_id": arguments["order_id"]}, limit=200
            )
        return order

    def _orders_search_orders(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("orders", filters=filters, limit=limit)

    def _orders_lookup_invoice(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return self._repository.get_row("invoices", arguments["invoice_id"])

    def _orders_search_invoices(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("invoices", filters=filters, limit=limit)

    # -- travel --------------------------------------------------------

    def _travel_search_routes(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("travel_routes", filters=filters, limit=limit)

    def _travel_search_fares(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("travel_fares", filters=filters, limit=limit)

    def _applicable_travel_policy(self) -> dict[str, Any]:
        employee = self._repository.get_row("employees", self._scope.employee_id)
        if employee is None:
            raise ScopeViolationError("the resolved traveller is not visible in the request scope")
        policies = self._repository.list_rows(
            "travel_policies",
            filters={"region": employee["region"]},
            limit=10,
        )
        wants_exception = employee["department"] in TRAVEL_EXCEPTION_DEPARTMENTS
        suffix = "-02" if wants_exception else "-01"
        policy = next((candidate for candidate in policies if str(candidate["policy_id"]).endswith(suffix)), None)
        if policy is None:
            raise ScopeViolationError("no travel policy applies to this scope")
        return policy

    def _travel_get_policy(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        return [self._applicable_travel_policy()]

    def _travel_simulate_booking(self, arguments: dict[str, Any]) -> dict[str, Any]:
        fares = self._repository.list_rows("travel_fares", filters={"route_id": arguments["route_id"]}, limit=200)
        fare = next((row for row in fares if row["fare_id"] == arguments["fare_id"]), None)
        if fare is None:
            raise ToolError(
                f"travel_simulate_booking: fare {arguments['fare_id']!r} is not published for route "
                f"{arguments['route_id']!r}"
            )

        policy = self._applicable_travel_policy()

        reasons: list[str] = []
        fare_ranking = {"economy": 1, "economy_plus": 2, "business": 3, "first": 4}
        requested_rank = fare_ranking.get(str(fare["fare_class"]).lower(), 99)
        permitted_rank = fare_ranking.get(str(policy["max_fare_class"]).lower(), 0)
        if requested_rank > permitted_rank:
            reasons.append(
                f"fare class {fare['fare_class']} exceeds the permitted maximum of {policy['max_fare_class']}"
            )

        try:
            depart_on = dt.date.fromisoformat(arguments["depart_on"])
            return_on = dt.date.fromisoformat(arguments["return_on"]) if arguments.get("return_on") else None
        except ValueError as error:
            raise ToolError("travel_simulate_booking: dates must be valid ISO calendar dates") from error
        if depart_on < self._as_of:
            raise ToolError("travel_simulate_booking: departure cannot precede the configured planning date")
        if return_on is not None and return_on < depart_on:
            raise ToolError("travel_simulate_booking: return date cannot precede departure")

        price = float(fare["base_price"]) * (2 if return_on is not None else 1)
        threshold = float(policy["requires_approval_over"])
        if price > threshold:
            reasons.append(f"price {price:.2f} {fare['currency']} is over the approval threshold of {threshold:.2f}")

        lead_days = (depart_on - self._as_of).days
        minimum_notice = int(policy["advance_purchase_days"])
        if lead_days < minimum_notice:
            reasons.append(
                f"departure is {lead_days} day(s) away; policy requires {minimum_notice} days advance purchase"
            )

        if int(fare["seats_available"]) <= 0:
            reasons.append("no seats remain at this fare")

        if any("exceeds the permitted maximum" in reason or "no seats remain" in reason for reason in reasons):
            decision = "refused"
        elif reasons:
            decision = "requires_approval"
        else:
            decision = "allowed"

        return {
            "decision": decision,
            "route_id": arguments["route_id"],
            "fare_id": arguments["fare_id"],
            "depart_on": depart_on.isoformat(),
            "return_on": return_on.isoformat() if return_on is not None else None,
            "policy_id": policy["policy_id"],
            "price": price,
            "currency": fare["currency"],
            "reasons": reasons,
            # Named so nobody mistakes this for an itinerary. Nothing is written.
            "simulated": True,
            "projected_seats_remaining": max(0, int(fare["seats_available"]) - 1),
        }

    def _travel_list_my_bookings(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        requested_employee = filters.get("traveller_employee_id")
        if not self._scope.has_role("travel.coordinate"):
            if requested_employee not in (None, self._scope.employee_id):
                raise PermissionError("travel_list_my_bookings: ordinary readers may request only their own bookings")
            filters["traveller_employee_id"] = self._scope.employee_id
        return self._repository.list_rows("travel_bookings", filters=filters, limit=limit)

    # -- hr ------------------------------------------------------------

    def _hr_get_policies(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("hr_policies", filters=filters, limit=limit)

    def _hr_search_roster(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("employees", filters=filters, limit=limit)

    def _hr_count_roster(self, arguments: dict[str, Any]) -> dict[str, Any]:
        filters, _ = self._split_limit(arguments)
        return {"count": self._repository.count_rows("employees", filters=filters)}

    def _hr_describe_roster(self, _: dict[str, Any]) -> dict[str, Any]:
        return self._repository.describe("employees")

    def _hr_get_management_chain(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        return self._repository.hierarchy(arguments["employee_id"])

    def _hr_aggregate_roster(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters = {key: value for key, value in arguments.items() if key != "group_by"}
        return self._repository.aggregate("employees", group_by=arguments["group_by"], filters=filters)

    # -- support -------------------------------------------------------

    def _support_lookup_case(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        case = self._repository.get_row("support_cases", arguments["case_id"])
        if case is None:
            return None
        if arguments.get("include_notes", False):
            notes = self._repository.list_rows(
                "support_case_notes", filters={"case_id": arguments["case_id"]}, limit=200
            )
            if not self._scope.has_role("support.write"):
                # An internal note is where a colleague writes what they would
                # not say to the customer. Read-only callers get the customer-
                # facing record.
                notes = [note for note in notes if note.get("visibility") != "internal"]
            case = dict(case)
            case["notes"] = notes
        return case

    def _support_search_cases(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("support_cases", filters=filters, limit=limit)

    def _support_update_case(self, arguments: dict[str, Any]) -> dict[str, Any]:
        changes = {key: value for key, value in arguments.items() if key != "case_id"}
        return self._repository.update_support_case(arguments["case_id"], changes)

    # -- operations ----------------------------------------------------

    def _operations_lookup_work_order(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return self._repository.get_row("work_orders", arguments["work_order_id"])

    def _operations_search_work_orders(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("work_orders", filters=filters, limit=limit)

    def _operations_list_locations(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        filters, limit = self._split_limit(arguments)
        return self._repository.list_rows("locations", filters=filters, limit=limit)