from __future__ import annotations

import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agents" / "travel"
sys.path.insert(0, str(AGENT_ROOT / "src"))

from contoso_travel_agent.traffic import (  # noqa: E402
    TrafficConfigurationError,
    TrafficPlan,
    _flush_telemetry,
    run_from_environment,
)


@pytest.fixture(scope="module")
def plan():
    return TrafficPlan.load(AGENT_ROOT / "traffic.yaml")


def test_traffic_is_disabled_by_default(plan):
    assert plan.scenario_for(datetime(2026, 8, 24, 15, 0, tzinfo=UTC), enabled=False) is None
    assert (
        plan.scenario_for(
            datetime(2026, 8, 24, 2, 2, tzinfo=UTC),
            enabled=True,
            force=True,
        )
        is not None
    )


def test_scheduled_prompts_use_business_language_without_expiring_dates(plan):
    for scenario in plan.scenarios:
        assert not any(token in scenario.prompt for token in ("LOC-", "ROUTE-", "2026-", "travel_search_"))
        assert "synthetic" not in scenario.prompt.casefold()


def test_weekday_business_hours_reserve_half_the_estate_slots_for_travel(plan):
    scenarios = [
        plan.scenario_for(datetime(2026, 8, 24, 14, minute, tzinfo=UTC), enabled=True)
        for minute in (0, 15, 30, 45)
    ]
    assert [scenario is not None for scenario in scenarios] == [True, False, True, False]
    assert plan.maximum_per_hour == 4
    assert plan.travel_maximum_per_hour == 2
    assert plan.scenario_for(datetime(2026, 8, 24, 14, 1, tzinfo=UTC), enabled=True) is None


def test_forced_acceptance_runs_bypass_the_schedule(plan):
    allowed = [
        plan.scenario_for(datetime(2026, 8, 24, 2, minute, tzinfo=UTC), enabled=True, force=True)
        for minute in (2, 17, 32, 47)
    ]
    assert [scenario is not None for scenario in allowed] == [True, True, True, True]


def test_weekends_and_holidays_are_quiet(plan):
    # Saturday and US Christmas use only the two explicitly declared local slots.
    saturday = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
    christmas = datetime(2026, 12, 25, 16, 0, tzinfo=UTC)
    assert plan.scenario_for(saturday, enabled=True) is not None
    assert plan.scenario_for(saturday.replace(minute=15), enabled=True) is None
    assert plan.scenario_for(christmas, enabled=True) is not None


def test_complete_federal_calendar_includes_observed_and_relative_holidays(plan):
    assert len(plan.federal_holidays) == 11
    # Independence Day 2026 falls on Saturday, so Friday is the observed holiday.
    observed_independence_day = datetime(2026, 7, 3, 15, 0, tzinfo=UTC)
    assert plan.scenario_for(observed_independence_day, enabled=True) is not None
    assert (
        plan.scenario_for(observed_independence_day.replace(minute=15), enabled=True)
        is None
    )
    # Martin Luther King Jr. Day is the third Monday in January.
    mlk_day = datetime(2026, 1, 19, 16, 0, tzinfo=UTC)
    assert plan.scenario_for(mlk_day, enabled=True) is not None
    assert plan.scenario_for(mlk_day.replace(minute=15), enabled=True) is None
    # New Year's Day 2022 fell on Saturday and was observed in the prior year.
    observed_new_year = datetime(2021, 12, 31, 16, 0, tzinfo=UTC)
    assert plan.scenario_for(observed_new_year, enabled=True) is not None
    assert plan.scenario_for(observed_new_year.replace(minute=15), enabled=True) is None


def test_federal_calendar_fails_closed_when_incomplete(tmp_path):
    source = (AGENT_ROOT / "traffic.yaml").read_text(encoding="utf-8")
    incomplete = source.replace(
        "  - {id: christmas-day, month: 12, day: 25, observe_weekend: true}\n",
        "",
    )
    path = tmp_path / "traffic.yaml"
    path.write_text(incomplete, encoding="utf-8")
    with pytest.raises(TrafficConfigurationError, match="every US federal holiday"):
        TrafficPlan.load(path)


def test_schedule_requires_aware_time(plan):
    with pytest.raises(TrafficConfigurationError, match="aware"):
        plan.scenario_for(datetime(2026, 8, 24, 9, 0), enabled=True)


def test_slot_key_is_one_per_quarter_hour(plan):
    assert plan.slot_key(datetime(2026, 8, 24, 14, 14, tzinfo=UTC)).endswith("/09/0.json")
    assert plan.slot_key(datetime(2026, 8, 24, 14, 15, tzinfo=UTC)).endswith("/09/1.json")


