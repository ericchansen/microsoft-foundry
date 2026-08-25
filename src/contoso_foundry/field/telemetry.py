"""Azure Monitor OpenTelemetry wiring for Pydantic AI."""

from __future__ import annotations

import os
from contextlib import suppress

from azure.identity import DefaultAzureCredential
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from pydantic_ai import Agent, InstrumentationSettings

from .settings import FieldSettings

GEN_AI_AGENT_ID = "gen_ai.agent.id"
PYDANTIC_AI_SCOPE = "pydantic-ai"

_configured = False


class MissingAgentIdSpanProcessor(SpanProcessor):
    """Project glue for frameworks that omit the Foundry external-agent correlation ID."""

    def __init__(self, agent_id: str) -> None:
        self._agent_id = agent_id

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        del parent_context
        scope = getattr(span, "instrumentation_scope", None)
        attributes = getattr(span, "attributes", None) or {}
        if (
            span.is_recording()
            and getattr(scope, "name", None) == PYDANTIC_AI_SCOPE
            and GEN_AI_AGENT_ID not in attributes
        ):
            span.set_attribute(GEN_AI_AGENT_ID, self._agent_id)

    def on_end(self, span: ReadableSpan) -> None:
        del span


def configure_telemetry(settings: FieldSettings) -> None:
    """Configure Azure Monitor once, before the Pydantic AI agent is constructed."""
    global _configured
    if _configured or not settings.application_insights_connection_string:
        return

    os.environ["OTEL_SERVICE_NAME"] = settings.service_name
    credential = DefaultAzureCredential(managed_identity_client_id=settings.azure_client_id)
    configure_azure_monitor(
        connection_string=settings.application_insights_connection_string,
        credential=credential,
        enable_live_metrics=False,
    )

    provider = trace.get_tracer_provider()
    if settings.enrich_missing_agent_id:
        add_processor = getattr(provider, "add_span_processor", None)
        if add_processor is None:
            raise RuntimeError("the configured tracer provider cannot install the agent-id span processor")
        add_processor(MissingAgentIdSpanProcessor(settings.otel_agent_id))

    Agent.instrument_all(
        InstrumentationSettings(
            tracer_provider=provider,
            include_content=False,
        )
    )
    _configured = True


def flush_telemetry(timeout_millis: int = 10_000) -> bool:
    provider = trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if force_flush is None:
        return False
    with suppress(Exception):
        return bool(force_flush(timeout_millis=timeout_millis))
    return False
