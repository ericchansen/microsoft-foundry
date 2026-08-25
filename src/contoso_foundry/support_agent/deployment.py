"""Fail-closed verification of a deployed hosted-agent version and route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contoso_foundry import azure_cli

_AUDIENCE = "https://ai.azure.com"


class DeploymentVerificationError(RuntimeError):
    """Raised when the deployed version is not active and exclusively routed."""


@dataclass(frozen=True)
class DeploymentEvidence:
    agent_name: str
    version: str
    status: str
    traffic_percentage: int
    protocol: str


def _get(url: str) -> dict[str, Any]:
    result = azure_cli.run(
        [
            "rest",
            "--method",
            "GET",
            "--url",
            url,
            "--resource",
            _AUDIENCE,
        ]
    )
    if not isinstance(result, dict):
        raise DeploymentVerificationError(f"Foundry returned an invalid response for {url}")
    return result


def verify_deployment(
    *,
    project_endpoint: str,
    agent_name: str,
    expected_version: str,
) -> DeploymentEvidence:
    """Verify version status, Responses protocol, and the sole 100% route."""

    endpoint = project_endpoint.rstrip("/")
    if not endpoint.startswith("https://") or "/api/projects/" not in endpoint:
        raise DeploymentVerificationError("project_endpoint is not a Foundry project endpoint")
    if not agent_name or "/" in agent_name or not expected_version or "/" in expected_version:
        raise DeploymentVerificationError("agent name and expected version must be simple identifiers")

    agent = _get(f"{endpoint}/agents/{agent_name}?api-version=v1")
    version = _get(f"{endpoint}/agents/{agent_name}/versions/{expected_version}?api-version=v1")

    status = str(version.get("status", "")).lower()
    if status != "active":
        raise DeploymentVerificationError(
            f"hosted agent {agent_name!r} version {expected_version!r} is {status or 'missing'}, not active"
        )

    definition = version.get("definition")
    protocol_versions = definition.get("protocol_versions") if isinstance(definition, dict) else None
    if not isinstance(protocol_versions, list) or not any(
        isinstance(item, dict)
        and item.get("protocol") == "responses"
        and item.get("version") == "2.0.0"
        for item in protocol_versions
    ):
        raise DeploymentVerificationError("deployed version does not expose Responses protocol 2.0.0")

    agent_endpoint = agent.get("agent_endpoint")
    selector = agent_endpoint.get("version_selector") if isinstance(agent_endpoint, dict) else None
    rules = selector.get("version_selection_rules") if isinstance(selector, dict) else None
    expected_rule = {
        "agent_version": expected_version,
        "traffic_percentage": 100,
        "type": "FixedRatio",
    }
    if rules != [expected_rule]:
        raise DeploymentVerificationError(
            f"endpoint must route exactly 100% to version {expected_version!r}; observed {rules!r}"
        )

    protocol_configuration = (
        agent_endpoint.get("protocol_configuration") if isinstance(agent_endpoint, dict) else None
    )
    if not isinstance(protocol_configuration, dict) or "responses" not in protocol_configuration:
        raise DeploymentVerificationError("agent endpoint does not publish the Responses protocol")

    return DeploymentEvidence(
        agent_name=agent_name,
        version=expected_version,
        status=status,
        traffic_percentage=100,
        protocol="responses/2.0.0",
    )
