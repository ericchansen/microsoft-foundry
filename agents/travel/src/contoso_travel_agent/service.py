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

    def _json_response(self, status: HTTPStatus, payload: Any) -> None:
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
            if self.tracer is not None:
                with self.tracer.start_as_current_span(
                    "contoso.travel.openapi.authentication"
                ) as span:
                    span.set_attribute("contoso.synthetic", True)
                    span.set_attribute("http.request.method", "POST")
                    span.set_attribute("http.route", route)
                    span.set_attribute("http.response.status_code", 401)
            self._json_response(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        prefix = "/operations/"
        operation = self.path.removeprefix(prefix) if self.path.startswith(prefix) else ""
        content_type = self.headers.get_content_type()
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_response(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return
        if (
            content_type != "application/json"
            or content_length < 0
            or content_length > MAX_REQUEST_BYTES
        ):
            self._json_response(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON request"})
            return
        try:
            arguments = json.loads(self.rfile.read(content_length) or b"{}")
            span_context = (
                self.tracer.start_as_current_span("contoso.travel.openapi")
                if self.tracer is not None
                else nullcontext(None)
            )
            with span_context as span:
                if span is not None:
                    span.set_attribute("contoso.synthetic", True)
                    span.set_attribute("gen_ai.tool.name", operation)
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
            self._json_response(
                HTTPStatus.BAD_REQUEST,
                {"error": type(error).__name__},
            )
            return
        self._json_response(HTTPStatus.OK, result)


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
