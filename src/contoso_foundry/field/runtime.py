"""Pydantic AI field-service agent backed by the canonical Toolbox."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from contoso_foundry.data.build import build
from contoso_foundry.toolbox.identity import principal_from_fixture
from contoso_foundry.toolbox.tools import Toolbox, ToolError

from .settings import FieldSettings

SYSTEM_INSTRUCTIONS = """
You are Contoso Field, an operations assistant for field engineers.
Use the canonical tools before stating any work-order, customer, product, or location fact.
Treat tool scope as authoritative and never ask for an identity, tenant, role, or region override.
Do not invent a work order, part, customer, or site. Explain when a scoped lookup returns no record.
This agent is read-only: it can inspect and summarize work but cannot dispatch, close, or modify it.
""".strip()


@dataclass(frozen=True)
class FieldDependencies:
    toolbox: Toolbox


def _call_toolbox(toolbox: Toolbox, name: str, arguments: dict[str, Any]) -> Any:
    try:
        return toolbox.call(name, arguments)
    except ToolError as error:
        raise ModelRetry(str(error)) from error


def _azure_model(settings: FieldSettings) -> Model:
    settings.validate_runtime()
    credential = DefaultAzureCredential(managed_identity_client_id=settings.azure_client_id)
    token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
    client = AsyncAzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_version=settings.azure_openai_api_version,
        azure_ad_token_provider=token_provider,
    )
    return OpenAIChatModel(
        settings.model_deployment,
        provider=OpenAIProvider(openai_client=client),
    )


def create_agent(settings: FieldSettings, *, model: Model | None = None) -> Agent[FieldDependencies, str]:
    agent = Agent(
        model or _azure_model(settings),
        deps_type=FieldDependencies,
        output_type=str,
        instructions=SYSTEM_INSTRUCTIONS,
        name=settings.agent_name,
        tool_timeout=20,
    )

    @agent.tool
    async def lookup_work_order(ctx: RunContext[FieldDependencies], work_order_id: str) -> dict[str, Any] | None:
        """Look up one canonical work order visible to this field-service identity."""
        return _call_toolbox(ctx.deps.toolbox, "operations_lookup_work_order", {"work_order_id": work_order_id})

    @agent.tool
    async def search_work_orders(
        ctx: RunContext[FieldDependencies],
        status: str | None = None,
        priority: str | None = None,
        location_id: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search the scoped work-order queue using canonical operational filters."""
        arguments = {
            key: value
            for key, value in {
                "status": status,
                "priority": priority,
                "location_id": location_id,
                "limit": limit,
            }.items()
            if value is not None
        }
        return _call_toolbox(ctx.deps.toolbox, "operations_search_work_orders", arguments)

    @agent.tool
    async def list_locations(
        ctx: RunContext[FieldDependencies],
        country: str | None = None,
        kind: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List field-service locations visible to this identity."""
        arguments = {
            key: value
            for key, value in {"country": country, "kind": kind, "limit": limit}.items()
            if value is not None
        }
        return _call_toolbox(ctx.deps.toolbox, "operations_list_locations", arguments)

    @agent.tool
    async def lookup_product(ctx: RunContext[FieldDependencies], product_id: str) -> dict[str, Any] | None:
        """Look up the canonical product referenced by a work order."""
        return _call_toolbox(ctx.deps.toolbox, "catalog_lookup_product", {"product_id": product_id})

    @agent.tool
    async def lookup_customer(ctx: RunContext[FieldDependencies], customer_id: str) -> dict[str, Any] | None:
        """Look up the in-scope canonical customer referenced by a work order."""
        return _call_toolbox(ctx.deps.toolbox, "customer_lookup", {"customer_id": customer_id})

    return agent


class FieldService:
    """Owns immutable runtime configuration and opens one scoped Toolbox per request."""

    def __init__(
        self,
        settings: FieldSettings,
        *,
        model: Model | None = None,
        database_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self._database_path = database_path
        self.agent = create_agent(settings, model=model)

    @property
    def database_path(self) -> Path:
        if self._database_path is None:
            raise RuntimeError("field-service data has not been prepared")
        return self._database_path

    def prepare_data(self) -> Path:
        if self._database_path is None:
            result = build(
                config_path=self.settings.data_config,
                seed_dir=self.settings.seed_dir,
                out_dir=self.settings.data_dir,
            )
            self._database_path = result.root / "contoso.db"
        return self._database_path

    async def run(self, prompt: str) -> str:
        if not prompt.strip():
            raise ValueError("the field-service prompt must not be empty")
        database = self.prepare_data()
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            toolbox = Toolbox(
                connection,
                principal_from_fixture(self.settings.principal_oid, self.settings.principal_tid),
                contracts_dir=self.settings.contracts_dir,
            )
            result = await self.agent.run(prompt, deps=FieldDependencies(toolbox))
            return result.output
        finally:
            connection.close()
