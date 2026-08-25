"""Evidence-gated Foundry external-agent registration."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import ExternalAgentDefinition
from azure.identity import DefaultAzureCredential

from contoso_foundry import azure_cli

from .settings import FieldSettings
from .telemetry import SMOKE_CORRELATION_ID, SMOKE_REVISION

_SAFE_DIMENSION = re.compile(r"^[A-Za-z0-9._-]+$")


class RegistrationError(RuntimeError):
    """Raised when live telemetry does not prove the registration identity."""


@dataclass(frozen=True)
class LiveTelemetryEvidence:
    total_spans: int
    spans_with_agent_id: int
    agent_ids: tuple[str, ...]
    operation_ids: tuple[str, ...]
    smoke_correlation_id: str
    container_app_revision: str


def _table_row(payload: dict[str, Any]) -> dict[str, Any]:
    tables = payload.get("tables", [])
    if not tables or not tables[0].get("rows"):
        raise RegistrationError("Application Insights returned no telemetry summary row")
    table = tables[0]
    columns = [str(column["name"]) for column in table["columns"]]
    return dict(zip(columns, table["rows"][0], strict=True))


def _dynamic_values(row: dict[str, Any], name: str) -> tuple[str, ...]:
    values = row.get(name, [])
    if isinstance(values, str):
        values = json.loads(values)
    if not isinstance(values, list):
        raise RegistrationError(f"Application Insights returned {name} in an unexpected shape")
    return tuple(sorted(str(value) for value in values))


def verify_live_telemetry(
    payload: dict[str, Any],
    *,
    expected_agent_id: str,
    smoke_correlation_id: str,
    container_app_revision: str,
) -> LiveTelemetryEvidence:
    row = _table_row(payload)
    evidence = LiveTelemetryEvidence(
        total_spans=int(row["total_spans"]),
        spans_with_agent_id=int(row["spans_with_agent_id"]),
        agent_ids=_dynamic_values(row, "agent_ids"),
        operation_ids=_dynamic_values(row, "operation_ids"),
        smoke_correlation_id=str(row.get("smoke_correlation_id", "")),
        container_app_revision=str(row.get("container_app_revision", "")),
    )
    if evidence.smoke_correlation_id != smoke_correlation_id:
        raise RegistrationError("telemetry evidence does not match the requested smoke correlation ID")
    if evidence.container_app_revision != container_app_revision:
        raise RegistrationError("telemetry evidence does not match the requested Container App revision")
    if len(evidence.operation_ids) != 1:
        raise RegistrationError("telemetry evidence must contain exactly one correlated smoke operation")
    if evidence.total_spans == 0:
        raise RegistrationError("no live Pydantic AI spans were found for contoso-field")
    if evidence.spans_with_agent_id != evidence.total_spans:
        raise RegistrationError(
            f"only {evidence.spans_with_agent_id} of {evidence.total_spans} live Pydantic AI spans "
            "carry gen_ai.agent.id"
        )
    if evidence.agent_ids != (expected_agent_id,):
        raise RegistrationError(
            f"live gen_ai.agent.id values {evidence.agent_ids!r} do not match {expected_agent_id!r}"
        )
    return evidence


def query_live_telemetry(
    *,
    resource_group: str,
    application_insights_name: str,
    agent_name: str,
    smoke_correlation_id: str,
    smoke_started_at: datetime,
    container_app_revision: str,
) -> dict[str, Any]:
    for name, value in {
        "agent name": agent_name,
        "smoke correlation ID": smoke_correlation_id,
        "Container App revision": container_app_revision,
    }.items():
        if not _SAFE_DIMENSION.fullmatch(value):
            raise RegistrationError(f"{name} contains unsupported characters")
    if smoke_started_at.tzinfo is None:
        raise RegistrationError("smoke start time must include a timezone")
    started_at = smoke_started_at.astimezone(UTC).isoformat().replace("+00:00", "Z")

    query = f"""
