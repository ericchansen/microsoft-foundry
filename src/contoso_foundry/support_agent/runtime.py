"""Microsoft Agent Framework multi-agent workflow hosted on Responses 2.0."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from agent_framework import Agent, AgentExecutor, WorkflowBuilder, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import enable_instrumentation
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.core import get_request_context
from azure.identity import DefaultAzureCredential
from pydantic import Field

from contoso_foundry.support_agent.identity import RequestIdentityBinding
from contoso_foundry.support_agent.tools import CanonicalDataStore, ScopedToolSessionFactory, SupportToolDispatcher

PACKAGED_DATABASE = Path("/opt/contoso-support/contoso.db")
PACKAGED_DATABASE_SHA256 = Path("/opt/contoso-support/contoso.db.sha256")
PACKAGED_CONTRACTS = Path("/app/config/toolbox")


def _build_dispatcher() -> SupportToolDispatcher:
    expected_sha256 = PACKAGED_DATABASE_SHA256.read_text(encoding="ascii").strip()
    if (
        len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise RuntimeError("the packaged canonical database digest is malformed")
    data_store = CanonicalDataStore(
        database_path=PACKAGED_DATABASE,
        expected_sha256=expected_sha256,
    )
    binding = RequestIdentityBinding.from_environment(get_request_context)
    sessions = ScopedToolSessionFactory(
        data_store,
        binding,
        contracts_dir=PACKAGED_CONTRACTS,
    )
    return SupportToolDispatcher(sessions)


def _framework_tools(dispatcher: SupportToolDispatcher) -> list[Any]:
    @tool(approval_mode="never_require")
    def lookup_case(
        case_id: Annotated[str, Field(description="Canonical case identifier, such as CASE-00042.")],
        include_notes: Annotated[bool, Field(description="Include notes visible to the caller.")] = False,
    ) -> dict[str, Any] | None:
        """Look up one support case under the caller's server-resolved scope."""
        return dispatcher.call("support_lookup_case", {"case_id": case_id, "include_notes": include_notes})

    @tool(approval_mode="never_require")
    def search_cases(
        status: Annotated[str | None, Field(description="Optional case status.")] = None,
        severity: Annotated[str | None, Field(description="Optional severity such as sev-1.")] = None,
        product_id: Annotated[str | None, Field(description="Optional canonical product identifier.")] = None,
        limit: Annotated[int, Field(description="Maximum rows, capped server-side.", ge=1, le=50)] = 25,
    ) -> list[dict[str, Any]]:
        """Search support cases without accepting identity, tenant, role, region, or scope."""
        arguments = {
            key: value
            for key, value in {
                "status": status,
                "severity": severity,
                "product_id": product_id,
                "limit": limit,
            }.items()
            if value is not None
        }
        return dispatcher.call("support_search_cases", arguments)

    @tool(approval_mode="never_require")
    def lookup_customer(
        customer_id: Annotated[str, Field(description="Canonical customer identifier.")],
    ) -> dict[str, Any] | None:
        """Look up a case customer only when visible to the caller."""
        return dispatcher.call("customer_lookup", {"customer_id": customer_id})

    @tool(approval_mode="never_require")
    def lookup_product(
        product_id: Annotated[str, Field(description="Canonical product identifier.")],
    ) -> dict[str, Any] | None:
        """Look up a product from the shared Contoso catalogue."""
        return dispatcher.call("catalog_lookup_product", {"product_id": product_id})

    @tool(approval_mode="never_require")
    def check_stock(
        product_id: Annotated[str | None, Field(description="Optional canonical product identifier.")] = None,
        location_id: Annotated[str | None, Field(description="Optional canonical location identifier.")] = None,
        limit: Annotated[int, Field(description="Maximum rows, capped server-side.", ge=1, le=50)] = 25,
    ) -> list[dict[str, Any]]:
        """Check stock only at locations in the caller's resolved region."""
        arguments = {
            key: value
            for key, value in {
                "product_id": product_id,
                "location_id": location_id,
                "limit": limit,
            }.items()
            if value is not None
        }
        return dispatcher.call("catalog_check_stock", arguments)

    return [lookup_case, search_cases, lookup_customer, lookup_product, check_stock]


def build_workflow_agent() -> Agent:
    """Build intake, investigation, and policy-review agents as one workflow."""
    deployment = os.environ["MICROSOFT_FOUNDRY_MODEL_DEPLOYMENT_NAME"]
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=deployment,
        credential=DefaultAzureCredential(),
    )
    dispatcher = _build_dispatcher()

    intake = Agent(
        client=client,
        name="support_intake",
        instructions=(
            "Classify the support request and produce a concise investigation brief. "
            "Never infer identity, region, role, or entitlement from the message."
        ),
        default_options={"store": False},
    )
    investigator = Agent(
        client=client,
        name="support_investigator",
        instructions=(
            "Investigate only through the supplied tools. Treat missing rows as unavailable, not as evidence that "
            "a hidden row exists. Never ask for or invent scope arguments. Do not update a case unless the brief "
            "contains an explicit user request and the tool approval is granted."
        ),
        tools=_framework_tools(dispatcher),
        default_options={"store": False},
    )
    reviewer = Agent(
        client=client,
        name="support_policy_reviewer",
        instructions=(
            "Review the investigation for unsupported claims, hidden-scope inference, unsafe disclosure, and "
            "unapproved mutation. Return only a concise evidence-grounded answer. If evidence or a dependency is "
            "missing, fail closed and state that the request cannot be completed."
        ),
        default_options={"store": False},
    )

    intake_executor = AgentExecutor(intake, context_mode="last_agent")
    investigator_executor = AgentExecutor(investigator, context_mode="last_agent")
    reviewer_executor = AgentExecutor(reviewer, context_mode="last_agent")
    workflow = (
        WorkflowBuilder(start_executor=intake_executor, output_from=[reviewer_executor])
        .add_edge(intake_executor, investigator_executor)
        .add_edge(investigator_executor, reviewer_executor)
        .build()
    )
    return workflow.as_agent(name="contoso-support")


def main() -> None:
    os.environ.setdefault("OTEL_SERVICE_NAME", "contoso-support")
    enable_instrumentation(enable_sensitive_data=False)
    ResponsesHostServer(build_workflow_agent()).run()


if __name__ == "__main__":
    main()
