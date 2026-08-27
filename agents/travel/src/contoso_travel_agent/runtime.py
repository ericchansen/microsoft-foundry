"""Responses API loop bound to one synthetic, server-resolved Toolbox principal."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contoso_foundry.toolbox.contracts import FORBIDDEN_PARAMETERS
from contoso_foundry.toolbox.identity import Principal
from contoso_foundry.toolbox.tools import Toolbox
from contoso_travel_agent.definition import AgentSpec

_SYNTHETIC_PRINCIPAL = Principal(oid="OID-EMEA-TRAVEL-01", tid="TID-CONTOSO-01")


class AgentRuntimeError(RuntimeError):
    """Raised when a model response cannot be completed and verified."""


@dataclass(frozen=True)
class ExecutedToolCall:
    """One validated tool invocation in model-request order."""

    name: str
    arguments_json: str

    @classmethod
    def from_arguments(cls, name: str, arguments: dict[str, Any]) -> ExecutedToolCall:
        return cls(
            name=name,
            arguments_json=json.dumps(arguments, sort_keys=True, separators=(",", ":")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": json.loads(self.arguments_json)}


class TravelAgentRuntime:
    """Execute one travel turn without accepting identity or scope from the caller."""

    def __init__(
        self,
        openai_client: Any,
        spec: AgentSpec,
        connection: sqlite3.Connection,
        *,
        contracts_dir: Path,
        max_tool_rounds: int = 8,
        tracer: Any | None = None,
    ) -> None:
        self._client = openai_client
        self._spec = spec
        self._toolbox = Toolbox(
            connection,
            _SYNTHETIC_PRINCIPAL,
            contracts_dir=contracts_dir,
        )
        self._max_tool_rounds = max_tool_rounds
        self._tracer = tracer
        self._executed_calls: list[ExecutedToolCall] = []

    @property
    def audit(self) -> list[Any]:
        return self._toolbox.audit

    @property
    def executed_calls(self) -> list[ExecutedToolCall]:
        return list(self._executed_calls)

    def run_turn(self, prompt: str, *, agent_version: str) -> str:
        if not prompt.strip():
            raise AgentRuntimeError("prompt must not be empty")
        if not agent_version.strip():
            raise AgentRuntimeError("an exact agent version is required")

        response = self._client.responses.create(
            input=prompt,
            extra_body={
                "agent_reference": {
                    "name": self._spec.name,
                    "type": "agent_reference",
                    "version": agent_version,
                }
            },
        )
        for round_index in range(self._max_tool_rounds + 1):
            calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
            if not calls:
                text = str(getattr(response, "output_text", "")).strip()
                if not text:
                    raise AgentRuntimeError("the agent returned neither text nor tool calls")
                return text
            if round_index == self._max_tool_rounds:
                break

            outputs = []
            for call in calls:
                try:
                    arguments = json.loads(call.arguments)
                except (TypeError, json.JSONDecodeError) as error:
                    raise AgentRuntimeError(f"{call.name}: malformed function arguments") from error
                if not isinstance(arguments, dict):
                    raise AgentRuntimeError(f"{call.name}: function arguments must be an object")
                forbidden = sorted(set(arguments) & FORBIDDEN_PARAMETERS)
                if forbidden:
                    raise AgentRuntimeError(f"{call.name}: scope argument refused: {', '.join(forbidden)}")
                validated_arguments = {name: value for name, value in arguments.items() if value is not None}
                if self._tracer is None:
                    result = self._toolbox.call(call.name, arguments)
                else:
                    with self._tracer.start_as_current_span("contoso.travel.tool") as span:
                        span.set_attribute("contoso.synthetic", True)
                        span.set_attribute("gen_ai.tool.name", call.name)
                        result = self._toolbox.call(call.name, arguments)
                self._executed_calls.append(
                    ExecutedToolCall.from_arguments(call.name, validated_arguments)
                )
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(result, sort_keys=True, separators=(",", ":")),
                    }
                )

            response = self._client.responses.create(
                input=outputs,
                previous_response_id=response.id,
                extra_body={
                    "agent_reference": {
                        "name": self._spec.name,
                        "type": "agent_reference",
                        "version": agent_version,
                    }
                },
            )
        raise AgentRuntimeError(f"tool loop exceeded {self._max_tool_rounds} rounds")
