"""Live smoke entry point executed inside the Container App revision."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

from opentelemetry import trace
from opentelemetry.trace import Tracer

from .runtime import FieldService
from .settings import FieldSettings
from .telemetry import SMOKE_CORRELATION_ID, SMOKE_REVISION, configure_telemetry, flush_telemetry

DEFAULT_PROMPT = "Summarize WO-00010, including its customer, product, and site."


async def run_smoke(
    service: FieldService,
    prompt: str,
    correlation_id: str,
    revision: str,
    *,
    tracer: Tracer | None = None,
) -> str:
    tracer = tracer or trace.get_tracer(__name__)
    with tracer.start_as_current_span(
        "contoso.field.smoke",
        attributes={
            SMOKE_CORRELATION_ID: correlation_id,
            SMOKE_REVISION: revision,
        },
    ):
        return await service.run(prompt)


async def _run(prompt: str, correlation_id: str, revision: str) -> str:
    settings = FieldSettings.from_env()
    configure_telemetry(settings)
    service = FieldService(settings)
    return await run_smoke(service, prompt, correlation_id, revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one live Contoso Field golden scenario")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--correlation-id", type=UUID, default=uuid4())
    parser.add_argument("--revision", default=os.getenv("CONTAINER_APP_REVISION"))
    args = parser.parse_args(argv)
    if not args.revision:
        parser.error("--revision or CONTAINER_APP_REVISION is required")

    started_at = datetime.now(UTC)
    correlation_id = str(args.correlation_id)
    output = asyncio.run(_run(args.prompt, correlation_id, args.revision))
    flush_telemetry()
    print(
        json.dumps(
            {
                "correlation_id": correlation_id,
                "output": output,
                "revision": args.revision,
                "started_at": started_at.isoformat(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
