"""Fail-closed verification of a deployed hosted-agent version and route."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from contoso_foundry import azure_cli

_AUDIENCE = "https://ai.azure.com"
_FOUNDRY_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}\.services\.ai\.azure\.com$", re.IGNORECASE)
_PROJECT_PATH = re.compile(r"^/api/projects/[a-z0-9][a-z0-9-]{0,63}$", re.IGNORECASE)
_SIMPLE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise DeploymentVerificationError("project_endpoint is not a Foundry project endpoint") from error
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.hostname
        or not _FOUNDRY_HOST.fullmatch(parsed.hostname)
        or not _PROJECT_PATH.fullmatch(parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise DeploymentVerificationError("project_endpoint is not a Foundry project endpoint")
    if not _SIMPLE_IDENTIFIER.fullmatch(agent_name) or not _SIMPLE_IDENTIFIER.fullmatch(expected_version):
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
