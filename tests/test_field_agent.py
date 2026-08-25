"""Synthetic and golden tests for the Contoso Field external agent."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import ModelMessage, ModelResponse, RetryPromptPart, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from contoso_foundry.data.build import build
from contoso_foundry.field.api import create_app
from contoso_foundry.field.golden import SCENARIOS
from contoso_foundry.field.runtime import FieldDependencies, create_agent
from contoso_foundry.field.settings import FieldSettings
from contoso_foundry.field.smoke import run_smoke
from contoso_foundry.field.telemetry import (
    GEN_AI_AGENT_ID,
    SMOKE_CORRELATION_ID,
    SMOKE_REVISION,
    MissingAgentIdSpanProcessor,
)
from contoso_foundry.toolbox.identity import principal_from_fixture
from contoso_foundry.toolbox.tools import Toolbox

REPO_ROOT = Path(__file__).resolve().parents[1]


def settings(tmp_path: Path) -> FieldSettings:
    return FieldSettings(
        agent_name="contoso-field",
        otel_agent_id="contoso-field-v1",
        service_name="contoso-field",
        model_deployment="test",
        azure_openai_endpoint="https://example.openai.azure.com/",
        azure_openai_api_version="2025-04-01-preview",
        azure_client_id=None,
        application_insights_connection_string=None,
        enrich_missing_agent_id=False,
        principal_oid="OID-APAC-FIELDENG-01",
        principal_tid="TID-CONTOSO-01",
        data_config=REPO_ROOT / "config" / "data-spine.yaml",
        seed_dir=REPO_ROOT / "data" / "seed",
        contracts_dir=REPO_ROOT / "config" / "toolbox",
        data_dir=tmp_path / "data",
    )


def built_database(tmp_path: Path) -> Path:
    result = build(
        config_path=REPO_ROOT / "config" / "data-spine.yaml",
        seed_dir=REPO_ROOT / "data" / "seed",
        out_dir=tmp_path / "spine",
    )
    return result.root / "contoso.db"


def test_golden_scenarios_resolve_only_through_shared_toolbox(tmp_path: Path) -> None:
    connection = sqlite3.connect(built_database(tmp_path))
    try:
        toolbox = Toolbox(
            connection,
            principal_from_fixture("OID-APAC-FIELDENG-01", "TID-CONTOSO-01"),
            contracts_dir=REPO_ROOT / "config" / "toolbox",
        )
        for scenario in SCENARIOS:
            rendered: list[str] = []
            for tool_name, arguments in scenario.tool_calls:
                rendered.append(str(toolbox.call(tool_name, arguments)))
            combined = "\n".join(rendered)
            assert all(fact in combined for fact in scenario.expected_facts), scenario.name
    finally:
        connection.close()


def test_pydantic_agent_calls_the_canonical_work_order_tool(tmp_path: Path) -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("lookup_work_order", {"work_order_id": "WO-00010"})])
        tool_return = messages[-1].parts[0]
        assert tool_return.part_kind == "tool-return"
        assert "Install additional access point in loading bay" in str(tool_return.content)
        return ModelResponse(parts=[TextPart("WO-00010 is a completed priority access-point installation.")])

    connection = sqlite3.connect(built_database(tmp_path))
    try:
        toolbox = Toolbox(
            connection,
            principal_from_fixture("OID-APAC-FIELDENG-01", "TID-CONTOSO-01"),
            contracts_dir=REPO_ROOT / "config" / "toolbox",
        )
        agent = create_agent(settings(tmp_path), model=FunctionModel(model))
        result = asyncio.run(agent.run("Summarize WO-00010.", deps=FieldDependencies(toolbox)))
        assert result.output == "WO-00010 is a completed priority access-point installation."
        assert [call.tool for call in toolbox.audit] == ["operations_lookup_work_order"]
    finally:
        connection.close()


def test_pydantic_agent_retries_a_malformed_tool_identifier(tmp_path: Path) -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("lookup_work_order", {"work_order_id": "WO-10"})])
        if any(isinstance(part, RetryPromptPart) for part in messages[-1].parts):
            return ModelResponse(parts=[ToolCallPart("lookup_work_order", {"work_order_id": "WO-00010"})])
        return ModelResponse(parts=[TextPart("Recovered after canonical identifier validation.")])

    connection = sqlite3.connect(built_database(tmp_path))
    try:
        toolbox = Toolbox(
            connection,
            principal_from_fixture("OID-APAC-FIELDENG-01", "TID-CONTOSO-01"),
            contracts_dir=REPO_ROOT / "config" / "toolbox",
        )
        agent = create_agent(settings(tmp_path), model=FunctionModel(model))
        result = asyncio.run(agent.run("Summarize WO-10.", deps=FieldDependencies(toolbox)))
        assert result.output == "Recovered after canonical identifier validation."
        assert [call.tool for call in toolbox.audit] == ["operations_lookup_work_order"]
    finally:
        connection.close()


def test_agent_id_processor_only_enriches_missing_pydantic_ai_spans() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(MissingAgentIdSpanProcessor("contoso-field-v1"))
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with provider.get_tracer("pydantic-ai").start_as_current_span("missing"):
        pass
    with provider.get_tracer("pydantic-ai").start_as_current_span(
        "present", attributes={GEN_AI_AGENT_ID: "framework-value"}
    ):
        pass
    with provider.get_tracer("fastapi").start_as_current_span("unrelated"):
        pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["missing"].attributes[GEN_AI_AGENT_ID] == "contoso-field-v1"
    assert spans["present"].attributes[GEN_AI_AGENT_ID] == "framework-value"
    assert GEN_AI_AGENT_ID not in spans["unrelated"].attributes


def test_smoke_span_carries_correlation_and_revision() -> None:
    class Service:
        async def run(self, prompt: str) -> str:
            return f"result for {prompt}"

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    result = asyncio.run(
        run_smoke(
            Service(),
            "golden",
            "smoke-1",
            "contoso-field--rev-1",
            tracer=provider.get_tracer("test-smoke"),
        )
    )

    assert result == "result for golden"
    span = exporter.get_finished_spans()[0]
    assert span.attributes[SMOKE_CORRELATION_ID] == "smoke-1"
    assert span.attributes[SMOKE_REVISION] == "contoso-field--rev-1"


def test_runtime_config_file_is_read_with_environment_precedence(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "field-runtime-config"
    config_file.write_text(
        json.dumps(
            {
                "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=synthetic",
                "AZURE_OPENAI_ENDPOINT": "https://from-file.openai.azure.com/",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FIELD_RUNTIME_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://from-env.openai.azure.com/")

    configured = FieldSettings.from_env()

    assert configured.azure_openai_endpoint == "https://from-env.openai.azure.com/"
    assert configured.application_insights_connection_string == "InstrumentationKey=synthetic"


def test_internal_api_starts_and_reports_healthy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("FIELD_DATA_DIR", str(tmp_path / "api-data"))
    monkeypatch.setenv("FIELD_DATA_CONFIG", str(REPO_ROOT / "config" / "data-spine.yaml"))
    monkeypatch.setenv("FIELD_SEED_DIR", str(REPO_ROOT / "data" / "seed"))
    monkeypatch.setenv("FIELD_CONTRACTS_DIR", str(REPO_ROOT / "config" / "toolbox"))
    with TestClient(create_app()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
