"""Deterministic smoke and golden evaluations for the research graph."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import HumanMessage

from .runtime import build_runtime
from .synthesis import deterministic_synthesizer
from .workflow import AGENT_VERSION, build_research_graph


class EvaluationError(RuntimeError):
    """Raised when a required golden condition fails."""


def run_evaluations(repo_root: Path, scenarios_path: Path) -> list[str]:
    document = yaml.safe_load(scenarios_path.read_text(encoding="utf-8"))
    if document.get("agent_version") != AGENT_VERSION:
        raise EvaluationError(
            f"evaluation routes version {document.get('agent_version')!r}; expected {AGENT_VERSION!r}"
        )

    route = str(document.get("persona_route", ""))
    runtime = build_runtime(repo_root, persona_route=route, expected_version=AGENT_VERSION)
    graph = build_research_graph(runtime.toolbox, deterministic_synthesizer)
    results: list[str] = []
    try:
        for scenario in document.get("scenarios", []):
            output: dict[str, Any] = graph.invoke(
                {"messages": [HumanMessage(content=str(scenario["question"]))]}
            )
            actual_tools = [step["tool"] for step in output["plan"]]
            expected_tools = list(scenario["expected_tools"])
            if actual_tools != expected_tools:
                raise EvaluationError(
                    f"{scenario['id']}: expected tools {expected_tools!r}, received {actual_tools!r}"
                )

            row_counts = [
                len(item["result"]) if isinstance(item["result"], list) else (0 if item["result"] is None else 1)
                for item in output["evidence"]
            ]
            if row_counts != list(scenario["expected_rows"]):
                raise EvaluationError(
                    f"{scenario['id']}: expected row counts {scenario['expected_rows']!r}, received {row_counts!r}"
                )
            for text in scenario.get("answer_contains", []):
                if text not in output["answer"]:
                    raise EvaluationError(f"{scenario['id']}: answer omitted required text {text!r}")
            results.append(f"{scenario['id']}: passed")
    finally:
        runtime.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--scenarios", type=Path, default=Path("config/research-evals.yaml"))
    args = parser.parse_args()
    for line in run_evaluations(args.repo_root.resolve(), args.scenarios.resolve()):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
