from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agents" / "travel"
sys.path.insert(0, str(AGENT_ROOT / "src"))

from contoso_travel_agent.evaluation import (  # noqa: E402
    EvaluationError,
    collect_candidate_samples,
    load_golden,
    run_openai_eval,
)
from contoso_travel_agent.operations import OperationsError, _write_internal  # noqa: E402
from contoso_travel_agent.runtime import ExecutedToolCall  # noqa: E402


def test_collection_routes_every_case_to_the_exact_candidate():
    calls = []
    rows = load_golden(AGENT_ROOT / "golden" / "travel.jsonl")
    expected_by_prompt = {
        row["prompt"]: [
            ExecutedToolCall.from_arguments(tool["name"], tool["arguments"])
            for tool in row["expected_tools"]
        ]
        for row in rows
    }

    def run_case(prompt, name, version):
        calls.append((prompt, name, version))
        return "synthetic result", expected_by_prompt[prompt]

    samples = collect_candidate_samples(
        rows,
        agent_name="contoso-travel",
        agent_version="7",
        run_case=run_case,
    )
    assert len(samples) == len(rows)
    assert {(name, version) for _, name, version in calls} == {("contoso-travel", "7")}
    assert all(sample.eval_item()["tool_result"] == "PASS" for sample in samples)


def test_collection_has_no_first_agent_or_fallback_path():
    with pytest.raises(EvaluationError, match="exact"):
        collect_candidate_samples([], agent_name="contoso-travel", agent_version="", run_case=lambda *_: ("", []))


@pytest.mark.parametrize(
    "actual_calls",
    [
        [ExecutedToolCall.from_arguments("travel_search_routes", {"origin_location_id": "LOC-999"})],
        [
            ExecutedToolCall.from_arguments("travel_get_policy", {}),
            ExecutedToolCall.from_arguments(
                "travel_search_routes",
                {"origin_location_id": "LOC-001", "destination_location_id": "LOC-002"},
            ),
        ],
    ],
)
def test_tool_correctness_requires_exact_arguments_and_sequence(actual_calls):
    row = load_golden(AGENT_ROOT / "golden" / "travel.jsonl")[0]
    sample = collect_candidate_samples(
        [row],
        agent_name="contoso-travel",
        agent_version="7",
        run_case=lambda *_: ("synthetic result", actual_calls),
    )[0]
    assert sample.eval_item()["tool_result"] == "FAIL"


class FakeRuns:
    def __init__(self, *, counts=None, status="completed"):
        self.retrieved = 0
        self.counts = counts or {"passed": 3, "failed": 0, "errored": 0}
        self.status = status
        self.output_items = SimpleNamespace(
            list=lambda run_id, *, eval_id: [
                SimpleNamespace(
                    model_dump=lambda index=index, **_: {
                        "id": f"item-{index}",
                        "run_id": run_id,
                        "eval_id": eval_id,
                    }
                )
                for index in range(3)
            ]
        )

    def create(self, eval_id, **kwargs):
        assert eval_id == "eval-1"
        assert kwargs["data_source"]["type"] == "jsonl"
        return SimpleNamespace(id="run-1", status="queued")

    def retrieve(self, run_id, *, eval_id):
        self.retrieved += 1
        return SimpleNamespace(
            id=run_id,
            status=self.status,
            result_counts=SimpleNamespace(model_dump=lambda: self.counts),
            report_url="https://example.invalid/report",
            error=SimpleNamespace(code="evaluation_failed") if self.status == "failed" else None,
        )


def test_real_eval_polls_to_terminal_and_has_four_criteria():
    runs = FakeRuns()
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="eval-1")

    client = SimpleNamespace(evals=SimpleNamespace(create=create, runs=runs))
    rows = load_golden(AGENT_ROOT / "golden" / "travel.jsonl")
    samples = collect_candidate_samples(
        rows,
        agent_name="contoso-travel",
        agent_version="7",
        run_case=lambda *_: (
            "synthetic answer",
            [ExecutedToolCall.from_arguments("travel_get_policy", {})],
        ),
    )
    result = run_openai_eval(client, samples, judge_model="travel-gpt-5-4-mini", poll_seconds=0)
    assert result["status"] == "completed"
    assert len(result["output_items"]) == len(samples)
    assert runs.retrieved == 1
    assert {criterion["name"] for criterion in captured["testing_criteria"]} == {
        "quality",
        "safety",
        "task_correctness",
        "tool_correctness",
    }


def test_failed_eval_retains_per_sample_evidence():
    runs = FakeRuns(counts={"passed": 2, "failed": 1, "errored": 0})
    client = SimpleNamespace(
        evals=SimpleNamespace(
            create=lambda **_: SimpleNamespace(id="eval-1"),
            runs=runs,
        )
    )
    rows = load_golden(AGENT_ROOT / "golden" / "travel.jsonl")
    expected_by_prompt = {
        row["prompt"]: [
            ExecutedToolCall.from_arguments(tool["name"], tool["arguments"])
            for tool in row["expected_tools"]
        ]
        for row in rows
    }
    samples = collect_candidate_samples(
        rows,
        agent_name="contoso-travel",
        agent_version="7",
        run_case=lambda prompt, *_: ("synthetic answer", expected_by_prompt[prompt]),
    )
    with pytest.raises(EvaluationError, match="did not pass") as raised:
        run_openai_eval(client, samples, judge_model="travel-gpt-5-4-mini", poll_seconds=0)
    assert len(raised.value.evidence["output_items"]) == len(samples)


def test_terminal_eval_failure_retains_run_evidence():
    runs = FakeRuns(status="failed")
    client = SimpleNamespace(
        evals=SimpleNamespace(
            create=lambda **_: SimpleNamespace(id="eval-1"),
            runs=runs,
        )
    )
    sample = collect_candidate_samples(
        [load_golden(AGENT_ROOT / "golden" / "travel.jsonl")[1]],
        agent_name="contoso-travel",
        agent_version="7",
        run_case=lambda *_: (
            "synthetic answer",
            [ExecutedToolCall.from_arguments("travel_get_policy", {})],
        ),
    )
    with pytest.raises(EvaluationError, match="ended in failed") as raised:
        run_openai_eval(client, sample, judge_model="travel-gpt-5-4-mini", poll_seconds=0)
    assert raised.value.evidence["run_id"] == "run-1"
    assert raised.value.evidence["status"] == "failed"
    assert raised.value.evidence["output_items"] == []


def test_live_evidence_can_only_be_written_under_internal(tmp_path):
    with pytest.raises(OperationsError, match="internal"):
        _write_internal(tmp_path, Path("docs/result.json"), {"status": "passed"})


def test_azd_trace_discovery_uses_current_contract():
    config = REPO_ROOT / "evals" / "azure.eval.yaml"
    assert config.exists()
    text = config.read_text(encoding="utf-8")
    assert "type: traces" in text
    assert "agent_name: contoso-travel" in text
    assert not (REPO_ROOT / "eval.yaml").exists()
