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


def _function_tools(contract_path: Path) -> tuple[dict[str, Any], ...]:
    contract = load_contract(contract_path)
    return tuple(
        {
            "type": "function",
            "name": tool.name,
            "description": tool.summary,
            "parameters": tool.parameters,
            # Optional toolbox parameters make strict mode unsuitable.
            "strict": False,
        }
        for tool in contract.tools
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
    tools = _function_tools(contract_path)
    digest_input = {
        "definition_version": str(document["definition_version"]),
        "instructions": instructions,
        "model": model,
        "tools": tools,
    }
    digest = hashlib.sha256(json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    return AgentSpec(
        name=str(document["name"]),
        definition_version=str(document["definition_version"]),
        project=str(document["project"]),
        deployment_name=str(model["deployment"]),
        model_name=str(model["name"]),
        model_version=str(model["version"]),
        instructions=instructions,
        tools=tools,
        digest=f"sha256:{digest}",
        telemetry_service_name=str(document["telemetry"]["service_name"]),
    )


def create_agent_version(project_client: Any, spec: AgentSpec) -> dict[str, Any]:
    """Create one immutable Foundry agent version from the checked-in definition."""
    from azure.ai.projects.models import PromptAgentDefinition

    definition = PromptAgentDefinition(
        model=spec.deployment_name,
        instructions=spec.instructions,
        tools=list(spec.tools),
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
