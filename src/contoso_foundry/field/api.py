"""Internal-only HTTP surface for the Contoso Field Container App."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from contoso_foundry.toolbox.tools import ToolError

from .runtime import FieldService
from .settings import FieldSettings
from .telemetry import configure_telemetry, flush_telemetry


class FieldRequest(BaseModel):
    input: Annotated[str, Field(min_length=1, max_length=8_000)]


class FieldResponse(BaseModel):
    agent: str
    otel_agent_id: str
    output: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = FieldSettings.from_env()
    configure_telemetry(settings)
    service = FieldService(settings)
    service.prepare_data()
    app.state.field_service = service
    yield
    flush_telemetry()


def create_app() -> FastAPI:
    application = FastAPI(
        title="Contoso Field external agent",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @application.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/v1/respond", response_model=FieldResponse)
    async def respond(request: FieldRequest) -> FieldResponse:
        service: FieldService = application.state.field_service
        try:
            output = await service.run(request.input)
        except (PermissionError, ToolError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return FieldResponse(
            agent=service.settings.agent_name,
            otel_agent_id=service.settings.otel_agent_id,
            output=output,
        )

    return application


app = create_app()
