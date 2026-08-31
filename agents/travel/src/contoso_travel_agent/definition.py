"""Load the pinned prompt-agent definition and render its deployment payload."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from contoso_foundry.toolbox.contracts import load_contract


class AgentDefinitionError(RuntimeError):
    """Raised when the checked-in definition is incomplete or unpinned."""


@dataclass(frozen=True)
class AgentSpec:
    name: str
    definition_version: str
    project: str
    deployment_name: str
    model_name: str
    model_version: str
    instructions: str
    tools: tuple[dict[str, Any], ...]
    tool_connection_name: str
    digest: str
    telemetry_service_name: str

    def manifest(self, *, created_version: str | None = None) -> dict[str, Any]:
        manifest = {
            "agent_name": self.name,
            "definition_version": self.definition_version,
            "definition_digest": self.digest,
            "model_deployment": self.deployment_name,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "project": self.project,
        }
        if created_version is not None:
            if not created_version.strip():
                raise AgentDefinitionError("the service returned an empty agent version")
            manifest["created_version"] = created_version
        return manifest


def _openapi_operations(
    contract_path: Path,
    operation_names: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    contract = load_contract(contract_path)
    available = {tool.name: tool for tool in contract.tools}
    missing = sorted(set(operation_names) - set(available))
    if missing:
        raise AgentDefinitionError(
            f"agent.yaml references unknown Travel operations: {', '.join(missing)}"
        )
    return tuple(
        {
            "name": available[name].name,
            "title": available[name].title,
            "description": available[name].summary,
            "parameters": available[name].parameters,
            "returns": available[name].returns,
            "side_effect": available[name].side_effect,
        }
        for name in operation_names
    )


def _openapi_spec(
    operations: tuple[dict[str, Any], ...],
    *,
    server_url: str,
) -> dict[str, Any]:
    paths = {}
    for operation in operations:
        paths[f"/operations/{operation['name']}"] = {
            "post": {
                "operationId": operation["name"],
                "summary": operation["title"],
                "description": operation["description"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": operation["parameters"],
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Synthetic Travel operation result.",
                        "content": {
                            "application/json": {
                                "schema": operation["returns"],
                            }
                        },
                    },
                    "400": {"description": "The operation arguments were rejected."},
                    "401": {"description": "The project connection was not authenticated."},
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Contoso synthetic Travel Toolbox",
            "version": "2.0.0",
            "description": (
                "Four deterministic synthetic travel operations. Caller identity and "
                "regional scope are fixed by the service."
            ),
        },
        "servers": [{"url": server_url.rstrip("/")}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "TravelToolKey": {
                    "type": "apiKey",
                    "name": "x-travel-tool-key",
                    "in": "header",
                }
            }
        },
        "security": [{"TravelToolKey": []}],
    }


def build_agent_definition(
    spec: AgentSpec,
    *,
    server_url: str,
    project_connection_id: str,
) -> Any:
    """Render the typed server-executed OpenAPI tool for one environment."""
    from azure.ai.projects.models import (
        OpenApiFunctionDefinition,
        OpenApiProjectConnectionAuthDetails,
        OpenApiProjectConnectionSecurityScheme,
        OpenApiTool,
        PromptAgentDefinition,
    )

    if not server_url.startswith("https://"):
        raise AgentDefinitionError("the Travel tool connection target must use HTTPS")
    if not project_connection_id.strip():
        raise AgentDefinitionError("the Travel tool project connection ID is required")
    tool = OpenApiTool(
        openapi=OpenApiFunctionDefinition(
            name="contoso_travel_toolbox",
            description="Four authenticated synthetic Travel operations with immutable server scope.",
            spec=_openapi_spec(spec.tools, server_url=server_url),
            auth=OpenApiProjectConnectionAuthDetails(
                security_scheme=OpenApiProjectConnectionSecurityScheme(
                    project_connection_id=project_connection_id,
                )
            ),
        )
    )
    return PromptAgentDefinition(
        model=spec.deployment_name,
        instructions=spec.instructions,
        tools=[tool],
    )


def load_agent_spec(path: Path) -> AgentSpec:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AgentDefinitionError("agent.yaml must contain a mapping")

    model = document.get("model")
    if not isinstance(model, dict):
        raise AgentDefinitionError("agent.yaml must declare a model")
    required_model = ("deployment", "name", "version", "sku", "version_upgrade_option")
    missing = [key for key in required_model if not model.get(key)]
    if missing:
        raise AgentDefinitionError(f"the model is not pinned: missing {', '.join(missing)}")
    if str(model["version"]).lower() == "latest":
        raise AgentDefinitionError("the model version must be exact, not latest")
    if model["version_upgrade_option"] != "NoAutoUpgrade":
        raise AgentDefinitionError("model deployments must use NoAutoUpgrade")

    instructions_path = path.parent / str(document["instructions"])
    contract_path = (path.parent / str(document["tool_contract"])).resolve()
    instructions = instructions_path.read_text(encoding="utf-8").strip()
    if not instructions:
        raise AgentDefinitionError("instructions must not be empty")
    transport = document.get("tool_transport")
    if not isinstance(transport, dict) or transport.get("type") != "openapi_project_connection":
        raise AgentDefinitionError(
            "browser-facing Travel agents require openapi_project_connection tools"
        )
    connection_name = str(transport.get("connection", "")).strip()
    operation_names = tuple(str(name) for name in transport.get("operations", []))
    if not connection_name or not operation_names:
        raise AgentDefinitionError("OpenAPI transport requires a connection and operations")
    if len(operation_names) != len(set(operation_names)):
        raise AgentDefinitionError("OpenAPI operation names must be unique")
    definition_version = str(document["definition_version"])
    version_parts = definition_version.split(".")
    if len(version_parts) != 3 or not all(part.isdigit() for part in version_parts):
        raise AgentDefinitionError("definition_version must use major.minor.patch")
    expected_connection = f"travel-openapi-v{version_parts[0]}"
    if connection_name != expected_connection:
        raise AgentDefinitionError(
            f"definition version {definition_version} requires connection {expected_connection!r}"
        )
    tools = _openapi_operations(contract_path, operation_names)
    digest_input = {
        "definition_version": definition_version,
        "instructions": instructions,
        "model": model,
        "tool_connection_name": connection_name,
        "tool_transport": "openapi_project_connection",
        "tools": tools,
    }
    digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    return AgentSpec(
        name=str(document["name"]),
        definition_version=definition_version,
        project=str(document["project"]),
        deployment_name=str(model["deployment"]),
        model_name=str(model["name"]),
        model_version=str(model["version"]),
        instructions=instructions,
        tools=tools,
        tool_connection_name=connection_name,
        digest=f"sha256:{digest}",
        telemetry_service_name=str(document["telemetry"]["service_name"]),
    )


def create_agent_version(project_client: Any, spec: AgentSpec) -> dict[str, Any]:
    """Create one immutable Foundry agent version from the checked-in definition."""
    connection = project_client.connections.get(spec.tool_connection_name)
    server_url = str(getattr(connection, "target", "")).strip()
    connection_id = str(getattr(connection, "id", "")).strip()
    if not server_url:
        raise AgentDefinitionError(
            f"project connection {spec.tool_connection_name!r} has no target"
        )
    if not connection_id:
        raise AgentDefinitionError(
            f"project connection {spec.tool_connection_name!r} has no resource ID"
        )
    definition = build_agent_definition(
        spec,
        server_url=server_url,
        project_connection_id=connection_id,
    )
    created = project_client.agents.create_version(
        agent_name=spec.name,
        definition=definition,
        metadata={"definition_digest": spec.digest},
    )
    returned_name = str(getattr(created, "name", "")).strip()
    version = str(getattr(created, "version", "")).strip()
    if returned_name != spec.name:
        raise AgentDefinitionError(
            f"the service returned agent name {returned_name!r}, expected {spec.name!r}"
        )
    manifest = spec.manifest(created_version=version)
    readback = project_client.agents.get_version(
        agent_name=spec.name,
        agent_version=version,
    )
    if (
        str(getattr(readback, "name", "")).strip() != spec.name
        or str(getattr(readback, "version", "")).strip() != version
    ):
        raise AgentDefinitionError("the service read back a different agent version")
    metadata = getattr(readback, "metadata", None)
    if not isinstance(metadata, dict) or metadata.get("definition_digest") != spec.digest:
        raise AgentDefinitionError("the service did not preserve the definition digest")
    returned_definition = getattr(readback, "definition", None)
    if not hasattr(returned_definition, "as_dict") or returned_definition.as_dict() != definition.as_dict():
        raise AgentDefinitionError("the service read-back definition does not match the candidate")
    return manifest
