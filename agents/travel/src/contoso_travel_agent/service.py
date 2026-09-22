"""Authenticated HTTPS backend for the Travel prompt agent's OpenAPI tool."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import tempfile
from contextlib import nullcontext
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from contoso_foundry.data import build as build_mod
from contoso_foundry.toolbox.repository import ScopeViolationError
from contoso_foundry.toolbox.tools import Toolbox, ToolError
from contoso_travel_agent.identity import SYNTHETIC_TRAVEL_PRINCIPAL

SAFE_OPERATIONS = frozenset(
    {
        "travel_resolve_locations",
        "travel_search_routes",
        "travel_search_fares",
        "travel_get_policy",
        "travel_simulate_booking",
    }
)
MAX_REQUEST_BYTES = 16 * 1024
LOGGER = logging.getLogger(__name__)


def api_key_matches(supplied: str, expected: str) -> bool:
    try:
        supplied_bytes = supplied.encode("latin-1")
    except UnicodeEncodeError:
        supplied_bytes = supplied.encode("utf-8")
    expected_bytes = expected.encode("utf-8")
    return hmac.compare_digest(
        hashlib.sha256(supplied_bytes).digest(),
        hashlib.sha256(expected_bytes).digest(),
    )


def build_toolbox(repo_root: Path) -> tuple[sqlite3.Connection, Toolbox]:
    result = build_mod.build(
        config_path=repo_root / "config" / "data-spine.yaml",
        seed_dir=repo_root / "data" / "seed",
        out_dir=Path(tempfile.mkdtemp(prefix="contoso-travel-service-")),
        fixtures_dir=repo_root / "data" / "fixtures",
    )
    connection = sqlite3.connect(result.root / "contoso.db", check_same_thread=False)
    toolbox = Toolbox(
        connection,
        SYNTHETIC_TRAVEL_PRINCIPAL,
        contracts_dir=repo_root / "config" / "toolbox",
    )
    return connection, toolbox


def execute_operation(toolbox: Toolbox, operation: str, arguments: Any) -> Any:
    if operation not in SAFE_OPERATIONS:
        raise KeyError("unknown Travel operation")
    if not isinstance(arguments, dict):
        raise ValueError("request body must be a JSON object")
    return toolbox.call(operation, arguments)


class TravelToolHandler(BaseHTTPRequestHandler):
    toolbox: Toolbox
    api_key: str
    tracer: Any | None = None

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json_response(self, status: HTTPStatus, payload: Any, span: Any | None = None) -> None:
        if span is not None:
            from opentelemetry.trace import StatusCode

            span.set_attribute("http.response.status_code", int(status))
            if status >= HTTPStatus.BAD_REQUEST:
                span.set_status(StatusCode.ERROR)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json_response(HTTPStatus.OK, {"status": "healthy"})
            return
        self._json_response(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        prefix = "/operations/"
        operation = self.path.removeprefix(prefix) if self.path.startswith(prefix) else ""
        with self._operation_span(operation) as span:
            self._handle_operation(operation, span)

    def _operation_span(self, operation: str) -> Any:
        if self.tracer is None:
            return nullcontext(None)
        from opentelemetry.context import Context
        from opentelemetry.trace import SpanKind
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        # Only W3C trace context crosses this boundary. Never extract baggage,
        # identity headers, or arbitrary fields into authorization or telemetry.
        carrier = {}
        for name in ("traceparent", "tracestate"):
            values = self.headers.get_all(name, [])
            if len(values) == 1:
                carrier[name] = values[0]
        parent = TraceContextTextMapPropagator().extract(carrier, context=Context())
        known_operation = operation if operation in SAFE_OPERATIONS else "unknown"
        return self.tracer.start_as_current_span(
            "contoso.travel.openapi",
            context=parent,
            kind=SpanKind.SERVER,
            attributes={
                "contoso.synthetic": True,
                "gen_ai.agent.name": "contoso-travel",
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": known_operation,
                "http.request.method": "POST",
                "http.route": f"/operations/{known_operation}" if operation in SAFE_OPERATIONS else "/operations/*",
            },
            # Exception messages may contain rejected inputs. Export only a
            # safe error class and status, not request bodies or credentials.
            record_exception=False,
            set_status_on_exception=False,
        )

    def _handle_operation(self, operation: str, span: Any | None) -> None:
        supplied_key = self.headers.get("x-travel-tool-key", "")
        if not api_key_matches(supplied_key, self.api_key):
            route = "/operations/*" if self.path.startswith("/operations/") else "unknown"
            LOGGER.warning(
                "Travel tool authentication rejected",
                extra={
                    "custom_dimensions": {
                        "contoso.synthetic": True,
                        "http.request.method": "POST",
                        "http.route": route,
                    }
                },
            )
            self._json_response(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, span)
            return
        content_type = self.headers.get_content_type()
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_response(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"}, span)
            return
        if (
            content_type != "application/json"
            or content_length < 0
            or content_length > MAX_REQUEST_BYTES
        ):
            self._json_response(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON request"}, span)
            return
        try:
            arguments = json.loads(self.rfile.read(content_length) or b"{}")
            result = execute_operation(self.toolbox, operation, arguments)
        except (
            json.JSONDecodeError,
            KeyError,
            PermissionError,
            ScopeViolationError,
            ToolError,
            TypeError,
            ValueError,
        ) as error:
            if span is not None:
                span.set_attribute("error.type", type(error).__name__)
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": type(error).__name__},
                span,
            )
            return
        except Exception as error:
            if span is not None:
                span.set_attribute("error.type", type(error).__name__)
            self._json_response(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "tool execution failed"},
                span,
            )
            return
        self._json_response(HTTPStatus.OK, result, span)


def _configure_telemetry() -> Any | None:
    connection_string = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        return None
    from azure.identity import ManagedIdentityCredential
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry import trace

    client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
    if not client_id:
        raise RuntimeError("AZURE_CLIENT_ID is required when telemetry is configured")
    configure_azure_monitor(
        connection_string=connection_string,
        credential=ManagedIdentityCredential(client_id=client_id),
    )
    return trace.get_tracer("contoso.travel.openapi")


def main() -> int:
    api_key = os.environ.get("TRAVEL_TOOL_API_KEY", "")
    if len(api_key) < 32:
        raise RuntimeError("TRAVEL_TOOL_API_KEY must contain at least 32 characters")
    repo_root = Path(os.environ.get("REPO_ROOT", "/app"))
    connection, toolbox = build_toolbox(repo_root)
    TravelToolHandler.api_key = api_key
    TravelToolHandler.toolbox = toolbox
    TravelToolHandler.tracer = _configure_telemetry()
    server = HTTPServer(("0.0.0.0", 8080), TravelToolHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
