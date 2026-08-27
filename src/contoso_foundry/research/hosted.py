"""Microsoft Foundry Responses-protocol entry point for Contoso Research."""

from __future__ import annotations

import atexit
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from azure.ai.agentserver.responses import CreateResponse, ResponseContext
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_azure_ai.agents.hosting import ResponsesHostServer
from langchain_azure_ai.callbacks.tracers import AzureAIOpenTelemetryTracer
from langchain_openai import ChatOpenAI

from .request_context import resolve_trusted_request, trusted_user_routes_from_environment
from .runtime import runtime_from_environment
from .synthesis import model_synthesizer
from .workflow import AGENT_NAME, HOSTED_VERSION, build_research_graph

_AZURE_AI_SCOPE = "https://ai.azure.com/.default"


class ResearchResponsesHostServer(ResponsesHostServer):
    """Bind platform identity to graph state before any research node runs."""

    def __init__(
        self,
        graph,
        *,
        trusted_user_routes: Mapping[str, str],
        **kwargs,
    ) -> None:
        super().__init__(graph, **kwargs)
        self._trusted_user_routes = trusted_user_routes

    async def build_input(
        self,
        request: CreateResponse,
        context: ResponseContext,
        *,
        skip_call_ids: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        graph_input = await super().build_input(
            request,
            context,
            skip_call_ids=skip_call_ids,
        )
        trusted = resolve_trusted_request(
            context.platform_context,
            self._trusted_user_routes,
        )
        return {
            **graph_input,
            "caller_route": trusted.caller_route,
            "call_id": trusted.call_id,
        }


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _build_model(project_endpoint: str, deployment: str, credential: DefaultAzureCredential) -> ChatOpenAI:
    project = AIProjectClient(endpoint=project_endpoint, credential=credential)
    client = project.get_openai_client()
    token_provider = get_bearer_token_provider(credential, _AZURE_AI_SCOPE)
    return ChatOpenAI(
        model=deployment,
        base_url=str(client.base_url),
        api_key=token_provider,
        temperature=0,
    )


def _verify_hosted_route() -> None:
    actual_name = _required_environment("FOUNDRY_AGENT_NAME")
    if actual_name != AGENT_NAME:
        raise RuntimeError(f"FOUNDRY_AGENT_NAME must be {AGENT_NAME!r}")

    expected_version = _required_environment("CONTOSO_RESEARCH_HOSTED_VERSION")
    if expected_version != HOSTED_VERSION:
        raise RuntimeError(f"CONTOSO_RESEARCH_HOSTED_VERSION must be {HOSTED_VERSION!r}")

    actual_version = _required_environment("FOUNDRY_AGENT_VERSION")
    if actual_version != expected_version:
        raise RuntimeError(
            "hosted runtime version does not match the exact endpoint route: "
            f"expected {expected_version!r}, received {actual_version!r}"
        )


def main() -> None:
    os.environ.setdefault("OTEL_SERVICE_NAME", AGENT_NAME)
    if os.environ["OTEL_SERVICE_NAME"] != AGENT_NAME:
        raise RuntimeError(f"OTEL_SERVICE_NAME must be {AGENT_NAME!r}")

    _verify_hosted_route()
    endpoint = _required_environment("FOUNDRY_PROJECT_ENDPOINT")
    deployment = _required_environment("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    repo_root = Path(__file__).resolve().parents[3]
    runtime = runtime_from_environment(repo_root)
    atexit.register(runtime.close)

    credential = DefaultAzureCredential()
    model = _build_model(endpoint, deployment, credential)
    tracer = AzureAIOpenTelemetryTracer(
        project_endpoint=endpoint,
        credential=credential,
        name=AGENT_NAME,
        agent_id=AGENT_NAME,
        trace_all_langgraph_nodes=True,
        enable_content_recording=False,
    )
    graph = build_research_graph(runtime, model_synthesizer(model.invoke)).with_config(
        {"callbacks": [tracer], "run_name": AGENT_NAME}
    )
    ResearchResponsesHostServer(
        graph,
        trusted_user_routes=trusted_user_routes_from_environment(),
    ).run(port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
