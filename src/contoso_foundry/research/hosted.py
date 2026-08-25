"""Microsoft Foundry Responses-protocol entry point for Contoso Research."""

from __future__ import annotations

import atexit
import os
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_azure_ai.agents.hosting import ResponsesHostServer
from langchain_azure_ai.callbacks.tracers import AzureAIOpenTelemetryTracer
from langchain_openai import ChatOpenAI

from .runtime import runtime_from_environment
from .synthesis import model_synthesizer
from .workflow import AGENT_NAME, HOSTED_VERSION, build_research_graph

_AZURE_AI_SCOPE = "https://ai.azure.com/.default"


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
    graph = build_research_graph(runtime.toolbox, model_synthesizer(model.invoke)).with_config(
        {"callbacks": [tracer], "run_name": AGENT_NAME}
    )
    ResponsesHostServer(graph).run(port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()
