"""Transparent planner -> retrieval -> synthesis LangGraph workflow."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Annotated, Any, Protocol, TypedDict

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from contoso_foundry.research.planner import ResearchPlanError, plan_question
from contoso_foundry.research.request_context import TrustedRequestContext
from contoso_foundry.research.synthesis import Synthesizer
from contoso_foundry.toolbox.tools import Toolbox

AGENT_NAME = "contoso-research"
AGENT_VERSION = "1.0.0"
HOSTED_VERSION = "4"
PROTOCOL_VERSION = "2.0.0"
REQUIRED_CONTRACT_VERSIONS = {
    "catalog": "1.0.0",
    "customer": "1.0.0",
    "operations": "1.0.0",
    "orders": "1.0.0",
    "hr": "1.0.0",
    "support": "1.0.0",
    "travel": "1.0.0",
}


class ResearchState(TypedDict, total=False):
    """Serializable state intentionally retained between every graph node."""

    messages: Annotated[list[AnyMessage], add_messages]
    question: str
    plan: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    answer: str
    audit: list[dict[str, Any]]
    caller_route: str
    call_id: str


class RequestScopedRuntime(Protocol):
    contract_versions: Mapping[str, str]

    def open_toolbox(
        self,
        request: TrustedRequestContext,
    ) -> AbstractContextManager[Toolbox]: ...


def _question_from_state(state: ResearchState) -> str:
    question = str(state.get("question", "")).strip()
    if question:
        return question
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", "") == "human":
            content = getattr(message, "content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()
    raise ResearchPlanError("the graph received no user question")


def _verify_contract_versions(runtime: RequestScopedRuntime) -> None:
    actual = dict(runtime.contract_versions)
    if actual != REQUIRED_CONTRACT_VERSIONS:
        raise RuntimeError(
            f"Toolbox contract versions do not match the agent route: expected "
            f"{REQUIRED_CONTRACT_VERSIONS!r}, received {actual!r}"
        )


def build_research_graph(runtime: RequestScopedRuntime, synthesizer: Synthesizer) -> Any:
    """Compile a graph that opens one immutable Toolbox per invocation."""
    _verify_contract_versions(runtime)

    def planner(state: ResearchState) -> ResearchState:
        question = _question_from_state(state)
        return {"question": question, "plan": plan_question(question)}

    def retrieve(state: ResearchState) -> ResearchState:
        request = TrustedRequestContext(
            caller_route=state["caller_route"],
            call_id=state["call_id"],
        )
        evidence = []
        with runtime.open_toolbox(request) as toolbox:
            for step in state["plan"]:
                tool = str(step["tool"])
                arguments = dict(step["arguments"])
                evidence.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "result": toolbox.call(tool, arguments),
                    }
                )
            audit = [
                {
                    "tool": call.tool,
                    "persona": call.persona,
                    "argument_names": list(call.argument_names),
                    "result_rows": call.result_rows,
                }
                for call in toolbox.audit
            ]
        return {"evidence": evidence, "audit": audit}

    def synthesize(state: ResearchState) -> ResearchState:
        answer = synthesizer(state["question"], state["evidence"], state["call_id"])
        return {"answer": answer, "messages": [AIMessage(content=answer)]}

    graph = StateGraph(ResearchState)
    graph.add_node("planner", planner, metadata={"otel_trace": True})
    graph.add_node("retrieval", retrieve, metadata={"otel_trace": True})
    graph.add_node("synthesis", synthesize, metadata={"otel_trace": True})
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "retrieval")
    graph.add_edge("retrieval", "synthesis")
    graph.add_edge("synthesis", END)
    return graph.compile()
