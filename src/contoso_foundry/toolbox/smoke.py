"""A thin smoke client: proof that the contracts are callable, not just declarable.

It runs the same prompt as two different people and prints both answers. That is
the whole point of the exercise - a scoping design that is only asserted in tests
is hard to believe, whereas two side-by-side result sets with no overlap are
hard to argue with.

Nothing here talks to a network, an agent framework or Azure. It opens the
generated SQLite file, resolves three personas, and calls tools.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contoso_foundry.toolbox.identity import UnknownPrincipalError, principal_from_fixture
from contoso_foundry.toolbox.tools import Toolbox

# The prompt both people send. Identical text, identical arguments; the only
# difference between the two calls is who is making them.
SHARED_ROSTER_QUESTION = {"tool": "hr_search_roster", "arguments": {"limit": 200}}

EMEA_PRINCIPAL = ("OID-EMEA-HRBP-01", "TID-CONTOSO-01")
APAC_PRINCIPAL = ("OID-APAC-HRBP-01", "TID-CONTOSO-01")
TRAVEL_PRINCIPAL = ("OID-EMEA-TRAVEL-01", "TID-CONTOSO-01")
UNKNOWN_PRINCIPAL = ("OID-NOT-A-REAL-PRINCIPAL", "TID-CONTOSO-01")


@dataclass
class SmokeResult:
    """What the smoke run observed, so a caller can assert on it as well as read it."""

    lines: list[str]
    emea_employee_ids: set[str]
    apac_employee_ids: set[str]
    unknown_principal_failed: bool
    tools_exercised: set[str]

    @property
    def overlap(self) -> set[str]:
        return self.emea_employee_ids & self.apac_employee_ids

    def ok(self) -> bool:
        return (
            bool(self.emea_employee_ids)
            and bool(self.apac_employee_ids)
            and not self.overlap
            and self.unknown_principal_failed
        )


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _summarise(rows: Iterable[dict[str, Any]], key: str, limit: int = 3) -> str:
    values = [str(row.get(key, "")) for row in rows]
    head = ", ".join(values[:limit])
    if len(values) > limit:
        head += f", ... (+{len(values) - limit} more)"
    return head or "(none)"


def run_smoke(database: Path, contracts_dir: Path, *, minimum_cohort: int = 5) -> SmokeResult:
    """Exercise customer context, travel and scoped HR against canonical ids."""
    lines: list[str] = []
    tools_exercised: set[str] = set()
    connection = _connect(database)

    def call(toolbox: Toolbox, name: str, arguments: dict[str, Any] | None = None) -> Any:
        tools_exercised.add(name)
        return toolbox.call(name, arguments or {})

    try:
        # --- the two people, and the identical question ---------------
        rosters: dict[str, list[dict[str, Any]]] = {}
        for label, principal in (("EMEA", EMEA_PRINCIPAL), ("APAC", APAC_PRINCIPAL)):
            toolbox = Toolbox(
                connection,
                principal_from_fixture(*principal),
                contracts_dir=contracts_dir,
                minimum_cohort=minimum_cohort,
            )
            context = call(toolbox, "customer_get_caller_context")
            lines.append(
                f"{label}: resolved to {context['persona']} ({context['employee_id']}) "
                f"over region(s) {', '.join(context['regions'])}"
            )

            roster = call(toolbox, SHARED_ROSTER_QUESTION["tool"], dict(SHARED_ROSTER_QUESTION["arguments"]))
            rosters[label] = roster
            lines.append(
                f"{label}: hr_search_roster returned {len(roster)} row(s): {_summarise(roster, 'employee_id')}"
            )

            counted = call(toolbox, "hr_count_roster", {})
            lines.append(f"{label}: hr_count_roster returned {counted['count']}")

            grouped = call(toolbox, "hr_aggregate_roster", {"group_by": "department"})
            covered = sum(int(group["count"]) for group in grouped)
            # The gap between the roster count and the sum of the surviving
            # groups is the suppression, shown without naming what was withheld.
            lines.append(
                f"{label}: hr_aggregate_roster kept {len(grouped)} group(s) covering {covered} "
                f"of {counted['count']} people; {int(counted['count']) - covered} withheld "
                f"below the cohort floor of {minimum_cohort}"
            )

            policies = call(toolbox, "hr_get_policies", {})
            lines.append(f"{label}: hr_get_policies returned {len(policies)} policy id(s): "
                         f"{_summarise(policies, 'hr_policy_id')}")

        emea_ids = {str(row["employee_id"]) for row in rosters["EMEA"]}
        apac_ids = {str(row["employee_id"]) for row in rosters["APAC"]}
        overlap = emea_ids & apac_ids
        lines.append(
            f"overlap between the two answers: {len(overlap)} employee(s)"
            + (f" -> {sorted(overlap)}" if overlap else " (as required)")
        )

        # --- travel, on canonical ids --------------------------------
        travel = Toolbox(
            connection,
            principal_from_fixture(*TRAVEL_PRINCIPAL),
            contracts_dir=contracts_dir,
            minimum_cohort=minimum_cohort,
        )
        travel_context = call(travel, "customer_get_caller_context")
        lines.append(f"TRAVEL: resolved to {travel_context['persona']} over {', '.join(travel_context['regions'])}")

        policy = call(travel, "travel_get_policy")
        lines.append(f"TRAVEL: travel_get_policy returned {_summarise(policy, 'policy_id')}")

        routes = call(travel, "travel_search_routes", {"limit": 5})
        lines.append(f"TRAVEL: travel_search_routes returned {_summarise(routes, 'route_id')}")

        if routes:
            route_id = str(routes[0]["route_id"])
            fares = call(travel, "travel_search_fares", {"route_id": route_id, "limit": 5})
            lines.append(f"TRAVEL: travel_search_fares for {route_id} returned {_summarise(fares, 'fare_id')}")
            if fares:
                decision = call(
                    travel,
                    "travel_simulate_booking",
                    {"route_id": route_id, "fare_id": str(fares[0]["fare_id"]), "depart_on": "2026-03-04"},
                )
                lines.append(
                    f"TRAVEL: travel_simulate_booking -> {decision['decision']} "
                    f"under {decision['policy_id']} at {decision['price']:.2f} {decision['currency']}"
                    + (f"; reasons: {'; '.join(decision['reasons'])}" if decision["reasons"] else "")
                )

        customers = call(travel, "customer_search", {"limit": 3})
        lines.append(f"TRAVEL: customer_search returned {_summarise(customers, 'customer_id')}")

        # --- the negative case ---------------------------------------
        unknown_failed = False
        try:
            Toolbox(
                connection,
                principal_from_fixture(*UNKNOWN_PRINCIPAL),
                contracts_dir=contracts_dir,
                minimum_cohort=minimum_cohort,
            )
        except UnknownPrincipalError as error:
            unknown_failed = True
            lines.append(f"UNKNOWN: refused before any tool existed to call - {error}")
        else:
            lines.append("UNKNOWN: FAILED - an unrecognised principal resolved to a scope")

        return SmokeResult(
            lines=lines,
            emea_employee_ids=emea_ids,
            apac_employee_ids=apac_ids,
            unknown_principal_failed=unknown_failed,
            tools_exercised=tools_exercised,
        )
    finally:
        connection.close()