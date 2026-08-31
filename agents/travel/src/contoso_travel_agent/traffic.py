"""Bounded synthetic traffic for the Contoso Travel prompt agent."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from contoso_foundry.data import build as build_mod
from contoso_travel_agent.definition import load_agent_spec
from contoso_travel_agent.runtime import TravelAgentRuntime


class TrafficConfigurationError(RuntimeError):
    """Raised when traffic cannot prove it is bounded and synthetic."""


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    persona: str
    prompt: str


_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_REQUIRED_FEDERAL_HOLIDAYS = {
    "new-years-day",
    "martin-luther-king-jr-day",
    "washington-birthday",
    "memorial-day",
    "juneteenth",
    "independence-day",
    "labor-day",
    "columbus-day",
    "veterans-day",
    "thanksgiving-day",
    "christmas-day",
}


@dataclass(frozen=True)
class FederalHolidayRule:
    holiday_id: str
    month: int
    day: int | None
    weekday: int | None
    occurrence: int | str | None
    observe_weekend: bool

    @classmethod
    def load(cls, document: dict[str, Any]) -> FederalHolidayRule:
        holiday_id = str(document.get("id", "")).strip()
        month = int(document.get("month", 0))
        day = document.get("day")
        weekday_name = document.get("weekday")
        occurrence = document.get("occurrence")
        observed = document.get("observe_weekend", False)
        if not holiday_id or not 1 <= month <= 12 or not isinstance(observed, bool):
            raise TrafficConfigurationError("federal holiday rules require an id, month, and boolean observation")
        fixed = day is not None and weekday_name is None and occurrence is None
        relative = day is None and weekday_name in _WEEKDAYS and (
            occurrence == "last" or isinstance(occurrence, int) and 1 <= occurrence <= 5
        )
        if not fixed and not relative:
            raise TrafficConfigurationError(f"{holiday_id}: invalid federal holiday rule")
        if fixed:
            day = int(day)
            try:
                date(2024, month, day)
            except ValueError as error:
                raise TrafficConfigurationError(f"{holiday_id}: invalid fixed date") from error
        return cls(
            holiday_id=holiday_id,
            month=month,
            day=day,
            weekday=_WEEKDAYS.get(str(weekday_name)),
            occurrence=occurrence,
            observe_weekend=observed,
        )

    def dates(self, year: int) -> frozenset[date]:
        if self.day is not None:
            actual = date(year, self.month, self.day)
        else:
            assert self.weekday is not None
            if self.occurrence == "last":
                last = date(year, self.month, monthrange(year, self.month)[1])
                actual = last - timedelta(days=(last.weekday() - self.weekday) % 7)
            else:
                first = date(year, self.month, 1)
                offset = (self.weekday - first.weekday()) % 7
                actual = first + timedelta(days=offset + 7 * (int(self.occurrence) - 1))
        dates = {actual}
        if self.observe_weekend and actual.weekday() == 5:
            dates.add(actual - timedelta(days=1))
        elif self.observe_weekend and actual.weekday() == 6:
            dates.add(actual + timedelta(days=1))
        return frozenset(dates)


@dataclass(frozen=True)
class TrafficPlan:
    timezone: ZoneInfo
    maximum_per_hour: int
    travel_maximum_per_hour: int
    travel_quarter_minutes: frozenset[int]
    business_start: int
    business_end: int
    weekend_slots: frozenset[str]
    holiday_slots: frozenset[str]
    federal_holidays: tuple[FederalHolidayRule, ...]
    scenarios: tuple[Scenario, ...]

    @classmethod
    def load(cls, path: Path) -> TrafficPlan:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise TrafficConfigurationError("traffic.yaml must contain a mapping")
        maximum = int(document["maximum_conversations_per_hour"])
        if maximum != 4:
            raise TrafficConfigurationError("estate-wide traffic must remain capped at four conversations per hour")
        travel_maximum = int(document["travel_maximum_conversations_per_hour"])
        travel_quarter_minutes = frozenset(int(value) for value in document["travel_quarter_minutes"])
        if (
            travel_maximum != 2
            or len(travel_quarter_minutes) != travel_maximum
            or not travel_quarter_minutes <= {0, 15, 30, 45}
        ):
            raise TrafficConfigurationError("Travel must use exactly two quarter-hour slots per hour")
        scenarios = tuple(
            Scenario(
                scenario_id=str(item["id"]),
                persona=str(item["persona"]),
                prompt=str(item["prompt"]).strip(),
            )
            for item in document["scenarios"]
        )
        persona_ids = {str(item["id"]) for item in document["personas"]}
        if not scenarios or any(scenario.persona not in persona_ids for scenario in scenarios):
            raise TrafficConfigurationError("every scenario must reference a checked-in persona")
        federal_holidays = tuple(
            FederalHolidayRule.load(item) for item in document.get("federal_holidays", [])
        )
        holiday_ids = {rule.holiday_id for rule in federal_holidays}
        if holiday_ids != _REQUIRED_FEDERAL_HOLIDAYS or len(federal_holidays) != len(holiday_ids):
            raise TrafficConfigurationError("traffic.yaml must declare every US federal holiday exactly once")
        return cls(
            timezone=ZoneInfo(str(document["timezone"])),
            maximum_per_hour=maximum,
            travel_maximum_per_hour=travel_maximum,
            travel_quarter_minutes=travel_quarter_minutes,
            business_start=int(document["weekday_business_hours"]["start"]),
            business_end=int(document["weekday_business_hours"]["end"]),
            weekend_slots=frozenset(str(slot) for slot in document["quiet_hours"]["weekend_slots"]),
            holiday_slots=frozenset(str(slot) for slot in document["quiet_hours"]["holiday_slots"]),
            federal_holidays=federal_holidays,
            scenarios=scenarios,
        )

    def _is_us_holiday(self, local: datetime) -> bool:
        local_day = local.date()
        return any(
            local_day in rule.dates(year)
            for rule in self.federal_holidays
            for year in (local.year - 1, local.year, local.year + 1)
        )

    def scenario_for(self, instant: datetime, *, enabled: bool, force: bool = False) -> Scenario | None:
        if not enabled:
            return None
        if instant.tzinfo is None:
            raise TrafficConfigurationError("traffic scheduling requires an aware timestamp")
        local = instant.astimezone(self.timezone)
        slot = local.strftime("%H:%M")
        if force:
            allowed = True
        elif self._is_us_holiday(local):
            allowed = slot in self.holiday_slots
        elif local.weekday() >= 5:
            allowed = slot in self.weekend_slots
        else:
            allowed = (
                self.business_start <= local.hour < self.business_end
                and local.minute in self.travel_quarter_minutes
            )
        if not allowed:
            return None
        index = int(hashlib.sha256(local.isoformat(timespec="minutes").encode()).hexdigest(), 16) % len(self.scenarios)
        return self.scenarios[index]

    def slot_key(self, instant: datetime) -> str:
        local = instant.astimezone(self.timezone)
        quarter = local.minute // 15
        return f"{local:%Y/%m/%d/%H}/{quarter}.json"


def _build_database(repo_root: Path) -> sqlite3.Connection:
    out_dir = Path(tempfile.mkdtemp(prefix="contoso-travel-"))
    result = build_mod.build(
        config_path=repo_root / "config" / "data-spine.yaml",
        seed_dir=repo_root / "data" / "seed",
        out_dir=out_dir,
        fixtures_dir=repo_root / "data" / "fixtures",
    )
    return sqlite3.connect(result.root / "contoso.db")


def _flush_telemetry(provider: Any) -> None:
    force_flush = getattr(provider, "force_flush", None)
    if force_flush is None or not force_flush(timeout_millis=30_000):
        raise TrafficConfigurationError("synthetic conversation telemetry did not flush")


def run_from_environment(*, instant: datetime | None = None) -> dict[str, Any]:
    required = ("FOUNDRY_PROJECT_ENDPOINT", "TRAVEL_AGENT_VERSION")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise TrafficConfigurationError(f"missing required environment variable(s): {', '.join(missing)}")
    enabled = os.environ.get("TRAFFIC_ENABLED", "false").lower() == "true"
    force = os.environ.get("TRAFFIC_FORCE_RUN", "false").lower() == "true"

    repo_root = Path(os.environ.get("CONTOSO_REPO_ROOT", "/app"))
    plan = TrafficPlan.load(repo_root / "agents" / "travel" / "traffic.yaml")
    now = instant or datetime.now(UTC)
    scenario = plan.scenario_for(now, enabled=enabled, force=force)
    if scenario is None:
        return {"status": "skipped", "reason": "outside active synthetic traffic window"}

    os.environ.setdefault("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING", "true")
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.telemetry import AIProjectInstrumentor
    from azure.identity import DefaultAzureCredential
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry import trace

    credential = DefaultAzureCredential()
    slot_key = plan.slot_key(now)

    project = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=credential)
    connection_string = project.telemetry.get_application_insights_connection_string()
    configure_azure_monitor(connection_string=connection_string)
    AIProjectInstrumentor().instrument()

    tracer = trace.get_tracer("contoso.travel.synthetic")
    spec = load_agent_spec(repo_root / "agents" / "travel" / "agent.yaml")
    with (
        tracer.start_as_current_span("contoso.travel.synthetic_conversation") as span,
        project.get_openai_client() as openai_client,
        _build_database(repo_root) as connection,
    ):
        span.set_attribute("contoso.synthetic", True)
        span.set_attribute("contoso.traffic.slot", slot_key)
        span.set_attribute("contoso.scenario.id", scenario.scenario_id)
        span.set_attribute("gen_ai.agent.name", spec.name)
        span.set_attribute("gen_ai.agent.version", os.environ["TRAVEL_AGENT_VERSION"])
        runtime = TravelAgentRuntime(
            openai_client,
            spec,
            connection,
            contracts_dir=repo_root / "config" / "toolbox",
            tracer=tracer,
        )
        answer = runtime.run_turn(
            scenario.prompt,
            agent_version=os.environ["TRAVEL_AGENT_VERSION"],
        )
        span.set_attribute("contoso.tool.count", len(runtime.audit))
    _flush_telemetry(trace.get_tracer_provider())
    return {
        "status": "completed",
        "scenario_id": scenario.scenario_id,
        "slot": slot_key,
        "tool_names": [call.tool for call in runtime.audit],
        "answer_length": len(answer),
        "synthetic": True,
    }


def main() -> int:
    print(json.dumps(run_from_environment(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
