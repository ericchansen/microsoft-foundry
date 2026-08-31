"""Exact-candidate golden evaluation with a terminal-state OpenAI eval run."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contoso_travel_agent.runtime import ExecutedToolCall


class EvaluationError(RuntimeError):
    """Raised when evaluation cannot produce a terminal, passing result."""

    def __init__(self, message: str, *, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence


@dataclass(frozen=True)
class CandidateSample:
    case_id: str
    output: str
    tool_calls: tuple[ExecutedToolCall, ...]
    must_include: tuple[str, ...]
    must_not_include: tuple[str, ...]
    expected_tool_calls: tuple[ExecutedToolCall, ...]
    server_executed: bool = False

    def eval_item(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "output": self.output,
            "tool_calls": json.dumps([call.as_dict() for call in self.tool_calls]),
            "expected_tool_calls": json.dumps(
                [call.as_dict() for call in self.expected_tool_calls]
            ),
            "must_include": json.dumps(self.must_include),
            "must_not_include": json.dumps(self.must_not_include),
            "safety_result": (
                "PASS"
                if not any(value.casefold() in self.output.casefold() for value in self.must_not_include)
                else "FAIL"
            ),
            "task_result": (
                "PASS"
                if all(value.casefold() in self.output.casefold() for value in self.must_include)
                else "FAIL"
            ),
            "tool_result": (
                "PASS"
                if self.tool_calls == self.expected_tool_calls
                else "FAIL"
            ),
        }


def load_golden(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise EvaluationError("the golden dataset is empty")
    return rows


def collect_candidate_samples(
    golden: list[dict[str, Any]],
    *,
    agent_name: str,
    agent_version: str,
    run_case: Callable[[str, str, str], tuple[str, list[ExecutedToolCall]]],
    server_executed: bool = False,
) -> list[CandidateSample]:
    if not agent_name.strip() or not agent_version.strip():
        raise EvaluationError("evaluation requires an exact agent name and version")
    samples = []
    for row in golden:
        output, tool_calls = run_case(str(row["prompt"]), agent_name, agent_version)
        if not tool_calls and not server_executed:
            raise EvaluationError(f"{row['case_id']}: candidate called no tools")
        expected_tool_calls = tuple(
            ExecutedToolCall.from_arguments(str(tool["name"]), dict(tool["arguments"]))
            for tool in row["expected_tools"]
        )
        samples.append(
            CandidateSample(
                case_id=str(row["case_id"]),
                output=output,
                tool_calls=tuple(tool_calls),
                must_include=tuple(str(value) for value in row.get("must_include", [])),
                must_not_include=tuple(str(value) for value in row.get("must_not_include", [])),
                expected_tool_calls=expected_tool_calls,
                server_executed=server_executed,
            )
        )
    return samples


def _criterion(name: str, model: str, instruction: str) -> dict[str, Any]:
    return {
        "type": "label_model",
        "name": name,
        "model": model,
        "input": [
            {
                "role": "developer",
                "content": (
                    f"{instruction} Judge the answer text only; separate criteria validate "
                    "tool execution and deterministic constraints. Return exactly PASS or FAIL."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Output: {{item.output}}\nRequired: {{item.must_include}}\n"
                    "Forbidden: {{item.must_not_include}}"
                ),
            },
        ],
        "labels": ["PASS", "FAIL"],
        "passing_labels": ["PASS"],
    }


def _serialize_output_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    raise EvaluationError("eval output item could not be serialized")


def run_openai_eval(
    openai_client: Any,
    samples: list[CandidateSample],
    *,
    judge_model: str,
    poll_seconds: float = 5.0,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    if not samples:
        raise EvaluationError("refusing to create an eval with no candidate samples")
    evaluation = openai_client.evals.create(
        name="contoso-travel-exact-candidate",
        data_source_config={
            "type": "custom",
            "item_schema": {
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "output": {"type": "string"},
                    "tool_calls": {"type": "string"},
                    "expected_tool_calls": {"type": "string"},
                    "must_include": {"type": "string"},
                    "must_not_include": {"type": "string"},
                    "safety_result": {"type": "string"},
                    "task_result": {"type": "string"},
                    "tool_result": {"type": "string"},
                },
                "required": [
                    "case_id",
                    "output",
                    "tool_calls",
                    "expected_tool_calls",
                    "must_include",
                    "must_not_include",
                    "safety_result",
                    "task_result",
                    "tool_result",
                ],
            },
        },
        testing_criteria=[
            _criterion("quality", judge_model, "Judge whether the answer is clear, grounded, and useful."),
            {
                "type": "string_check",
                "name": "safety",
                "input": "{{item.safety_result}}",
                "operation": "eq",
                "reference": "PASS",
            },
            {
                "type": "string_check",
                "name": "task_correctness",
                "input": "{{item.task_result}}",
                "operation": "eq",
                "reference": "PASS",
            },
            {
                "type": "string_check",
                "name": "tool_correctness",
                "input": "{{item.tool_result}}",
                "operation": "eq",
                "reference": "PASS",
            },
        ],
    )
    run = openai_client.evals.runs.create(
        evaluation.id,
        name="contoso-travel-golden",
        data_source={
            "type": "jsonl",
            "source": {
                "type": "file_content",
                "content": [{"item": sample.eval_item()} for sample in samples],
            },
        },
    )
    deadline = time.monotonic() + timeout_seconds
    while run.status not in {"completed", "failed", "canceled"}:
        if time.monotonic() >= deadline:
            raise EvaluationError(
                f"eval run {run.id} did not reach a terminal state",
                evidence={
                    "eval_id": evaluation.id,
                    "run_id": run.id,
                    "status": run.status,
                    "error": "terminal-state timeout",
                    "output_items": [],
                },
            )
        time.sleep(poll_seconds)
        run = openai_client.evals.runs.retrieve(run.id, eval_id=evaluation.id)
    if run.status != "completed":
        error = str(getattr(run, "error", "") or "")
        raise EvaluationError(
            f"eval run ended in {run.status}: {error or None}",
            evidence={
                "eval_id": evaluation.id,
                "run_id": run.id,
                "status": run.status,
                "error": error,
                "output_items": [],
            },
        )
    counts = run.result_counts.model_dump() if hasattr(run.result_counts, "model_dump") else dict(run.result_counts)
    output_items = [
        _serialize_output_item(item)
        for item in openai_client.evals.runs.output_items.list(run.id, eval_id=evaluation.id)
    ]
    result = {
        "eval_id": evaluation.id,
        "run_id": run.id,
        "status": run.status,
        "result_counts": counts,
        "report_url_present": bool(getattr(run, "report_url", "")),
        "output_items": output_items,
    }
    if len(output_items) != len(samples):
        raise EvaluationError(
            f"eval run returned {len(output_items)} output items for {len(samples)} samples",
            evidence=result,
        )
    if counts.get("errored", 0) or counts.get("failed", 0):
        raise EvaluationError(f"eval run did not pass: {counts}", evidence=result)
    return result
