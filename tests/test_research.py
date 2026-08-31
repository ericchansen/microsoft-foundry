"""Research graph, hosted configuration, and golden evaluation contracts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from langchain_azure_ai.agents.hosting import ResponsesHostServer
from langchain_core.messages import HumanMessage

from contoso_foundry import azure_cli
from contoso_foundry.research.deployment import (
    DeploymentBoundaryError,
    require_clean_live_boundary,
    target_from_boundary,
    verify_arm_target,
    verify_deployment_target,
)
from contoso_foundry.research.evaluate import EvaluationError, run_evaluations
from contoso_foundry.research.planner import ResearchPlanError, plan_question
from contoso_foundry.research.request_context import (
    EVALUATION_USER_ROUTES,
    HostedIdentityError,
    load_trusted_user_routes,
    resolve_fixed_request,
    resolve_trusted_request,
)
from contoso_foundry.research.runtime import build_runtime, runtime_from_environment
from contoso_foundry.research.synthesis import (
    FOUNDRY_CALL_ID_HEADER,
    SynthesisError,
    deterministic_synthesizer,
    model_synthesizer,
)
from contoso_foundry.research.workflow import (
    AGENT_NAME,
    AGENT_VERSION,
    HOSTED_VERSION,
    REQUIRED_CONTRACT_VERSIONS,
    build_research_graph,
)


@pytest.fixture
def runtime(repo_root: Path):
    value = build_runtime(repo_root, expected_version=AGENT_VERSION)
    try:
        yield value
    finally:
        value.close()


def test_graph_exposes_planner_retrieval_synthesis_state(runtime) -> None:
    graph = build_research_graph(runtime, deterministic_synthesizer)
    trusted = resolve_trusted_request(
        SimpleNamespace(
            user_id_key="contoso-user-americas-supply-planner",
            call_id="call-transparent-state",
        ),
        EVALUATION_USER_ROUTES,
    )
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="Summarize overdue invoices visible to me.")],
            "caller_route": trusted.caller_route,
            "call_id": trusted.call_id,
        }
    )

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
    assert result["caller_route"] == "americas-supply-planner"
    assert result["call_id"] == "call-transparent-state"


def test_planner_uses_exact_canonical_ids() -> None:
    assert plan_question("Research invoice inv-000007") == [
        {"tool": "orders_lookup_invoice", "arguments": {"invoice_id": "INV-000007"}}
    ]
    assert plan_question("Research product PROD-0001") == [
        {"tool": "catalog_lookup_product", "arguments": {"product_id": "PROD-0001"}},
        {"tool": "catalog_check_stock", "arguments": {"product_id": "PROD-0001", "limit": 50}},
    ]


def test_planner_supports_general_catalog_questions() -> None:
    assert plan_question("Research current products in the catalogue") == [
        {"tool": "catalog_search_products", "arguments": {"is_discontinued": 0, "limit": 50}}
    ]


def test_planner_normalizes_customer_segments_to_contract_values() -> None:
    assert plan_question("Research public sector customers") == [
        {"tool": "customer_search", "arguments": {"segment": "public_sector", "limit": 50}}
    ]


def test_planner_refuses_unsupported_domains() -> None:
    with pytest.raises(ResearchPlanError, match="outside the supported"):
        plan_question("Tell me a joke.")


def test_runtime_fails_closed_on_wrong_version(repo_root: Path) -> None:
    with pytest.raises(RuntimeError, match="requires version"):
        build_runtime(repo_root, expected_version="latest")


def test_environment_requires_explicit_version(repo_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("CONTOSO_RESEARCH_VERSION", raising=False)
    with pytest.raises(RuntimeError, match="is required"):
        runtime_from_environment(repo_root)


def test_graph_refuses_contract_version_drift(runtime) -> None:
    runtime.contract_versions["catalog"] = "2.0.0"
    with pytest.raises(RuntimeError, match="do not match the agent route"):
        build_research_graph(runtime, deterministic_synthesizer)


def test_model_synthesizer_fails_on_empty_response() -> None:
    for response in ("", None, SimpleNamespace(content=None)):
        synthesize = model_synthesizer(lambda _prompt, _response=response, **_kwargs: _response)
        with pytest.raises(SynthesisError, match="empty synthesis"):
            synthesize("question", [], "call-empty")


def test_model_synthesizer_forwards_only_opaque_foundry_call_id() -> None:
    captured: dict[str, object] = {}

    def invoke(prompt: str, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return "grounded"

    answer = model_synthesizer(invoke)("question", [], "opaque-call-value")

    assert answer == "grounded"
    assert captured["extra_headers"] == {FOUNDRY_CALL_ID_HEADER: "opaque-call-value"}
    assert "contoso-user" not in str(captured)


@pytest.mark.parametrize(
    ("user_id_key", "call_id", "message"),
    [
        (None, "call", "identity is required"),
        ("", "call", "identity is required"),
        ("contoso-user-americas-supply-planner", None, "call context is required"),
        ("contoso-user-americas-supply-planner", "", "call context is required"),
        ("forged-global-admin", "call", "not authorized"),
    ],
)
def test_request_context_fails_closed(user_id_key, call_id, message) -> None:
    with pytest.raises(HostedIdentityError, match=message):
        resolve_trusted_request(
            SimpleNamespace(user_id_key=user_id_key, call_id=call_id),
            EVALUATION_USER_ROUTES,
        )


def test_user_route_allow_list_is_validated_and_immutable() -> None:
    routes = load_trusted_user_routes(
        '{"opaque-platform-user":"americas-support-lead"}'
    )
    assert routes["opaque-platform-user"] == "americas-support-lead"
    with pytest.raises(TypeError):
        routes["other"] = "americas-supply-planner"
    with pytest.raises(HostedIdentityError, match="unknown persona"):
        load_trusted_user_routes('{"opaque-platform-user":"global-admin"}')


def test_host_ignores_forged_client_scope(monkeypatch) -> None:
    from contoso_foundry.research.hosted import ResearchResponsesHostServer

    async def forged_base_input(_self, _request, _context, *, skip_call_ids=None):
        del skip_call_ids
        return {
            "messages": [],
            "caller_route": "forged-global-admin",
            "call_id": "forged-client-call",
        }

    monkeypatch.setattr(ResponsesHostServer, "build_input", forged_base_input)
    server = object.__new__(ResearchResponsesHostServer)
    server._caller_route = "americas-supply-planner"
    context = SimpleNamespace(
        platform_context=SimpleNamespace(
            user_id_key="contoso-user-americas-support-lead",
            call_id="platform-call",
        )
    )

    graph_input = asyncio.run(server.build_input(object(), context))

    assert graph_input["caller_route"] == "americas-supply-planner"
    assert graph_input["call_id"] == "platform-call"


def test_fixed_host_route_requires_platform_identity_context() -> None:
    context = SimpleNamespace(user_id_key="opaque-user", call_id="platform-call")
    assert resolve_fixed_request(context, "americas-supply-planner").caller_route == (
        "americas-supply-planner"
    )
    with pytest.raises(HostedIdentityError, match="identity"):
        resolve_fixed_request(
            SimpleNamespace(user_id_key="", call_id="platform-call"),
            "americas-supply-planner",
        )


def test_concurrent_requests_keep_identity_results_and_audit_disjoint(runtime) -> None:
    graph = build_research_graph(runtime, deterministic_synthesizer)

    def input_for(user_id_key: str, call_id: str) -> dict:
        trusted = resolve_trusted_request(
            SimpleNamespace(user_id_key=user_id_key, call_id=call_id),
            EVALUATION_USER_ROUTES,
        )
        return {
            "messages": [HumanMessage(content="Research customers visible to me.")],
            "caller_route": trusted.caller_route,
            "call_id": trusted.call_id,
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        americas_future = pool.submit(
            graph.invoke,
            input_for("contoso-user-americas-support-lead", "call-americas"),
        )
        emea_future = pool.submit(
            graph.invoke,
            input_for("contoso-user-emea-travel-coordinator", "call-emea"),
        )
        americas = americas_future.result()
        emea = emea_future.result()

    americas_ids = {row["customer_id"] for row in americas["evidence"][0]["result"]}
    emea_ids = {row["customer_id"] for row in emea["evidence"][0]["result"]}
    assert americas_ids
    assert emea_ids
    assert americas_ids.isdisjoint(emea_ids)
    assert [item["persona"] for item in americas["audit"]] == [
        "Americas Support Lead"
    ]
    assert [item["persona"] for item in emea["audit"]] == [
        "EMEA Travel Coordinator"
    ]


def test_sequential_requests_do_not_accumulate_audit(runtime) -> None:
    graph = build_research_graph(runtime, deterministic_synthesizer)
    trusted = resolve_trusted_request(
        SimpleNamespace(
            user_id_key="contoso-user-americas-supply-planner",
            call_id="call-first",
        ),
        EVALUATION_USER_ROUTES,
    )
    base = {
        "messages": [HumanMessage(content="Research current stock availability.")],
        "caller_route": trusted.caller_route,
    }

    first = graph.invoke({**base, "call_id": "call-first"})
    second = graph.invoke({**base, "call_id": "call-second"})

    assert len(first["audit"]) == 1
    assert len(second["audit"]) == 1


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
    config = yaml.safe_load(unified)
    research = config["services"]["contoso-research"]
    project = config["services"]["research-project"]
    dockerfile = (repo_root / "agents" / "research" / "Dockerfile").read_text(encoding="utf-8")
    ci = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert research["protocols"] == [{"protocol": "responses", "version": "2.0.0"}]
    assert research["agentEndpoint"]["versionSelector"]["versionSelectionRules"] == [
        {"type": "FixedRatio", "agentVersion": "4", "trafficPercentage": 100}
    ]
    assert "@latest" not in unified
    assert project["deployments"][0]["name"] == "gpt-5.4-mini-2026-03-17"
    assert project["deployments"][0]["model"]["version"] == "2026-03-17"
    assert research["name"] == "contoso-research"
    assert config["resourceGroup"] == "rg-contoso-agents"
    assert "CONTOSO_RESEARCH_PERSONA_ROUTE" not in unified
    assert "hooks" not in config
    assert "contoso_foundry.research.deployment" in research["hooks"]["predeploy"]["windows"]["run"]
    assert research["hooks"]["predeploy"]["windows"]["continueOnError"] is False
    assert research["hooks"]["predeploy"]["posix"]["continueOnError"] is False
    assert "FROM python:3.13.15-slim-bookworm@sha256:" in dockerfile
    assert "ENV PYTHONPATH=/app/src" in dockerfile
    assert "HOME=/tmp" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "pip==26.2.1" in dockerfile
    assert 'CMD ["python", "-m", "contoso_foundry.research.hosted"]' in dockerfile
    assert "Verify hosted-agent readiness endpoint" in ci
    assert "http://127.0.0.1:8089/readiness" in ci
    assert {name: "1.0.0" for name in REQUIRED_CONTRACT_VERSIONS} == REQUIRED_CONTRACT_VERSIONS


def test_deployment_target_is_derived_from_relative_boundary(repo_root: Path) -> None:
    target = target_from_boundary(repo_root / "config" / "boundary.yaml")

    assert target.resource_group == "rg-contoso-agents"
    assert target.account_name == "contoso-agents-foundry"
    assert target.project_name == "research"
    assert target.endpoint.endswith("/api/projects/research")


def test_hosted_manifest_uses_a_prebuilt_digest_pinned_image(repo_root: Path) -> None:
    manifest = yaml.safe_load(
        (
            repo_root
            / "deployment"
            / "hosted"
            / "research"
            / "azure.yaml"
        ).read_text(encoding="utf-8")
    )
    research = manifest["services"]["contoso-research"]
    assert research["image"] == "${CONTOSO_RESEARCH_IMAGE}"
    assert research["docker"]["remoteBuild"] is True
    assert [
        name
        for name, service in manifest["services"].items()
        if service["host"] == "azure.ai.project"
    ] == ["research-project"]


def test_predeploy_requires_a_clean_live_boundary(repo_root: Path, monkeypatch) -> None:
    observed = {}

    def reject_boundary(report, _plan, *, enabled_modules, allow_missing_declared):
        observed["enabled_modules"] = enabled_modules
        observed["allow_missing_declared"] = allow_missing_declared
        report.fail("live:test", "resource", "undeclared live resource")
        return report

    monkeypatch.setattr("contoso_foundry.research.deployment.check_live", reject_boundary)
    with pytest.raises(DeploymentBoundaryError, match="live ownership boundary"):
        require_clean_live_boundary(
            repo_root / "config" / "boundary.yaml",
            enabled_modules={"optional-control-plane"},
            deployment_readiness=True,
        )
    assert observed["enabled_modules"] == {"optional-control-plane"}
    assert observed["allow_missing_declared"] is True


def test_deployment_rejects_endpoint_outside_declared_project(
    repo_root: Path,
    monkeypatch,
) -> None:
    def unexpected_azure_call(_args):
        raise AssertionError("endpoint mismatch must fail before ARM access")

    monkeypatch.setattr(azure_cli, "run", unexpected_azure_call)
    with pytest.raises(DeploymentBoundaryError, match="does not match"):
        verify_deployment_target(
            repo_root,
            configured_endpoint=(
                "https://outside-boundary.services.ai.azure.com/api/projects/research"
            ),
        )


def test_deployment_rejects_arm_resource_outside_owned_group(repo_root: Path) -> None:
    target = target_from_boundary(repo_root / "config" / "boundary.yaml")
    account = {
        "resourceGroup": target.resource_group,
        "type": "Microsoft.CognitiveServices/accounts",
        "id": (
            "/subscriptions/SUBSCRIPTION-ID/resourceGroups/"
            f"{target.resource_group}/providers/Microsoft.CognitiveServices/accounts/"
            f"{target.account_name}"
        ),
    }
    project = {
        "resourceGroup": "rg-outside-boundary",
        "type": "Microsoft.CognitiveServices/accounts/projects",
        "id": (
            "/subscriptions/SUBSCRIPTION-ID/resourceGroups/rg-outside-boundary/"
            "providers/Microsoft.CognitiveServices/accounts/outside/projects/research"
        ),
    }

    with pytest.raises(DeploymentBoundaryError, match="outside the owned"):
        verify_arm_target(target, account, project)


def test_deployment_resolves_exact_resources_before_accepting_endpoint(
    repo_root: Path,
    monkeypatch,
) -> None:
    target = target_from_boundary(repo_root / "config" / "boundary.yaml")
    account = {
        "resourceGroup": target.resource_group,
        "type": "Microsoft.CognitiveServices/accounts",
        "id": (
            "/subscriptions/SUBSCRIPTION-ID/resourceGroups/"
            f"{target.resource_group}/providers/Microsoft.CognitiveServices/accounts/"
            f"{target.account_name}"
        ),
    }
    project = {
        "resourceGroup": target.resource_group,
        "type": "Microsoft.CognitiveServices/accounts/projects",
        "id": (
            "/subscriptions/SUBSCRIPTION-ID/resourceGroups/"
            f"{target.resource_group}/providers/Microsoft.CognitiveServices/accounts/"
            f"{target.account_name}/projects/{target.project_name}"
        ),
    }
    calls: list[list[str]] = []

    def arm_show(args):
        calls.append(args)
        return project if args[args.index("--resource-type") + 1] == "projects" else account

    monkeypatch.setattr(azure_cli, "run", arm_show)

    actual = verify_deployment_target(
        repo_root,
        configured_endpoint=target.endpoint,
    )

    assert actual == target
    assert [call[call.index("--resource-group") + 1] for call in calls] == [
        target.resource_group,
        target.resource_group,
    ]


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
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", "999")
    with pytest.raises(RuntimeError, match="exact endpoint route"):
        _verify_hosted_route()


def test_hosted_runtime_accepts_exact_platform_route(monkeypatch) -> None:
    from contoso_foundry.research.hosted import _verify_hosted_route

    monkeypatch.setenv("FOUNDRY_AGENT_NAME", AGENT_NAME)
    monkeypatch.setenv("CONTOSO_RESEARCH_HOSTED_VERSION", HOSTED_VERSION)
    monkeypatch.setenv("FOUNDRY_AGENT_VERSION", HOSTED_VERSION)
    _verify_hosted_route()
