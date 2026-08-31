"""Evidence-gated Foundry external-agent registration."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import ExternalAgentDefinition
from azure.identity import DefaultAzureCredential

from contoso_foundry import azure_cli, boundary

from .settings import FieldSettings
from .telemetry import SMOKE_CORRELATION_ID, SMOKE_REVISION

_SAFE_DIMENSION = re.compile(r"^[A-Za-z0-9._-]+$")
_PROJECT_SCOPE = re.compile(
    r"^providers/Microsoft\.CognitiveServices/accounts/"
    r"(?P<account>[a-z0-9][a-z0-9-]{1,62})/projects/"
    r"(?P<project>[a-z0-9][a-z0-9-]{0,63})$",
    re.IGNORECASE,
)
_APP_INSIGHTS_SCOPE = re.compile(
    r"^providers/(?:Microsoft|microsoft)\.Insights/components/(?P<name>[a-z0-9][a-z0-9-]{1,259})$",
    re.IGNORECASE,
)


class RegistrationError(RuntimeError):
    """Raised when live telemetry does not prove the registration identity."""


@dataclass(frozen=True)
class LiveTelemetryEvidence:
    total_spans: int
    spans_with_agent_id: int
    agent_ids: tuple[str, ...]
    operation_ids: tuple[str, ...]
    smoke_correlation_ids: tuple[str, ...]
    container_app_revisions: tuple[str, ...]


@dataclass(frozen=True)
class ExternalAgentReadback:
    name: str
    agent_id: str
    version: str
    version_id: str
    otel_agent_id: str


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
        smoke_correlation_ids=_dynamic_values(row, "smoke_correlation_ids"),
        container_app_revisions=_dynamic_values(row, "container_app_revisions"),
    )
    if evidence.smoke_correlation_ids != (smoke_correlation_id,):
        raise RegistrationError("telemetry evidence does not match the requested smoke correlation ID")
    if evidence.container_app_revisions != (container_app_revision,):
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
let smoke_evidence = union isfuzzy=true dependencies, requests, traces
| where timestamp >= datetime({started_at})
| extend smoke_correlation_id = tostring(customDimensions["{SMOKE_CORRELATION_ID}"])
| extend smoke_revision = tostring(customDimensions["{SMOKE_REVISION}"])
| where cloud_RoleName == "contoso-field"
    and smoke_correlation_id == "{smoke_correlation_id}"
    and smoke_revision == "{container_app_revision}"
| summarize
    operation_ids = make_set(operation_Id, 2),
    smoke_correlation_ids = make_set(smoke_correlation_id, 2),
    container_app_revisions = make_set(smoke_revision, 2);
let smoke_operations = toscalar(smoke_evidence | project operation_ids);
union isfuzzy=true dependencies, requests, traces
| where timestamp >= datetime({started_at})
| where set_has_element(smoke_operations, operation_Id)
| extend agent_name = tostring(customDimensions["gen_ai.agent.name"])
| extend agent_id = tostring(customDimensions["gen_ai.agent.id"])
| where cloud_RoleName == "contoso-field" and agent_name == "{agent_name}"
| summarize
    total_spans = count(),
    spans_with_agent_id = countif(isnotempty(agent_id)),
    agent_ids = make_set_if(agent_id, isnotempty(agent_id), 10),
    operation_ids = make_set(operation_Id, 2)
| extend
    smoke_correlation_ids = toscalar(smoke_evidence | project smoke_correlation_ids),
    container_app_revisions = toscalar(smoke_evidence | project container_app_revisions)
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
    allow_write: bool = False,
) -> ExternalAgentReadback:
    if not allow_write:
        raise RegistrationError("external-agent registration requires an explicit write authorization")
    credential = DefaultAzureCredential(managed_identity_client_id=client_id)
    project = AIProjectClient(
        endpoint=project_endpoint,
        credential=credential,
        allow_preview=True,
    )
    created = project.agents.create_version(
        agent_name=agent_name,
        description="Read-only Contoso field-service agent hosted on Azure Container Apps.",
        definition=ExternalAgentDefinition(otel_agent_id=otel_agent_id),
    )
    created_version = str(getattr(created, "version", "") or "")
    created_name = str(getattr(created, "name", "") or "")
    created_agent_id = str(getattr(created, "agent_guid", "") or "")
    created_version_id = str(getattr(created, "id", "") or "")
    if created_name != agent_name:
        raise RegistrationError(
            f"registration create response name {created_name!r} does not match {agent_name!r}"
        )
    if not created_agent_id or not created_version or not created_version_id:
        raise RegistrationError("registration create response omitted its agent or version identity")
    return verify_registration_readback(
        project.agents.get_version(
            agent_name=agent_name,
            agent_version=created_version,
        ),
        expected_name=agent_name,
        expected_otel_agent_id=otel_agent_id,
        expected_agent_id=created_agent_id,
        expected_version=created_version,
        expected_version_id=created_version_id,
    )


def verify_registration_readback(
    registered: Any,
    *,
    expected_name: str,
    expected_otel_agent_id: str,
    expected_agent_id: str,
    expected_version: str,
    expected_version_id: str,
) -> ExternalAgentReadback:
    definition = getattr(registered, "definition", None)
    readback = ExternalAgentReadback(
        name=str(getattr(registered, "name", "") or ""),
        agent_id=str(getattr(registered, "agent_guid", "") or ""),
        version=str(getattr(registered, "version", "") or ""),
        version_id=str(getattr(registered, "id", "") or ""),
        otel_agent_id=str(getattr(definition, "otel_agent_id", "") or ""),
    )
    if readback.name != expected_name:
        raise RegistrationError(
            f"registration readback name {readback.name!r} does not match {expected_name!r}"
        )
    if readback.otel_agent_id != expected_otel_agent_id:
        raise RegistrationError(
            f"registration readback OpenTelemetry ID {readback.otel_agent_id!r} "
            f"does not match {expected_otel_agent_id!r}"
        )
    if not readback.agent_id or not readback.version or not readback.version_id:
        raise RegistrationError("registration readback omitted its agent or version identity")
    if readback.agent_id != expected_agent_id:
        raise RegistrationError(
            f"registration readback agent ID {readback.agent_id!r} does not match the create response"
        )
    if readback.version != expected_version:
        raise RegistrationError(
            f"registration readback version {readback.version!r} does not match {expected_version!r}"
        )
    if readback.version_id != expected_version_id:
        raise RegistrationError(
            f"registration readback version ID {readback.version_id!r} does not match the create response"
        )
    return readback


def validate_registration_target(
    plan: dict[str, Any],
    *,
    project_endpoint: str,
    resource_group: str,
    application_insights_name: str,
) -> None:
    if resource_group != plan.get("resource_group"):
        raise RegistrationError("registration resource group does not match the ownership boundary")

    projects = [
        item
        for item in plan.get("resources", [])
        if item.get("name") == "platform-project"
    ]
    insights = [
        item
        for item in plan.get("resources", [])
        if item.get("name") == "app-insights"
    ]
    if len(projects) != 1 or len(insights) != 1:
        raise RegistrationError("boundary must declare one platform project and one Application Insights component")

    project_match = _PROJECT_SCOPE.fullmatch(str(projects[0].get("scope", "")))
    insights_match = _APP_INSIGHTS_SCOPE.fullmatch(str(insights[0].get("scope", "")))
    if project_match is None or insights_match is None:
        raise RegistrationError("boundary registration resources do not use exact relative scopes")

    expected_endpoint = (
        f"https://{project_match.group('account')}.services.ai.azure.com"
        f"/api/projects/{project_match.group('project')}"
    )
    if project_endpoint != expected_endpoint:
        raise RegistrationError("project endpoint does not match the boundary-declared platform project")
    if application_insights_name.casefold() != insights_match.group("name").casefold():
        raise RegistrationError("Application Insights name does not match the ownership boundary")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify telemetry, then register the external field agent")
    parser.add_argument("--project-endpoint", default=os.getenv("FOUNDRY_PROJECT_ENDPOINT"))
    parser.add_argument("--resource-group", default="rg-contoso-agents")
    parser.add_argument("--app-insights", default="contoso-agents-insights")
    parser.add_argument("--boundary-config", type=Path, default=Path("config/boundary.yaml"))
    parser.add_argument(
        "--enable-module",
        action="append",
        default=[],
        help="include a disabled-by-default module in the exact live boundary",
    )
    parser.add_argument("--confirm-resource-group", required=True)
    parser.add_argument("--smoke-correlation-id", required=True)
    parser.add_argument("--smoke-started-at", required=True)
    parser.add_argument("--container-app-revision", required=True)
    parser.add_argument(
        "--register",
        action="store_true",
        help="create the external-agent registration after all read-only gates pass",
    )
    parser.add_argument(
        "--confirm-registration",
        help="type the exact Field agent name to authorize --register",
    )
    args = parser.parse_args(argv)
    settings = FieldSettings.from_env()
    if not args.project_endpoint:
        parser.error("--project-endpoint or FOUNDRY_PROJECT_ENDPOINT is required")
    if args.confirm_resource_group != args.resource_group:
        parser.error("--confirm-resource-group must exactly match --resource-group")
    if args.register and args.confirm_registration != settings.agent_name:
        parser.error(f"--register requires --confirm-registration {settings.agent_name}")
    if not args.register and args.confirm_registration:
        parser.error("--confirm-registration is only valid with --register")

    boundary_path = args.boundary_config.resolve()
    enabled_modules = set(args.enable_module) | boundary.enabled_modules_from_environment()
    try:
        report = boundary.require_clean_live(
            boundary_path,
            enabled_modules=enabled_modules,
        )
    except PermissionError as error:
        raise RegistrationError("live ownership boundary is not clean") from error
    plan = boundary.load_plan(boundary_path)
    validate_registration_target(
        plan,
        project_endpoint=args.project_endpoint,
        resource_group=report.resource_group,
        application_insights_name=args.app_insights,
    )
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
    if not args.register:
        print(
            f"verified {evidence.total_spans} live span(s), "
            f"gen_ai.agent.id={settings.otel_agent_id}; registration not requested"
        )
        return 0

    registered = register_external_agent(
        project_endpoint=args.project_endpoint,
        agent_name=settings.agent_name,
        otel_agent_id=settings.otel_agent_id,
        client_id=settings.azure_client_id,
        allow_write=True,
    )
    print(
        f"registered {registered.name} version {registered.version}: "
        f"{evidence.total_spans} verified live span(s), "
        f"gen_ai.agent.id={settings.otel_agent_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
