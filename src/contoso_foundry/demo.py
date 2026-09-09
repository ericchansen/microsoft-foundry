"""Bounded presenter exercises against the actual owned platform."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import UUID, uuid4

import requests
import yaml

from . import azure_cli, boundary, gateway


class DemoError(RuntimeError):
    """An exercise did not produce the required observable evidence."""


def _save(directory: Path, name: str, payload: Any) -> None:
    (directory / f"{name}.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8",
    )
    if not name.endswith(("-logs", "-telemetry")):
        print(f"Recorded {name}", flush=True)


def evidence_directory(root: Path, relative: str, *, existing: bool = False) -> Path:
    destination = (root / relative).resolve()
    if (root / "internal").resolve() not in destination.parents:
        raise DemoError("Live evidence must be stored inside internal/")
    if existing:
        if not destination.is_dir():
            raise DemoError("The existing private evidence directory was not found")
    else:
        destination.mkdir(parents=True, exist_ok=False)
    return destination


def _request(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    key: str | None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Ocp-Apim-Subscription-Key"] = key
    started = datetime.now(UTC).isoformat()
    response = session.post(url, json=payload, headers=headers, timeout=60, allow_redirects=False)
    return {
        "started_at": started,
        "http_status": response.status_code,
        "gateway_request_id": response.headers.get("x-contoso-gateway-request-id"),
        "response": response.json(),
    }


def verify_gateway_log(rows: list[dict[str, Any]], request_id: str, status: int) -> bool:
    """Match one APIM request, not an unrelated successful call in a time window."""
    matching = [
        row for row in rows
        if str(row.get("CorrelationId", "")).casefold() == request_id.casefold()
    ]
    if not matching:
        return False
    if any(int(row["ResponseCode"]) != status for row in matching):
        raise DemoError("The correlated Gateway log disagrees with the HTTP response")
    # The CLI tabular adapter serializes a null backend status as the string "None".
    backend_codes = [
        None if row.get("BackendResponseCode") in (None, "", "None") else int(row["BackendResponseCode"])
        for row in matching
    ]
    if status == 200 and any(code != 200 for code in backend_codes):
        raise DemoError("The correlated Gateway request did not reach a successful backend")
    if status == 401 and any(code not in (None, 0) for code in backend_codes):
        raise DemoError("The denied Gateway request unexpectedly reached the backend")
    return True


def rehearse_gateway(
    root: Path,
    destination: Path,
    *,
    enabled_modules: list[str],
    session: requests.Session,
    log_timeout: float = 240,
) -> dict[str, Any]:
    report = boundary.require_clean_live(
        root / "config" / "boundary.yaml", enabled_modules=enabled_modules,
    )
    config = gateway.load_config(root / "config" / "gateway.yaml")
    location = yaml.safe_load((root / "config" / "selected-region.yaml").read_text())["region"]
    verified = gateway.collect_status(
        report.resource_group, "contoso-agents", config, expected_location=location,
    )
    if not verified.ok:
        raise DemoError("Gateway governance preflight failed")
    service = azure_cli.run(["apim", "show", "-g", report.resource_group, "-n", "contoso-agents-gateway"])
    api = azure_cli.run([
        "apim", "api", "show", "-g", report.resource_group,
        "--service-name", service["name"], "--api-id", "foundry-travel",
    ])
    operation = azure_cli.run([
        "apim", "api", "operation", "show", "-g", report.resource_group,
        "--service-name", service["name"], "--api-id", api["name"],
        "--operation-id", "chat-completions",
    ])
    if operation["method"] != "POST" or operation["urlTemplate"] != "/chat/completions":
        raise DemoError("The enrolled route is not the expected chat-completions operation")
    url = service["gatewayUrl"].rstrip("/") + "/" + api["path"].strip("/") + operation["urlTemplate"]
    if urlparse(url).scheme != "https" or urlparse(url).netloc != urlparse(service["gatewayUrl"]).netloc:
        raise DemoError("The request target is not the owned HTTPS Gateway")
    subscription_name = "travel-gateway"
    subscription_url = urljoin(
        "https://management.azure.com" + service["id"] + "/",
        f"subscriptions/{subscription_name}?api-version=2024-05-01",
    )
    subscription = azure_cli.run(["rest", "--method", "get", "--url", subscription_url])
    if (
        subscription["properties"]["state"] != "active"
        or subscription["properties"]["scope"].casefold() != api["id"].casefold()
    ):
        raise DemoError("The Gateway subscription is not scoped to the expected API")
    payload = {
        "model": "travel-gpt-5-4-mini",
        "messages": [{"role": "user", "content": "Reply with exactly CONTOSO_GATEWAY_READY."}],
        "max_completion_tokens": 80,
    }
    results = {}
    results["denied"] = _request(session, url, payload, key=None)
    _save(destination, "gateway-denied", results["denied"])
    if results["denied"]["http_status"] != 401:
        raise DemoError("The Gateway did not reject a request without its subscription credential")
    # listSecrets is an ARM POST but does not create or rotate a credential.
    keys = azure_cli.run([
        "rest", "--method", "post", "--url",
        urljoin(subscription_url.split("?")[0] + "/", "listSecrets?api-version=2024-05-01"),
    ], allow_write=True)
    key = keys.get("primaryKey")
    if not isinstance(key, str) or not key:
        raise DemoError("The existing Gateway subscription has no usable credential")
    results["allowed"] = _request(session, url, payload, key=key)
    _save(destination, "gateway-allowed", results["allowed"])
    if results["allowed"]["http_status"] != 200:
        raise DemoError("The authenticated Gateway request did not succeed")
    choices = results["allowed"]["response"].get("choices", [])
    if not choices or "CONTOSO_GATEWAY_READY" not in choices[0].get("message", {}).get("content", ""):
        raise DemoError("The authenticated model did not return the requested bounded response")
    for result in results.values():
        try:
            UUID(str(result["gateway_request_id"]))
        except (ValueError, TypeError, AttributeError) as error:
            raise DemoError("The Gateway did not return its own correlation header") from error
    workspace = azure_cli.run([
        "monitor", "log-analytics", "workspace", "show", "-g", report.resource_group,
        "-n", "contoso-agents-logs",
    ])
    ids = ", ".join(f"'{UUID(result['gateway_request_id'])}'" for result in results.values())
    query = (
        "ApiManagementGatewayLogs | where TimeGenerated > ago(15m) "
        f"| where CorrelationId in ({ids}) "
        "| project TimeGenerated, CorrelationId, ApiId, OperationId, ResponseCode, BackendResponseCode"
    )
    deadline = time.monotonic() + log_timeout
    while True:
        rows = azure_cli.run([
            "monitor", "log-analytics", "query", "--workspace", workspace["customerId"],
            "--analytics-query", query, "--timespan", "PT15M",
        ])
        _save(destination, "gateway-logs", rows)
        if all(
            verify_gateway_log(rows, result["gateway_request_id"], result["http_status"])
            for result in results.values()
        ):
            break
        if time.monotonic() >= deadline:
            raise DemoError("The exact Gateway requests have not appeared in diagnostic logs")
        time.sleep(10)
    summary = {
        "scenario": "gateway-access-boundary",
        "status": "passed",
        "unauthenticated_http_status": 401,
        "authenticated_http_status": 200,
        "both_requests_correlated": True,
        "denied_before_backend": True,
        "model": payload["model"],
        "policy_or_credentials_changed": False,
    }
    _save(destination, "summary", summary)
    return summary


def parse_field_console(text: str) -> dict[str, Any]:
    results = []
    for line in text.splitlines():
        if not line.lstrip().startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and {"correlation_id", "revision", "output", "started_at"} <= value.keys():
            results.append(value)
    if len(results) != 1:
        raise DemoError("The Field console must return exactly one structured execution result")
    return results[0]


def _verify_field_answer(output: str) -> None:
    required = (
        "WO-00015", "Juniper Reach Industries", "Contoso Compact Cable Tray H301",
        "Contoso Singapore Office",
    )
    if not all(value.casefold() in output.casefold() for value in required):
        raise DemoError("Field did not return the expected work order, customer, product and site")


def _wait_for_replica(group: str, previous_revision: str, *, new_revision: bool) -> str:
    deadline = time.monotonic() + 300
    while True:
        app = azure_cli.run(["containerapp", "show", "-g", group, "-n", "contoso-field"])
        revision = app["properties"].get("latestReadyRevisionName")
        if revision and (not new_revision or revision != previous_revision):
            replicas = azure_cli.run([
                "containerapp", "replica", "list", "-g", group, "-n", "contoso-field",
                "--revision", revision,
            ])
            if replicas:
                return revision
        if time.monotonic() >= deadline:
            raise DemoError("The exact Field revision did not acquire a replica within five minutes")
        time.sleep(5)


def rehearse_field(
    root: Path, destination: Path, *, enabled_modules: list[str], log_timeout: float = 240
) -> dict[str, Any]:
    report = boundary.require_clean_live(
        root / "config" / "boundary.yaml", enabled_modules=enabled_modules,
    )
    group = report.resource_group
    app = azure_cli.run(["containerapp", "show", "-g", group, "-n", "contoso-field"])
    properties = app["properties"]
    scale = properties["template"]["scale"]
    if (
        properties["configuration"]["ingress"]["external"] is not False
        or scale["minReplicas"] != 0
        or scale["maxReplicas"] != 1
        or properties["configuration"]["activeRevisionsMode"] != "Single"
    ):
        raise DemoError("Field must retain internal ingress, single-revision mode and zero-to-one scaling")
    deployments = azure_cli.run(["deployment", "group", "list", "-g", group])
    if any(item["properties"]["provisioningState"] in {"Running", "Accepted"} for item in deployments):
        raise DemoError("An owned resource-group deployment is active")
    previous_revision = properties["latestReadyRevisionName"]
    correlation = str(uuid4())
    _save(destination, "field-before", app)
    _save(destination, "field-restoration", {"resource_group": group, "original_min_replicas": 0})
    try:
        azure_cli.run([
            "containerapp", "update", "-g", group, "-n", "contoso-field", "--min-replicas", "1",
        ], allow_write=True)
        revision = _wait_for_replica(group, previous_revision, new_revision=True)
        console = azure_cli.run([
            "containerapp", "exec", "-g", group, "-n", "contoso-field",
            "--revision", revision, "--container", "field", "--command",
            f"env FIELD_DATA_DIR=/tmp/contoso-field-smoke-{correlation} "
            f"python -m contoso_foundry.field.smoke --correlation-id {correlation} --revision {revision}",
        ], allow_write=True, parse_json=False, timeout=240)
        _save(destination, "field-console", console)
        result = parse_field_console(console)
        _save(destination, "field-execution", result)
        if result["correlation_id"] != correlation or result["revision"] != revision:
            raise DemoError("Field returned a different execution correlation or revision")
        _verify_field_answer(str(result["output"]))
    finally:
        azure_cli.run([
            "containerapp", "update", "-g", group, "-n", "contoso-field", "--min-replicas", "0",
        ], allow_write=True)
        restored_revision = _wait_for_restoration(group)
        _save(destination, "field-restored", {"min_replicas": 0, "replicas": 0, "revision": restored_revision})
    return verify_field_evidence(destination, group=group, log_timeout=log_timeout)


def verify_field_evidence(
    destination: Path, *, group: str, log_timeout: float = 240,
) -> dict[str, Any]:
    """Finish a recorded execution without repeating inference or scaling."""
    from .field.register import query_live_telemetry, verify_live_telemetry

    result = json.loads((destination / "field-execution.json").read_text(encoding="utf-8"))
    original = json.loads((destination / "field-before.json").read_text(encoding="utf-8"))
    if (
        original.get("name") != "contoso-field"
        or original.get("resourceGroup", "").casefold() != group.casefold()
    ):
        raise DemoError("The Field evidence does not belong to the expected owned resource")
    correlation = str(UUID(result["correlation_id"]))
    revision = str(result["revision"])
    _verify_field_answer(str(result["output"]))
    started = datetime.fromisoformat(result["started_at"].replace("Z", "+00:00"))
    if started.tzinfo is None or started > datetime.now(UTC):
        raise DemoError("Field evidence must have a valid past timezone-aware start time")
    restored_revision = _wait_for_restoration(group)
    _save(destination, "field-restored", {"min_replicas": 0, "replicas": 0, "revision": restored_revision})
    deadline = time.monotonic() + log_timeout
    while True:
        payload = query_live_telemetry(
            resource_group=group, application_insights_name="contoso-agents-insights",
            agent_name="contoso-field", smoke_correlation_id=correlation,
            smoke_started_at=started, container_app_revision=revision,
        )
        _save(destination, "field-telemetry", payload)
        tables = payload.get("tables", [])
        if tables and tables[0].get("rows"):
            columns = [column["name"] for column in tables[0]["columns"]]
            row = dict(zip(columns, tables[0]["rows"][0], strict=True))
            if int(row.get("total_spans", 0)) > 0:
                evidence = verify_live_telemetry(
                    payload, expected_agent_id="contoso-field-v1",
                    smoke_correlation_id=correlation, container_app_revision=revision,
                )
                break
        if time.monotonic() >= deadline:
            raise DemoError("Fresh Field telemetry did not arrive before the deadline; scale was restored")
        time.sleep(10)
    summary = {
        "scenario": "external-field-work-order",
        "status": "passed",
        "correlated_spans": evidence.total_spans,
        "all_spans_attributed": True,
        "restored_min_replicas": 0,
        "restored_actual_replicas": 0,
        "ingress_changed": False,
        "registration_changed": False,
        "invocation_path": "inside the deployed container, not public HTTP ingress",
        "execution_started_at": started.isoformat(),
        "verified_at": datetime.now(UTC).isoformat(),
    }
    _save(destination, "summary", summary)
    return summary


def _wait_for_restoration(group: str) -> str:
    deadline = time.monotonic() + 600
    while True:
        app = azure_cli.run(["containerapp", "show", "-g", group, "-n", "contoso-field"])
        properties = app["properties"]
        if (
            properties["template"]["scale"]["minReplicas"] != 0
            or properties["configuration"]["ingress"]["external"] is not False
        ):
            raise DemoError("Field restoration did not preserve scale and ingress")
        revision = properties["latestReadyRevisionName"]
        if revision == properties["latestRevisionName"]:
            revisions = azure_cli.run(["containerapp", "revision", "list", "-g", group, "-n", "contoso-field"])
            old_active = any(
                item["name"] != revision and item["properties"]["active"]
                for item in revisions
            )
            replicas = azure_cli.run([
                "containerapp", "replica", "list", "-g", group, "-n", "contoso-field",
                "--revision", revision,
            ])
            if not replicas and not old_active:
                return revision
        if time.monotonic() >= deadline:
            raise DemoError("Field has not returned to zero replicas; private restoration evidence is available")
        time.sleep(10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=["gateway", "field"])
    parser.add_argument("--run", action="store_true", help="Explicitly authorize bounded live requests")
    parser.add_argument(
        "--resume", action="store_true",
        help="Finish existing Field evidence verification without repeating inference or scaling",
    )
    parser.add_argument("--enable-module", action="append", default=[])
    parser.add_argument("--output", default=None, help="New evidence directory under internal/")
    args = parser.parse_args()
    if args.resume and (args.scenario != "field" or not args.output or args.run):
        parser.error("--resume requires scenario field and --output, and cannot be combined with --run")
    if not args.run and not args.resume:
        parser.error("--run is required; exercises make real model calls and may temporarily scale owned compute")
    root = Path(__file__).resolve().parents[2]
    relative = args.output or f"internal/demo/{args.scenario}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    destination = evidence_directory(root, relative, existing=args.resume)
    if args.resume:
        report = boundary.require_clean_live(
            root / "config" / "boundary.yaml", enabled_modules=args.enable_module,
        )
        result = verify_field_evidence(destination, group=report.resource_group)
    elif args.scenario == "gateway":
        with requests.Session() as session:
            result = rehearse_gateway(root, destination, enabled_modules=args.enable_module, session=session)
    else:
        result = rehearse_field(root, destination, enabled_modules=args.enable_module)
    print(json.dumps(result, indent=2))
    print(f"Private evidence: {destination.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