def test_traffic_flushes_completed_conversation_span():
    class Provider:
        def __init__(self):
            self.timeout = None

        def force_flush(self, *, timeout_millis):
            self.timeout = timeout_millis
            return True

    provider = Provider()
    _flush_telemetry(provider)
    assert provider.timeout == 30_000


@pytest.mark.parametrize("tool_count", [0, 2])
def test_scheduled_traffic_counts_actual_server_executed_calls(monkeypatch, tool_count):
    import azure.ai.projects
    import azure.ai.projects.telemetry
    import azure.identity
    import azure.monitor.opentelemetry
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    requests = []
    calls = []
    for index in range(tool_count):
        calls.extend([
            SimpleNamespace(type="openapi_call", name="travel_get_policy",
                            arguments="{}", call_id=f"call-{index}", status="completed"),
            SimpleNamespace(type="openapi_call_output", call_id=f"call-{index}", status="completed"),
        ])

    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(output=calls, output_text="A verified answer.")

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    project = SimpleNamespace(
        telemetry=SimpleNamespace(get_application_insights_connection_string=lambda: "synthetic"),
        get_openai_client=lambda: nullcontext(client),
    )
    monkeypatch.setattr(azure.ai.projects, "AIProjectClient", lambda **_: project)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: None)
    monkeypatch.setattr(azure.monitor.opentelemetry, "configure_azure_monitor", lambda **_: None)
    monkeypatch.setattr(
        azure.ai.projects.telemetry, "AIProjectInstrumentor",
        lambda: SimpleNamespace(instrument=lambda: None),
    )
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(trace, "get_tracer", provider.get_tracer)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://travel.example.invalid")
    monkeypatch.setenv("TRAVEL_AGENT_VERSION", "8")
    monkeypatch.setenv("TRAFFIC_ENABLED", "true")
    monkeypatch.setenv("TRAFFIC_FORCE_RUN", "true")
    monkeypatch.setenv("CONTOSO_REPO_ROOT", str(REPO_ROOT))
    result = run_from_environment(instant=datetime(2026, 8, 24, 15, 0, tzinfo=UTC))
    assert result["tool_names"] == ["travel_get_policy"] * tool_count
    assert result["status"] == "completed"
    assert len(requests) == 1
    assert requests[0]["extra_body"]["agent_reference"]["version"] == "8"
    span, = exporter.get_finished_spans()
    assert span.attributes["contoso.tool.count"] == tool_count
    provider.shutdown()


@pytest.mark.parametrize("provider", [object(), type("Provider", (), {"force_flush": lambda self, **_: False})()])
def test_traffic_fails_closed_when_telemetry_does_not_flush(provider):
    with pytest.raises(TrafficConfigurationError, match="did not flush"):
        _flush_telemetry(provider)


def test_container_and_bicep_pin_security_contracts():
    dockerfile = (AGENT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    bicep = (REPO_ROOT / "infra" / "travel.bicep").read_text(encoding="utf-8")
    assert "FROM python:3.13.15-slim-bookworm@sha256:" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "image: '${imageRepository}@sha256:${imageDigest}'" in bicep
    assert "param trafficCronExpression string = '0,30 * * * *'" in bicep
    assert "cronExpression: trafficCronExpression" in bicep
    assert "parallelism: 1" in bicep
    assert "TRAFFIC_ENABLED" in bicep
    assert "OTEL_SERVICE_NAME" in bicep
    assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" in bicep
    assert "AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED" in bicep
    assert "TRAFFIC_LEDGER_URL" not in bicep
    assert "Storage Blob Data Contributor" not in bicep


def test_live_workflow_detects_optional_boundary_and_uploads_only_sanitized_evidence():
    workflow = (REPO_ROOT / ".github" / "workflows" / "travel.yml").read_text(
        encoding="utf-8"
    )
    assert "--enable-module optional-control-plane" in workflow
    assert "path: internal/travel" not in workflow
    assert "path: reports/travel-candidate.json" in workflow
    assert "foundry scan reports/travel-candidate.json" in workflow
    assert "--tag contoso-travel:ci" in workflow
    assert workflow.count("docker/build-push-action@") == 1
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow


def test_travel_image_is_rescanned_after_merge_and_on_schedule():
    workflow = (REPO_ROOT / ".github" / "workflows" / "travel.yml").read_text(
        encoding="utf-8"
    )
    assert 'branches: ["main"]' in workflow
    assert 'cron: "43 6 * * 1"' in workflow
    assert "github.event_name == 'workflow_dispatch' && inputs.deploy" in workflow
