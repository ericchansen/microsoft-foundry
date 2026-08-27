"""Loading and validating the versioned tool contracts in ``config/toolbox``.

A tool contract is a promise made to two audiences at once: to an agent, about
what it may ask; and to a reviewer, about what it cannot. The validator here
enforces the second promise mechanically, because a security property that is
only stated in prose is a security property that drifts.

The contracts are deliberately framework-neutral. They are YAML documents whose
``parameters`` block is JSON Schema, so the same file can be rendered into a
Foundry tool definition, an MCP ``tools/list`` entry, or an OpenAI function
schema without any of those vocabularies leaking into the source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# Parameter names that would let a caller express an opinion about whose data
# they are reading. Every one of these has appeared in a real prompt-injection
# write-up; the point of banning the *names* is that a contract reviewer sees
# the violation in the diff rather than having to reason about the handler.
FORBIDDEN_PARAMETERS = frozenset(
    {
        "oid",
        "tid",
        "object_id",
        "tenant_id",
        "principal",
        "principal_oid",
        "upn",
        "user",
        "user_id",
        "username",
        "scope",
        "scopes",
        "scope_regions",
        "region_override",
        "regions_override",
        "on_behalf_of",
        "impersonate",
        "as_user",
        "run_as",
        "act_as",
        "acting_employee_id",
        "caller_employee_id",
        "roles",
        "role",
        "bypass_scope",
        "include_all_regions",
        "all_regions",
        "unscoped",
    }
)

# A contract may say its scope comes from the server, and nothing else. The
# field exists so that the absence of a decision is impossible: a contract that
# forgets to say fails validation rather than silently defaulting.
ALLOWED_SCOPE_SOURCES = frozenset({"server"})

ALLOWED_SIDE_EFFECTS = frozenset({"read", "write", "simulate"})

ALLOWED_CLASSIFICATIONS = frozenset({"public", "internal", "personal", "restricted"})


class ContractError(Exception):
    """Raised when a contract document violates an invariant."""


@dataclass(frozen=True)
class ToolContract:
    """One callable tool, as declared."""

    name: str
    title: str
    summary: str
    side_effect: str
    required_roles: tuple[str, ...]
    data_classification: str
    parameters: dict[str, Any]
    returns: dict[str, Any]
    examples: tuple[dict[str, Any], ...] = ()

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.parameters.get("properties", {})))


@dataclass(frozen=True)
class ToolboxContract:
    """One capability area: a versioned file containing related tools."""

    capability: str
    version: str
    title: str
    summary: str
    scope_source: str
    policy_keys: tuple[str, ...]
    tools: tuple[ToolContract, ...]
    source_path: Path

    def tool(self, name: str) -> ToolContract:
        for candidate in self.tools:
            if candidate.name == name:
                return candidate
        raise KeyError(name)


def _require(document: dict[str, Any], key: str, where: str) -> Any:
    if key not in document:
        raise ContractError(f"{where}: missing required field {key!r}")
    return document[key]


def _validate_parameters(schema: Any, where: str) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise ContractError(f"{where}: parameters must be a JSON Schema object")
    if schema.get("type") != "object":
        raise ContractError(f"{where}: parameters must declare type: object")
    if schema.get("additionalProperties") is not False:
        # Without this, a caller can attach any key they like. The handler would
        # ignore it, but a permissive schema also tells a model that unlisted
        # arguments are worth trying.
        raise ContractError(f"{where}: parameters must set additionalProperties: false")

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ContractError(f"{where}: parameters must declare a properties object")

    for name, definition in sorted(properties.items()):
        lowered = name.lower()
        if lowered in FORBIDDEN_PARAMETERS:
            raise ContractError(
                f"{where}: parameter {name!r} would let a caller choose whose data to read; "
                "scope is resolved server-side"
            )
        if not isinstance(definition, dict):
            raise ContractError(f"{where}: parameter {name!r} must be an object")
        if not definition.get("description"):
            # An undescribed parameter is one a model will guess at, and a guess
            # is how a business filter turns into an accidental probe.
            raise ContractError(f"{where}: parameter {name!r} needs a description")
        if "type" not in definition and "enum" not in definition:
            raise ContractError(f"{where}: parameter {name!r} needs a type or an enum")

    for required in schema.get("required", []):
        if required not in properties:
            raise ContractError(f"{where}: required parameter {required!r} is not declared in properties")

    return schema


def _validate_tool(document: Any, where: str) -> ToolContract:
    if not isinstance(document, dict):
        raise ContractError(f"{where}: each tool must be a mapping")

    name = str(_require(document, "name", where))
    tool_where = f"{where}/{name}"

    side_effect = str(_require(document, "side_effect", tool_where))
    if side_effect not in ALLOWED_SIDE_EFFECTS:
        raise ContractError(f"{tool_where}: side_effect must be one of {sorted(ALLOWED_SIDE_EFFECTS)}")

    classification = str(_require(document, "data_classification", tool_where))
    if classification not in ALLOWED_CLASSIFICATIONS:
        raise ContractError(f"{tool_where}: data_classification must be one of {sorted(ALLOWED_CLASSIFICATIONS)}")

    roles = _require(document, "required_roles", tool_where)
    if not isinstance(roles, list) or not roles:
        # A tool with no required role is a tool anyone can call. That may be the
        # right answer, but it should be spelled with an explicit role rather
        # than by omission.
        raise ContractError(f"{tool_where}: required_roles must be a non-empty list")

    parameters = _validate_parameters(_require(document, "parameters", tool_where), tool_where)

    returns = _require(document, "returns", tool_where)
    if not isinstance(returns, dict) or "type" not in returns:
        raise ContractError(f"{tool_where}: returns must be a schema object with a type")

    examples = document.get("examples", [])
    if not isinstance(examples, list):
        raise ContractError(f"{tool_where}: examples must be a list")

    return ToolContract(
        name=name,
        title=str(_require(document, "title", tool_where)),
        summary=str(_require(document, "summary", tool_where)),
        side_effect=side_effect,
        required_roles=tuple(str(role) for role in roles),
        data_classification=classification,
        parameters=parameters,
        returns=returns,
        examples=tuple(examples),
    )


def load_contract(path: Path) -> ToolboxContract:
    """Load and validate one capability file."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ContractError(f"{path.name}: the contract must be a mapping")

    where = path.name
    version = str(_require(document, "version", where))
    if not SEMVER.match(version):
        # Semver rather than a date so that a breaking parameter change is
        # expressible. Agents pin a major; a minor addition must not break them.
        raise ContractError(f"{where}: version {version!r} is not semver (major.minor.patch)")

    identity = _require(document, "identity", where)
    if not isinstance(identity, dict):
        raise ContractError(f"{where}: identity must be a mapping")

    scope_source = str(_require(identity, "scope_source", f"{where}/identity"))
    if scope_source not in ALLOWED_SCOPE_SOURCES:
        raise ContractError(
            f"{where}/identity: scope_source must be 'server'; a contract that accepts scope from the "
            "caller accepts it from the model"
        )

    policy_keys = _require(identity, "policy_keys", f"{where}/identity")
    if not isinstance(policy_keys, list) or sorted(str(key) for key in policy_keys) != ["oid", "tid"]:
        raise ContractError(
            f"{where}/identity: policy_keys must be exactly ['oid', 'tid'] - the immutable directory keys"
        )

    tools_document = _require(document, "tools", where)
    if not isinstance(tools_document, list) or not tools_document:
        raise ContractError(f"{where}: tools must be a non-empty list")

    tools = tuple(_validate_tool(item, where) for item in tools_document)

    names = [tool.name for tool in tools]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ContractError(f"{where}: duplicate tool names: {', '.join(duplicates)}")

    return ToolboxContract(
        capability=str(_require(document, "capability", where)),
        version=version,
        title=str(_require(document, "title", where)),
        summary=str(_require(document, "summary", where)),
        scope_source=scope_source,
        policy_keys=tuple(sorted(str(key) for key in policy_keys)),
        tools=tools,
        source_path=path,
    )


