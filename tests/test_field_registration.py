"""The registration gate must fail closed on missing or mismatched live span IDs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from contoso_foundry.field import register
from contoso_foundry.field.register import (
    RegistrationError,
    validate_registration_target,
    verify_live_telemetry,
    verify_registration_readback,
)

PROJECT_ENDPOINT = (
    "https://" + "contoso-agents-foundry" + ".services.ai.azure.com/api/projects/platform"
)


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
                    {"name": "smoke_correlation_ids"},
                    {"name": "container_app_revisions"},
                ],
                "rows": [
                    [
                        total,
                        with_id,
                        agent_ids,
                        ["operation-1"] if operation_ids is None else operation_ids,
                        [correlation_id],
                        [revision],
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
    assert "set_has_element(smoke_operations, operation_Id)" in query
    assert "smoke_correlation_ids = toscalar(smoke_evidence" in query
    assert "container_app_revisions = toscalar(smoke_evidence" in query
    assert '| extend smoke_correlation_id = "smoke-1"' not in query


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


def test_registration_requires_explicit_write_authorization(monkeypatch) -> None:
    monkeypatch.setattr(
        register,
        "DefaultAzureCredential",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("credential must not be created")),
    )
    with pytest.raises(RegistrationError, match="explicit write authorization"):
        register.register_external_agent(
            project_endpoint=PROJECT_ENDPOINT,
            agent_name="contoso-field",
            otel_agent_id="contoso-field-v1",
            client_id=None,
        )


def test_registration_verifies_sdk_readback(monkeypatch) -> None:
    class Agents:
        def create_version(self, **kwargs):
            assert kwargs["agent_name"] == "contoso-field"
            assert kwargs["definition"].otel_agent_id == "contoso-field-v1"
            return SimpleNamespace(
                agent_guid="agent-id",
                id="version-id",
                name="contoso-field",
                version="1",
            )

        def get_version(self, *, agent_name, agent_version):
            assert agent_name == "contoso-field"
            assert agent_version == "1"
            return SimpleNamespace(
                name=agent_name,
                agent_guid="agent-id",
                id="version-id",
                version="1",
                definition=SimpleNamespace(otel_agent_id="contoso-field-v1"),
            )

    monkeypatch.setattr(register, "DefaultAzureCredential", lambda **_kwargs: object())
    monkeypatch.setattr(
        register,
        "AIProjectClient",
        lambda **kwargs: (
            SimpleNamespace(agents=Agents())
            if kwargs["allow_preview"] is True
            else (_ for _ in ()).throw(AssertionError("preview must be enabled"))
        ),
    )

    readback = register.register_external_agent(
        project_endpoint=PROJECT_ENDPOINT,
        agent_name="contoso-field",
        otel_agent_id="contoso-field-v1",
        client_id=None,
        allow_write=True,
    )

    assert readback.name == "contoso-field"
    assert readback.agent_id == "agent-id"
    assert readback.version == "1"
    assert readback.version_id == "version-id"
    assert readback.otel_agent_id == "contoso-field-v1"


@pytest.mark.parametrize(
    ("registered", "message"),
    [
        (
            SimpleNamespace(
                name="wrong",
                agent_guid="agent-id",
                id="version-id",
                version="1",
                definition=SimpleNamespace(otel_agent_id="contoso-field-v1"),
            ),
            "readback name",
        ),
        (
            SimpleNamespace(
                name="contoso-field",
                agent_guid="agent-id",
                id="version-id",
                version="1",
                definition=SimpleNamespace(otel_agent_id="wrong"),
            ),
            "OpenTelemetry ID",
        ),
        (
            SimpleNamespace(
                agent_guid="agent-id",
                id="version-id",
                name="contoso-field",
                version="2",
                definition=SimpleNamespace(otel_agent_id="contoso-field-v1"),
            ),
            "readback version",
        ),
        (
            SimpleNamespace(
                agent_guid="wrong-agent-id",
                id="version-id",
                name="contoso-field",
                version="1",
                definition=SimpleNamespace(otel_agent_id="contoso-field-v1"),
            ),
            "readback agent ID",
        ),
        (
            SimpleNamespace(
                agent_guid="agent-id",
                id="wrong-version-id",
                name="contoso-field",
                version="1",
                definition=SimpleNamespace(otel_agent_id="contoso-field-v1"),
            ),
            "readback version ID",
        ),
        (
            SimpleNamespace(
                agent_guid="",
                id="",
                name="contoso-field",
                version="",
                definition=SimpleNamespace(otel_agent_id="contoso-field-v1"),
            ),
            "omitted its agent or version identity",
        ),
    ],
)
def test_registration_rejects_invalid_sdk_readback(registered, message) -> None:
    with pytest.raises(RegistrationError, match=message):
        verify_registration_readback(
            registered,
            expected_name="contoso-field",
            expected_otel_agent_id="contoso-field-v1",
            expected_agent_id="agent-id",
            expected_version="1",
            expected_version_id="version-id",
        )


def test_registration_target_is_derived_from_the_boundary(repo_root) -> None:
    plan = register.boundary.load_plan(repo_root / "config" / "boundary.yaml")
    validate_registration_target(
        plan,
        project_endpoint=PROJECT_ENDPOINT,
        resource_group="rg-contoso-agents",
        application_insights_name="contoso-agents-insights",
    )
    with pytest.raises(RegistrationError, match="platform project"):
        validate_registration_target(
            plan,
            project_endpoint="https://attacker.example/api/projects/platform",
            resource_group="rg-contoso-agents",
            application_insights_name="contoso-agents-insights",
        )


def test_main_verifies_without_registering_by_default(monkeypatch, repo_root) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        register.boundary,
        "require_clean_live",
        lambda _path, *, enabled_modules: (
            order.append(f"boundary:{','.join(sorted(enabled_modules))}")
            or SimpleNamespace(resource_group="rg-contoso-agents")
        ),
    )
    monkeypatch.setattr(
        register,
        "query_live_telemetry",
        lambda **_kwargs: order.append("query") or payload(5, 5, ["contoso-field-v1"]),
    )
    monkeypatch.setattr(
        register,
        "verify_live_telemetry",
        lambda *_args, **_kwargs: order.append("verify")
        or SimpleNamespace(total_spans=5),
    )

    monkeypatch.setattr(
        register,
        "register_external_agent",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("registration must not run")),
    )
    result = register.main(
        [
            "--project-endpoint",
            PROJECT_ENDPOINT,
            "--resource-group",
            "rg-contoso-agents",
            "--app-insights",
            "contoso-agents-insights",
            "--boundary-config",
            str(repo_root / "config" / "boundary.yaml"),
            "--confirm-resource-group",
            "rg-contoso-agents",
            "--enable-module",
            "optional-control-plane",
            "--smoke-correlation-id",
            "smoke-1",
            "--smoke-started-at",
            "2026-08-25T02:30:00Z",
            "--container-app-revision",
            "contoso-field--rev-1",
        ]
    )
    assert result == 0
    assert order == [
        "boundary:optional-control-plane",
        "query",
        "verify",
    ]


def test_main_registers_only_after_explicit_confirmation(monkeypatch, repo_root) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        register.boundary,
        "require_clean_live",
        lambda _path, *, enabled_modules: (
            order.append(f"boundary:{','.join(sorted(enabled_modules))}")
            or SimpleNamespace(resource_group="rg-contoso-agents")
        ),
    )
    monkeypatch.setattr(
        register,
        "query_live_telemetry",
        lambda **_kwargs: order.append("query") or payload(5, 5, ["contoso-field-v1"]),
    )
    monkeypatch.setattr(
        register,
        "verify_live_telemetry",
        lambda *_args, **_kwargs: order.append("verify")
        or SimpleNamespace(total_spans=5),
    )

    def fake_register(**kwargs):
        assert kwargs["allow_write"] is True
        order.append("register")
        return SimpleNamespace(name="contoso-field", version="1")

    monkeypatch.setattr(register, "register_external_agent", fake_register)
    result = register.main(
        [
            "--project-endpoint",
            PROJECT_ENDPOINT,
            "--resource-group",
            "rg-contoso-agents",
            "--app-insights",
            "contoso-agents-insights",
            "--boundary-config",
            str(repo_root / "config" / "boundary.yaml"),
            "--confirm-resource-group",
            "rg-contoso-agents",
            "--enable-module",
            "optional-control-plane",
            "--smoke-correlation-id",
            "smoke-1",
            "--smoke-started-at",
            "2026-08-25T02:30:00Z",
            "--container-app-revision",
            "contoso-field--rev-1",
            "--register",
            "--confirm-registration",
            "contoso-field",
        ]
    )
    assert result == 0
    assert order == [
        "boundary:optional-control-plane",
        "query",
        "verify",
        "register",
    ]


def test_main_rejects_registration_without_exact_confirmation(repo_root) -> None:
    with pytest.raises(SystemExit):
        register.main(
            [
                "--project-endpoint",
                PROJECT_ENDPOINT,
                "--boundary-config",
                str(repo_root / "config" / "boundary.yaml"),
                "--confirm-resource-group",
                "rg-contoso-agents",
                "--smoke-correlation-id",
                "smoke-1",
                "--smoke-started-at",
                "2026-08-25T02:30:00Z",
                "--container-app-revision",
                "contoso-field--rev-1",
                "--register",
            ]
        )
