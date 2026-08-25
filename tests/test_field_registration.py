"""The registration gate must fail closed on missing or mismatched live span IDs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from contoso_foundry.field import register
from contoso_foundry.field.register import RegistrationError, verify_live_telemetry


def payload(
    total: int,
    with_id: int,
    agent_ids: list[str] | str,
    *,
    operation_ids: list[str] | str | None = None,
    correlation_id: str = "smoke-1",
    revision: str = "contoso-field--rev-1",
) -> dict:
    return {
        "tables": [
            {
                "columns": [
                    {"name": "total_spans"},
                    {"name": "spans_with_agent_id"},
                    {"name": "agent_ids"},
                    {"name": "operation_ids"},
                    {"name": "smoke_correlation_id"},
                    {"name": "container_app_revision"},
                ],
                "rows": [
                    [
                        total,
                        with_id,
                        agent_ids,
                        ["operation-1"] if operation_ids is None else operation_ids,
                        correlation_id,
                        revision,
                    ]
                ],
            }
        ]
    }


def verify(summary: dict):
    return verify_live_telemetry(
        summary,
        expected_agent_id="contoso-field-v1",
        smoke_correlation_id="smoke-1",
        container_app_revision="contoso-field--rev-1",
    )


def test_registration_accepts_one_matching_live_agent_id() -> None:
    evidence = verify(payload(5, 5, ["contoso-field-v1"]))
    assert evidence.total_spans == 5
    assert evidence.agent_ids == ("contoso-field-v1",)
    assert evidence.operation_ids == ("operation-1",)


def test_registration_parses_application_insights_dynamic_values() -> None:
    evidence = verify(
        payload(
            5,
            5,
            '["contoso-field-v1"]',
            operation_ids='["operation-1"]',
        )
    )
    assert evidence.agent_ids == ("contoso-field-v1",)


@pytest.mark.parametrize(
    ("summary", "message"),
    [
        (payload(0, 0, []), "no live Pydantic AI spans"),
        (payload(5, 4, ["contoso-field-v1"]), "only 4 of 5"),
        (payload(5, 5, ["wrong-id"]), "do not match"),
        (payload(5, 5, ["contoso-field-v1"], operation_ids=[]), "exactly one correlated"),
        (payload(5, 5, ["contoso-field-v1"], correlation_id="stale"), "correlation ID"),
        (payload(5, 5, ["contoso-field-v1"], revision="stale"), "Container App revision"),
    ],
)
def test_registration_refuses_unproven_identity(summary: dict, message: str) -> None:
    with pytest.raises(RegistrationError, match=message):
        verify(summary)


def test_registration_query_is_scoped_to_smoke_start_trace_and_revision(monkeypatch) -> None:
    captured: list[str] = []
    expected = payload(5, 5, ["contoso-field-v1"])

    def fake_run(arguments: list[str]):
        captured.extend(arguments)
        return expected

    monkeypatch.setattr(register.azure_cli, "run", fake_run)
    result = register.query_live_telemetry(
        resource_group="rg-contoso-agents",
        application_insights_name="contoso-agents-insights",
        agent_name="contoso-field",
        smoke_correlation_id="smoke-1",
        smoke_started_at=datetime(2026, 8, 25, 2, 30, tzinfo=UTC),
        container_app_revision="contoso-field--rev-1",
    )

    query = captured[captured.index("--analytics-query") + 1]
    assert result == expected
    assert "ago(" not in query
    assert "timestamp >= datetime(2026-08-25T02:30:00Z)" in query
    assert 'smoke_correlation_id == "smoke-1"' in query
    assert 'smoke_revision == "contoso-field--rev-1"' in query
    assert "operation_Id in (smoke_operations)" in query


def test_registration_query_rejects_untrusted_dimensions() -> None:
    with pytest.raises(RegistrationError, match="unsupported characters"):
        register.query_live_telemetry(
            resource_group="rg-contoso-agents",
            application_insights_name="contoso-agents-insights",
            agent_name="contoso-field",
            smoke_correlation_id='smoke" | take 1',
            smoke_started_at=datetime.now(UTC),
            container_app_revision="contoso-field--rev-1",
        )