def load_contracts(directory: Path) -> list[ToolboxContract]:
    """Load every contract in a directory, in a stable order."""
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ContractError(f"no tool contracts found in {directory}")
    return [load_contract(path) for path in paths]


def load_contracts_reporting(directory: Path) -> tuple[list[ToolboxContract], list[str]]:
    """Load every contract, keeping the good ones when a sibling is broken.

    ``load_contracts`` raises on the first bad file, which is right for callers
    that need the whole set, but wrong for a gate: one malformed contract would
    hide every other finding and the reviewer would fix them one run at a time.
    """
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        return [], [f"no tool contracts found in {directory}"]

    contracts: list[ToolboxContract] = []
    findings: list[str] = []
    for path in paths:
        try:
            contracts.append(load_contract(path))
        except ContractError as error:
            findings.append(str(error))
    return contracts, findings


def validate_contracts(directory: Path) -> list[str]:
    """Validate every contract and return human-readable findings.

    Returns findings rather than raising so that the CLI can report all of them
    at once. A reviewer fixing contracts one exception at a time is a reviewer
    who runs the gate five times.
    """
    return validate_loaded_contracts(*load_contracts_reporting(directory))


def validate_loaded_contracts(
    contracts: list[ToolboxContract], findings: list[str] | None = None
) -> list[str]:
    """Apply the cross-contract invariants that a single file cannot check."""
    findings = list(findings or [])

    seen_tools: dict[str, str] = {}
    for contract in contracts:
        for tool in contract.tools:
            if tool.name in seen_tools:
                findings.append(
                    f"{contract.source_path.name}: tool {tool.name!r} is also declared in {seen_tools[tool.name]}"
                )
            seen_tools[tool.name] = contract.source_path.name
            if not tool.name.startswith(f"{contract.capability}_"):
                # Names are globally unique across capabilities because a
                # published toolbox is a flat namespace, and a collision there
                # is resolved by whichever tool registered last.
                findings.append(
                    f"{contract.source_path.name}: tool {tool.name!r} should be prefixed "
                    f"with its capability {contract.capability!r}"
                )

    return findings