let smoke_operations = union isfuzzy=true dependencies, requests, traces
| where timestamp >= datetime({started_at})
| extend smoke_correlation_id = tostring(customDimensions["{SMOKE_CORRELATION_ID}"])
| extend smoke_revision = tostring(customDimensions["{SMOKE_REVISION}"])
| where cloud_RoleName == "contoso-field"
    and smoke_correlation_id == "{smoke_correlation_id}"
    and smoke_revision == "{container_app_revision}"
| distinct operation_Id;
union isfuzzy=true dependencies, requests, traces
| where timestamp >= datetime({started_at})
| where operation_Id in (smoke_operations)
| extend agent_name = tostring(customDimensions["gen_ai.agent.name"])
| extend agent_id = tostring(customDimensions["gen_ai.agent.id"])
| where cloud_RoleName == "contoso-field" and agent_name == "{agent_name}"
| summarize
    total_spans = count(),
    spans_with_agent_id = countif(isnotempty(agent_id)),
    agent_ids = make_set_if(agent_id, isnotempty(agent_id), 10),
    operation_ids = make_set(operation_Id, 2)
| extend smoke_correlation_id = "{smoke_correlation_id}"
| extend container_app_revision = "{container_app_revision}"
""".strip()
    query = " ".join(line.strip() for line in query.splitlines())
    payload = azure_cli.run(
        [
            "monitor",
            "app-insights",
            "query",
            "--resource-group",
            resource_group,
            "--app",
            application_insights_name,
            "--analytics-query",
            query,
        ]
    )
    if not isinstance(payload, dict):
        raise RegistrationError("Application Insights query returned an unexpected response")
    return payload


def register_external_agent(
    *,
    project_endpoint: str,
    agent_name: str,
    otel_agent_id: str,
    client_id: str | None,
) -> Any:
    credential = DefaultAzureCredential(managed_identity_client_id=client_id)
    project = AIProjectClient(
        endpoint=project_endpoint,
        credential=credential,
        allow_preview=True,
    )
    return project.agents.create_version(
        agent_name=agent_name,
        description="Read-only Contoso field-service agent hosted on Azure Container Apps.",
        definition=ExternalAgentDefinition(otel_agent_id=otel_agent_id),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify telemetry, then register the external field agent")
    parser.add_argument("--project-endpoint", default=os.getenv("FOUNDRY_PROJECT_ENDPOINT"))
    parser.add_argument("--resource-group", default="rg-contoso-agents")
    parser.add_argument("--app-insights", default="contoso-agents-insights")
    parser.add_argument("--smoke-correlation-id", required=True)
    parser.add_argument("--smoke-started-at", required=True)
    parser.add_argument("--container-app-revision", required=True)
    args = parser.parse_args(argv)
    if not args.project_endpoint:
        parser.error("--project-endpoint or FOUNDRY_PROJECT_ENDPOINT is required")

    settings = FieldSettings.from_env()
    try:
        smoke_started_at = datetime.fromisoformat(args.smoke_started_at.replace("Z", "+00:00"))
    except ValueError as error:
        parser.error(f"--smoke-started-at must be ISO 8601: {error}")
    payload = query_live_telemetry(
        resource_group=args.resource_group,
        application_insights_name=args.app_insights,
        agent_name=settings.agent_name,
        smoke_correlation_id=args.smoke_correlation_id,
        smoke_started_at=smoke_started_at,
        container_app_revision=args.container_app_revision,
    )
    evidence = verify_live_telemetry(
        payload,
        expected_agent_id=settings.otel_agent_id,
        smoke_correlation_id=args.smoke_correlation_id,
        container_app_revision=args.container_app_revision,
    )
    registered = register_external_agent(
        project_endpoint=args.project_endpoint,
        agent_name=settings.agent_name,
        otel_agent_id=settings.otel_agent_id,
        client_id=settings.azure_client_id,
    )
    print(
        f"registered {registered.name}: {evidence.total_spans} verified live span(s), "
        f"gen_ai.agent.id={settings.otel_agent_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
