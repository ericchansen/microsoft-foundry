"""Evidence-grounded synthesis interfaces for Contoso Research."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol


class SynthesisError(RuntimeError):
    """Raised when synthesis fails or produces an empty answer."""


class Synthesizer(Protocol):
    def __call__(self, question: str, evidence: list[dict[str, Any]]) -> str: ...


def evidence_prompt(question: str, evidence: list[dict[str, Any]]) -> str:
    """Render bounded Toolbox evidence for a model without hidden context."""
    return (
        "Answer the Contoso research question using only the JSON evidence below. "
        "State the visible scope limitation, cite tool names inline, distinguish facts from inference, "
        "and say when evidence is empty. Never invent identifiers or totals.\n\n"
        f"Question:\n{question}\n\n"
        f"Evidence:\n{json.dumps(evidence, sort_keys=True, separators=(',', ':'))}"
    )


def model_synthesizer(invoke: Callable[[str], Any]) -> Synthesizer:
    """Adapt a LangChain chat model while keeping the graph easy to test."""

    def synthesize(question: str, evidence: list[dict[str, Any]]) -> str:
        response = invoke(evidence_prompt(question, evidence))
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        answer = str(content).strip()
        if not answer:
            raise SynthesisError("the model returned an empty synthesis")
        return answer

    return synthesize


def deterministic_synthesizer(question: str, evidence: list[dict[str, Any]]) -> str:
    """Stable test/evaluation synthesis; production never selects this path."""
    del question
    parts = []
    total = 0
    for item in evidence:
        result = item["result"]
        count = len(result) if isinstance(result, list) else (0 if result is None else 1)
        total += count
        parts.append(f"{item['tool']} returned {count} visible record(s)")
    return (
        f"Scoped Contoso evidence contains {total} record(s). "
        + "; ".join(parts)
        + ". This answer is limited to the server-resolved caller scope."
    )
