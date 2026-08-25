"""Research graph, hosted configuration, and golden evaluation contracts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from contoso_foundry.research.evaluate import EvaluationError, run_evaluations
from contoso_foundry.research.planner import ResearchPlanError, plan_question
from contoso_foundry.research.runtime import build_runtime, runtime_from_environment
from contoso_foundry.research.synthesis import SynthesisError, deterministic_synthesizer, model_synthesizer
from contoso_foundry.research.workflow import (
    AGENT_NAME,
    AGENT_VERSION,
    HOSTED_VERSION,
    REQUIRED_CONTRACT_VERSIONS,
    build_research_graph,
)


@pytest.fixture
def runtime(repo_root: Path):
    value = build_runtime(
        repo_root,
        persona_route="americas-supply-planner",
        expected_version=AGENT_VERSION,
    )
    try:
        yield value
    finally:
        value.close()


def test_graph_exposes_planner_retrieval_synthesis_state(runtime) -> None:
    graph = build_research_graph(runtime.toolbox, deterministic_synthesizer)
    result = graph.invoke({"messages": [HumanMessage(content="Summarize overdue invoices visible to me.")]})

    assert result["question"] == "Summarize overdue invoices visible to me."
    assert result["plan"] == [
        {"tool": "orders_search_invoices", "arguments": {"status": "overdue", "limit": 50}}
    ]
    assert len(result["evidence"][0]["result"]) == 50
    assert result["audit"] == [
        {
            "tool": "orders_search_invoices",
            "persona": "Americas Supply Planner",
            "argument_names": ["limit", "status"],
            "result_rows": 50,
        }
    ]
    assert result["messages"][-1].content == result["answer"]


def test_planner_uses_exact_canonical_ids() -> None:
    assert plan_question("Research invoice inv-000007") == [
        {"tool": "orders_lookup_invoice", "arguments": {"invoice_id": "INV-000007"}}
    ]


def test_planner_refuses_unsupported_domains() -> None:
    with pytest.raises(ResearchPlanError, match="outside the supported"):
        plan_question("Tell me a joke.")


def test_runtime_fails_closed_on_unknown_route_or_version(repo_root: Path) -> None:
    with pytest.raises(RuntimeError, match="unknown research persona route"):
        build_runtime(repo_root, persona_route="global-admin", expected_version=AGENT_VERSION)
    with pytest.raises(RuntimeError, match="requires version"):
        build_runtime(repo_root, persona_route="americas-supply-planner", expected_version="latest")


def test_environment_requires_explicit_route_and_version(repo_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("CONTOSO_RESEARCH_PERSONA_ROUTE", raising=False)
    monkeypatch.delenv("CONTOSO_RESEARCH_VERSION", raising=False)
    with pytest.raises(RuntimeError, match="are required"):
        runtime_from_environment(repo_root)


def test_graph_refuses_contract_version_drift(runtime) -> None:
    runtime.toolbox._contracts[0] = replace(runtime.toolbox._contracts[0], version="2.0.0")
    with pytest.raises(RuntimeError, match="do not match the agent route"):
        build_research_graph(runtime.toolbox, deterministic_synthesizer)


def test_model_synthesizer_fails_on_empty_response() -> None:
    synthesize = model_synthesizer(lambda _: "")
    with pytest.raises(SynthesisError, match="empty synthesis"):
        synthesize("question", [])


def test_golden_scenarios(repo_root: Path) -> None:
    results = run_evaluations(repo_root, repo_root / "config" / "research-evals.yaml")
    assert results == [
        "overdue-receivables: passed",
        "outstanding-receivables: passed",
        "stock-availability: passed",
    ]


def test_evaluations_fail_on_wrong_agent_version(repo_root: Path, tmp_path: Path) -> None:
    scenarios = tmp_path / "wrong-version.yaml"
    scenarios.write_text("agent_version: 9.9.9\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="evaluation routes"):
        run_evaluations(repo_root, scenarios)


def test_hosted_configuration_routes_exact_versions(repo_root: Path) -> None:
    unified = (repo_root / "azure.yaml").read_text(encoding="utf-8")
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")

    assert "protocol: responses" in unified
    assert "version: 2.0.0" in unified
    assert "agentVersion: '1'" in unified
    assert "trafficPercentage: 100" in unified
    assert "@latest" not in unified
    assert "gpt-5.4-mini-2026-03-17" in unified
    assert "version: '2026-03-17'" in unified
    assert "contoso-research" in unified
    assert "resourceGroup: rg-contoso-agents" in unified
    assert "FROM python:3.13.7-slim" in dockerfile
    assert "pip==26.2.1" in dockerfile
    assert 'CMD ["python", "-m", "contoso_foundry.research.hosted"]' in dockerfile
    assert {name: "1.0.0" for name in REQUIRED_CONTRACT_VERSIONS} == REQUIRED_CONTRACT_VERSIONS


def test_hosted_runtime_uses_first_party_tracer_and_redacts_content(repo_root: Path) -> None:
    source = (repo_root / "src" / "contoso_foundry" / "research" / "hosted.py").read_text(encoding="utf-8")
    assert "AzureAIOpenTelemetryTracer" in source
    assert "agent_id=AGENT_NAME" in source
    assert "trace_all_langgraph_nodes=True" in source
    assert "enable_content_recording=False" in source
    assert 'os.environ.setdefault("OTEL_SERVICE_NAME", AGENT_NAME)' in source


def test_hosted_runtime_rejects_wrong_platform_route(monkeypatch) -> None:
    from contoso_foundry.research.hosted import _verify_hosted_route

    monkeypatch.setenv("FOUNDRY_AGENT_NAME", AGENT_NAME)
    monkeypatch.setenv("CONTOSO_RESEARCH_HOSTED_VERSION", HOSTED_VERSION)
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "2")
    with pytest.raises(RuntimeError, match="exact endpoint route"):
        _verify_hosted_route()


def test_hosted_runtime_accepts_exact_platform_route(monkeypatch) -> None:
    from contoso_foundry.research.hosted import _verify_hosted_route

    monkeypatch.setenv("FOUNDRY_AGENT_NAME", AGENT_NAME)
    monkeypatch.setenv("CONTOSO_RESEARCH_HOSTED_VERSION", HOSTED_VERSION)
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", HOSTED_VERSION)
    _verify_hosted_route()
