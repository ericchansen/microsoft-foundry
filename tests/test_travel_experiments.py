from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agents/travel/src"))

from contoso_travel_agent import experiments as ex  # noqa: E402


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline tests must not call Azure")
    monkeypatch.setattr(ex.azure_cli, "run", forbidden)
    monkeypatch.setattr(ex.boundary, "require_clean_live", forbidden)
    monkeypatch.setattr(ex.costs.PriceClient, "query", forbidden)


def deployment(name="travel-gpt-5-4-mini", model="gpt-5.4-mini", version="2026-03-17"):
    return {
        "id": f"/owned/deployments/{name}", "name": name, "sku": {"name": "GlobalStandard"},
        "properties": {
            "provisioningState": "Succeeded", "versionUpgradeOption": "NoAutoUpgrade",
            "model": {"name": model, "version": version, "format": "OpenAI"},
        },
    }


def targets():
    return {
        side: {
            "version": version, "definition_digest": ex.digest({"version": version}),
            "deployment_digest": ex.digest(deployment()), "deployment": deployment(),
        } for side, version in zip(ex.SIDES, ("2", "7"), strict=True)
    }


def tier(price=2, unit="1M", floor=0):
    return ex.costs.PriceTier(floor, price, unit, "private-meter", "tokens", "sku", "model", "today")


def prices():
    return {"travel-gpt-5-4-mini": {
        "input": {"tiers": [tier(2)]}, "output": {"tiers": [tier(8)]},
    }}


def turn(input_tokens=100, output_tokens=50, cached_tokens=None, elapsed=2):
    return {"elapsed_seconds": elapsed, "usage": {
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached_tokens,
    }}


def sample(key="route-r1"):
    return {
        "sample_key": key, "conversation": '[{"role":"user","content":"Seattle to Chicago?"}]',
        "reference": "ROUTE-0001, LOC-001 to LOC-002", "tool_calls": "[]", "retrieval_evidence": "FAIL",
    }


def output_item(data, passed):
    return {
        "id": "private-output", "status": "pass" if passed else "fail", "datasource_item": data,
        "results": [{"name": c, "passed": passed, "score": int(passed)} for c in ex.CRITERIA],
    }


def eval_client(*, baseline_pass=False):
    client = MagicMock()
    client.with_options.return_value = client
    client.evals.create.return_value = {"id": "private-eval"}
    client.evals.runs.create.side_effect = [
        {"id": "baseline-run", "status": "completed", "result_counts": {
            "total": 1, "passed": int(baseline_pass), "failed": int(not baseline_pass), "errored": 0,
        }},
        {"id": "candidate-run", "status": "completed", "result_counts": {
            "total": 1, "passed": 1, "failed": 0, "errored": 0,
        }},
    ]
    pages = []
    for passed in (baseline_pass, True):
        page = MagicMock()
        page.data = [output_item(sample(), passed)]
        page.has_next_page.return_value = False
        pages.append(page)
    client.evals.runs.output_items.list.side_effect = pages
    return client


def test_dataset_is_natural_bounded_and_reference_backed():
    rows, sha = ex.load_dataset(REPO_ROOT / "agents/travel/golden/operator.jsonl")
    assert len(rows) == 6
    assert sum(len(r["turns"]) for r in rows) == 10
    assert len(sha) == 64
    assert "Seattle" in rows[0]["turns"][0] and "Chicago" in rows[0]["turns"][0]
    assert all("synthetic" not in t for r in rows for t in r["turns"])
    assert all(r["reference"] for r in rows)
    assert "321.81" in rows[0]["reference"]


@pytest.mark.parametrize("case_id", [
    "seattle-chicago-continuity", "unknown-location", "ambiguous-location",
])
def test_location_cases_require_resolver_calls_not_guessed_ids(tmp_path, monkeypatch, case_id):
    rows, _ = ex.load_dataset(REPO_ROOT / "agents/travel/golden/operator.jsonl")
    row = next(row for row in rows if row["case_id"] == case_id)
    required = row["required_tools"]
    resolvers = [tool for tool in required if tool["name"] == "travel_resolve_locations"]
    assert len(resolvers) == 2
    queries = {
        "Seattle": "our SEATTLE headquarters", "Chicago": "Contoso Chicago distribution center",
        "Atlantis Moonbase": "Contoso Atlantis Moonbase office",
        "office": "our office", "distribution": "distribution centre",
    }

    class Runtime:
        def __init__(self, client, spec):
            self.executed_calls = []
            self.response = {"id": "response", "status": "completed"}

        def run_turn(self, prompt, *, agent_version, previous_response_id):
            calls = []
            for tool in required:
                if tool["name"] == "travel_resolve_locations":
                    if agent_version == "2":
                        continue
                    tool = {"name": tool["name"], "arguments": {"query": queries[tool["arguments"]["query"]]}}
                calls.append(SimpleNamespace(as_dict=lambda tool=tool: tool))
            self.executed_calls = calls
            return "plausible answer"

    monkeypatch.setattr(ex, "ServerExecutedTravelRuntime", Runtime)
    samples, _ = ex.collect(
        MagicMock(), None, targets(), [row], 1, ex.Evidence(tmp_path, Path("internal/resolver")),
        time.monotonic() + 30, 10,
    )
    assert samples["baseline"][0]["retrieval_evidence"] == "FAIL"
    assert samples["candidate"][0]["retrieval_evidence"] == "PASS"


@pytest.mark.parametrize("query,passed", [
    ("Contoso SEATTLE headquarters", True), ("Seattle-headquarters", True),
    ("Chicago office", False), ("Seattleish", False), ("", False), (None, False),
])
def test_resolver_evidence_matches_place_terms_not_arbitrary_calls(query, passed):
    assert ex.matches_required_tool(
        {"name": "travel_resolve_locations", "arguments": {"query": query}},
        {"name": "travel_resolve_locations", "arguments": {"query": "Seattle"}},
    ) is passed


def test_resolver_matching_does_not_relax_canonical_ids():
    assert not ex.matches_required_tool(
        {"name": "travel_search_routes", "arguments": {"origin_location_id": "loc-001"}},
        {"name": "travel_search_routes", "arguments": {"origin_location_id": "LOC-001"}},
    )


@pytest.mark.parametrize("version", ["latest", "@latest", "first", "0", "-1", "1/2", "１", ""])
def test_reject_selectors(version):
    with pytest.raises(ex.ExperimentError):
        ex.exact_version(version)


def test_read_exact_definitions_and_model_deployments(monkeypatch):
    project = MagicMock()
    project.agents.get_version.side_effect = [
        {"name": "travel", "version": v, "definition": {"kind": "prompt", "model": "travel-gpt-5-4-mini"}}
        for v in ("2", "7")
    ]
    monkeypatch.setattr(ex, "read_deployment", lambda account, name: deployment(name))
    result = ex.read_targets(project, SimpleNamespace(name="travel"), {"baseline": "2", "candidate": "7"}, "/owned")
    assert [c.kwargs["agent_version"] for c in project.agents.get_version.call_args_list] == ["2", "7"]
    assert result["baseline"]["definition_digest"] == ex.digest(result["baseline"]["definition"])
    project.agents.list_versions.assert_not_called()
    project.agents.create_version.assert_not_called()
    project.agents.update_details.assert_not_called()
    project.agents.delete_version.assert_not_called()


def test_wrong_exact_version_fails_before_traffic(monkeypatch):
    project = MagicMock()
    project.agents.get_version.return_value = {"name": "travel", "version": "9"}
    with pytest.raises(ex.ExperimentError, match="different"):
        ex.read_targets(project, SimpleNamespace(name="travel"), {"baseline": "2"}, "/owned")


@pytest.mark.parametrize("fault", [None, "boundary", "project-id", "endpoint", "environment"])
def test_owned_endpoint_and_optional_inventory_are_verified(tmp_path, monkeypatch, fault):
    plan = {"resource_group": "owned-group", "resources": [{
        "kind": "Microsoft.CognitiveServices/accounts/projects",
        "scope": "providers/Microsoft.CognitiveServices/accounts/owned-account/projects/travel",
    }]}
    guard = MagicMock(return_value=SimpleNamespace(live=True, ok=fault != "boundary", target_exists=True))
    monkeypatch.setattr(ex.boundary, "require_clean_live", guard)
    monkeypatch.setattr(ex.boundary, "load_plan", lambda path: plan)
    endpoint = f"https://{'owned-account'}.services.ai.azure.com/api/projects/travel"
    if fault == "environment":
        monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.invalid/project")
    else:
        monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)

    def read(args, **kwargs):
        assert not kwargs.get("allow_write")
        if args == ["account", "show"]:
            return {"id": "test-subscription"}
        resource_id = args[args.index("--ids") + 1]
        is_project = "/projects/" in resource_id
        return {
            "id": resource_id + ("/wrong" if is_project and fault == "project-id" else ""),
            "properties": {"endpoints": {
                "AI Foundry API": "https://example.invalid/project" if fault == "endpoint" else endpoint,
            }},
        }

    monkeypatch.setattr(ex.azure_cli, "run", read)
    if fault:
        with pytest.raises(ex.ExperimentError):
            ex.owned_project(tmp_path, SimpleNamespace(project="travel"), ["optional-control-plane"])
    else:
        assert ex.owned_project(
            tmp_path, SimpleNamespace(project="travel"), ["optional-control-plane"],
        )["endpoint"] == endpoint
    assert guard.call_args.kwargs["enabled_modules"] == ["optional-control-plane"]


@pytest.mark.parametrize("fault", ["id", "upgrade", "state", "version"])
def test_live_model_must_be_owned_healthy_and_pinned(monkeypatch, fault):
    model = deployment()
    if fault == "id":
        model["id"] = "/different/deployment"
    elif fault == "upgrade":
        model["properties"]["versionUpgradeOption"] = "OnceNewDefaultVersionAvailable"
    elif fault == "state":
        model["properties"]["provisioningState"] = "Failed"
    else:
        model["properties"]["model"]["version"] = ""
    monkeypatch.setattr(ex.azure_cli, "run", lambda *args, **kwargs: model)
    with pytest.raises(ex.ExperimentError):
        ex.read_deployment("/owned", "travel-gpt-5-4-mini")


def test_output_containment_and_no_overwrite(tmp_path):
    with pytest.raises(ex.ExperimentError, match="beneath"):
        ex.Evidence(tmp_path, Path("reports/experiment"))
    with pytest.raises(ex.ExperimentError):
        ex.Evidence(tmp_path, Path("internal/../outside"))
    evidence = ex.Evidence(tmp_path, Path("internal/experiment"))
    evidence.write("turn.json", {"answer": "keep"})
    with pytest.raises(FileExistsError):
        ex.Evidence(tmp_path, Path("internal/experiment"))
    with pytest.raises(FileExistsError):
        evidence.write("turn.json", {"answer": "overwrite"})
    with pytest.raises(ex.ExperimentError):
        evidence.write("../escape.json", {})
    assert json.loads((evidence.root / "turn.json").read_text()) == {"answer": "keep"}


def test_pairing_threading_incremental_capture_and_no_retries(tmp_path, monkeypatch):
    evidence = ex.Evidence(tmp_path, Path("internal/experiment"))
    invocations = []
    client = MagicMock()
    client.with_options.return_value = client

    class Runtime:
        def __init__(self, client, spec):
            self.executed_calls = []

        def run_turn(self, prompt, *, agent_version, previous_response_id):
            if invocations:
                assert len((evidence.root / "turns.jsonl").read_text().splitlines()) == len(invocations)
            invocations.append((prompt, agent_version, previous_response_id))
            self.response = {"id": f"response-{len(invocations)}", "status": "completed"}
            return "answer"

    monkeypatch.setattr(ex, "ServerExecutedTravelRuntime", Runtime)
    rows = [{"case_id": "one", "turns": ["question", "follow-up"], "reference": "ref"}]
    samples, turns = ex.collect(client, None, targets(), rows, 2, evidence, time.monotonic() + 30, 10)
    assert invocations[:4] == [
        ("question", "2", None), ("follow-up", "2", "response-1"),
        ("question", "7", None), ("follow-up", "7", "response-3"),
    ]
    assert invocations[4][1:] == ("7", None)
    assert invocations[6][1:] == ("2", None)
    assert [s["sample_key"] for s in samples["baseline"]] == [s["sample_key"] for s in samples["candidate"]]
    assert "follow-up" in samples["baseline"][0]["conversation"]
    assert turns["baseline"][0]["usage"]["input_tokens"] is None
    assert all(c.kwargs["max_retries"] == 0 for c in client.with_options.call_args_list)


def test_interruption_retains_completed_turn(tmp_path, monkeypatch):
    evidence = ex.Evidence(tmp_path, Path("internal/experiment"))

    class Runtime:
        def __init__(self, client, spec):
            self.executed_calls = []
            self.response = {"id": "private-response", "status": "completed"}

        def run_turn(self, prompt, **kwargs):
            if prompt == "interrupt":
                raise KeyboardInterrupt
            return "done"

    monkeypatch.setattr(ex, "ServerExecutedTravelRuntime", Runtime)
    with pytest.raises(KeyboardInterrupt):
        ex.collect(MagicMock(), None, targets(), [
            {"case_id": "one", "turns": ["first", "interrupt"], "reference": "ref"},
        ], 1, evidence, time.monotonic() + 30, 10)
    assert len((evidence.root / "turns.jsonl").read_text().splitlines()) == 1


def test_collect_integrates_with_server_runtime_without_promotion(tmp_path):
    client = MagicMock()
    client.with_options.return_value = client

    class Response(SimpleNamespace):
        def model_dump(self, **kwargs):
            return vars(self)

    client.responses.create.side_effect = [
        Response(id=f"response-{i}", status="completed", output=[], output_text="answer", usage=None)
        for i in range(4)
    ]
    evidence = ex.Evidence(tmp_path, Path("internal/integration"))
    ex.collect(
        client, SimpleNamespace(name="travel", tools=()), targets(),
        [{"case_id": "one", "turns": ["question", "follow-up"], "reference": "ref"}],
        1, evidence, time.monotonic() + 10, 5,
    )
    calls = client.responses.create.call_args_list
    assert [c.kwargs["extra_body"]["agent_reference"]["version"] for c in calls] == ["2", "2", "7", "7"]
    assert "previous_response_id" not in calls[0].kwargs and "previous_response_id" not in calls[2].kwargs
    assert calls[1].kwargs["previous_response_id"] == "response-0"
    assert calls[3].kwargs["previous_response_id"] == "response-2"
    assert {call[0] for call in client.mock_calls} <= {"with_options", "responses.create"}
    records = [json.loads(line) for line in (evidence.root / "turns.jsonl").read_text().splitlines()]
    assert records[0]["usage"]["input_tokens"] is None


def test_fixed_judge_poor_baseline_still_completes_both_runs(tmp_path):
    evidence = ex.Evidence(tmp_path, Path("internal/experiment"))
    client = eval_client()
    result = ex.evaluate_pair(
        client, {s: [sample()] for s in ex.SIDES}, targets(), "fixed-judge",
        {"experiment": "unique-comparison", "dataset_digest": "digest"},
        evidence, time.monotonic() + 30, 10,
    )
    assert result["baseline"]["route-r1"]["task_correctness"] is False
    assert result["candidate"]["route-r1"]["task_correctness"] is True
    client.evals.create.assert_called_once()
    configured = client.evals.create.call_args.kwargs["testing_criteria"]
    assert {c["model"] for c in configured if c["type"] == "label_model"} == {"fixed-judge"}
    assert all("item.conversation" in c["input"][1]["content"] for c in configured[:2])
    assert all("item.reference" in c["input"][1]["content"] for c in configured[:2])
    requests = client.evals.runs.create.call_args_list
    assert len(requests) == 2 and requests[0].args == requests[1].args == ("private-eval",)
    assert "baseline v2" in requests[0].kwargs["name"]
    assert "candidate v7" in requests[1].kwargs["name"]
    assert (evidence.root / "baseline-run-terminal.json").exists()


@pytest.mark.parametrize("passed", [False, True])
def test_foundry_completed_item_status_is_execution_not_quality(passed):
    data = sample()
    item = output_item(data, passed)
    item["status"] = "completed"
    run = {"result_counts": {"total": 1, "passed": int(passed), "failed": int(not passed), "errored": 0}}
    result = ex.outcomes(run, [item], [data])
    assert result[data["sample_key"]]["task_correctness"] is passed


@pytest.mark.parametrize(
    "mutation", [
        "error-count", "missing-grade", "duplicate", "wrong-case", "wrong-count",
        "bad-status", "bad-score", "grader-state", "grader-sample-error",
    ],
)
def test_schema_errors_are_not_poor_quality(mutation):
    data = sample()
    run = {"result_counts": {"total": 1, "passed": 0, "failed": 1, "errored": 0}}
    item = output_item(data, False)
    items = [item]
    if mutation == "error-count":
        run["result_counts"]["errored"] = 1
    elif mutation == "missing-grade":
        item["results"].pop()
    elif mutation == "duplicate":
        items.append(item)
    elif mutation == "wrong-case":
        item["datasource_item"] = sample("other-r1")
    elif mutation == "wrong-count":
        run["result_counts"]["passed"] = 1
    elif mutation == "bad-score":
        item["results"][0]["score"] = float("nan")
    elif mutation == "grader-state":
        item["results"][0]["status"] = "failed"
    elif mutation == "grader-sample-error":
        item["results"][0]["sample"] = {"error": "grader execution failed"}
    else:
        item["status"] = "error"
    with pytest.raises(ex.ExperimentError):
        ex.outcomes(run, items, [data])


def test_api_error_propagates_no_success_fallback(tmp_path):
    client = eval_client()
    client.evals.create.side_effect = RuntimeError("API schema error")
    with pytest.raises(RuntimeError, match="schema"):
        ex.evaluate_pair(client, {s: [sample()] for s in ex.SIDES}, targets(), "judge",
                         {"experiment": "unique"}, ex.Evidence(tmp_path, Path("internal/one")),
                         time.monotonic() + 10, 5)
    client.evals.runs.create.assert_not_called()


def test_poll_deadline_is_bounded(tmp_path):
    client = eval_client()
    client.evals.runs.create.side_effect = [
        {"id": side, "status": "queued"} for side in ex.SIDES
    ]
    client.evals.runs.retrieve.return_value = {"id": "baseline", "status": "queued"}
    with pytest.raises(ex.ExperimentError, match="deadline"):
        ex.evaluate_pair(client, {s: [sample()] for s in ex.SIDES}, targets(), "judge",
                         {"experiment": "unique"}, ex.Evidence(tmp_path, Path("internal/one")),
                         time.monotonic() + 0.01, 5, poll_seconds=0.01)
    assert client.evals.runs.create.call_count == 2


def test_missing_usage_is_not_zero_and_partial_totals_are_observed_only():
    assert ex.usage({}) == {"input_tokens": None, "output_tokens": None, "cached_tokens": None}
    assert ex.usage({"usage": {"input_tokens": 0, "output_tokens": 0}})["input_tokens"] == 0
    result = ex.metrics([turn(), turn(input_tokens=None)], prices()["travel-gpt-5-4-mini"])
    assert result["observed_tokens"]["input_tokens"] == 100
    assert result["usage_reported_turns"]["input_tokens"] == 1
    assert result["inference_list_price_usd"] is None
    assert result["observed_tokens"]["cached_tokens"] is None


@pytest.mark.parametrize("value", [-1, "10", True])
def test_malformed_usage_fails(value):
    with pytest.raises(ex.ExperimentError):
        ex.usage({"usage": {"input_tokens": value}})


def test_price_arithmetic_separates_input_output_and_ignores_cache_discount():
    result = ex.metrics([turn(1000000, 500000, 900000, 1), turn(0, 0, 0, 3)],
                        prices()["travel-gpt-5-4-mini"])
    assert result["inference_list_price_usd"] == 6
    assert result["median_turn_latency_seconds"] == 2
    assert ex.costs.cost_for_quantity([tier(2, "1K", 0), tier(1, "1K", 2)], 3000) == 5


def test_price_resolution_uses_selected_region_and_estimate_lines(monkeypatch):
    calls = []

    def resolve(client, meter, region):
        calls.append((meter, region))
        return [tier()]

    monkeypatch.setattr(ex.costs, "resolve_tiers", resolve)
    result = ex.resolve_prices(REPO_ROOT, [deployment()], ex.DEFAULT_PRICE_MAP, MagicMock())
    selected = yaml.safe_load((REPO_ROOT / "config/selected-region.yaml").read_text())["region"]
    assert all(region == selected for _, region in calls)
    assert len(calls) == 2
    assert result["travel-gpt-5-4-mini"]["input"]["line_id"] == "travel-gpt-5-4-mini-input"


def test_unpriced_or_wrong_model_fails_before_traffic(monkeypatch):
    with pytest.raises(ex.ExperimentError, match="mapping"):
        ex.resolve_prices(REPO_ROOT, [deployment()], {}, MagicMock())
    with pytest.raises(ex.ExperimentError, match="configured"):
        ex.resolve_prices(REPO_ROOT, [deployment(version="wrong")], ex.DEFAULT_PRICE_MAP, MagicMock())
    monkeypatch.setattr(ex.costs, "resolve_tiers", lambda *args: [])
    with pytest.raises(ex.ExperimentError, match="unresolved"):
        ex.resolve_prices(REPO_ROOT, [deployment()], ex.DEFAULT_PRICE_MAP, MagicMock())


def test_public_summary_allowlist_excludes_private_evidence():
    target = targets()
    target["baseline"]["definition"] = {"secret": "never-publish"}
    result = {s: {"route-r1": {c: s == "candidate" for c in ex.CRITERIA}} for s in ex.SIDES}
    summary = ex.public_summary(
        target, ex.digest("dataset"), ex.digest("judge"), result,
        {s: [turn()] for s in ex.SIDES}, prices(), 5,
    )
    text = json.dumps(summary)
    assert "never-publish" not in text and "private-meter" not in text
    assert "response_id" not in text and "report_url" not in text and "/owned/" not in text
    assert summary["candidate_minus_baseline_pass_rate"]["task_correctness"] == 1
    assert not ex.scan.scan_text(text)
    assert "Judge/eval/tool/platform costs excluded" in summary["cost_scope"]


def test_summary_scanner_rejects_identifiers():
    result = {s: {"route-r1": dict.fromkeys(ex.CRITERIA, True)} for s in ex.SIDES}
    # Construct a non-placeholder identifier locally; never publish the sample.
    private = str(ex.uuid.uuid4())
    with pytest.raises(ex.ExperimentError, match="scan"):
        ex.public_summary(targets(), private, ex.digest("judge"), result,
                          {s: [turn()] for s in ex.SIDES}, prices(), 5)


def test_default_cli_is_offline_and_does_not_write(tmp_path, capsys):
    assert ex.main([
        "--repo-root", str(REPO_ROOT), "--baseline-version", "2", "--candidate-version", "7",
        "--judge-deployment", "judge", "--budget-reserve-usd", "5",
        "--output-dir", str(tmp_path / "not-created"),
    ]) == 0
    assert "not_run" in capsys.readouterr().out
    assert not (tmp_path / "not-created").exists()


def test_live_cli_retains_error_without_printing_private_exception(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ex, "load_dataset", lambda path: ([sample()], ex.digest("test")))

    def fail(args, evidence):
        evidence.append("turns.jsonl", {"response_id": "private-response"})
        raise ex.ExperimentError("private-endpoint-and-token")

    monkeypatch.setattr(ex, "run_experiment", fail)
    args = [
        "--repo-root", str(tmp_path), "--baseline-version", "2", "--candidate-version", "7",
        "--judge-deployment", "judge", "--budget-reserve-usd", "5", "--output-dir", "internal/new", "--run",
    ]
    assert ex.main(args) == 1
    captured = capsys.readouterr()
    assert "private-endpoint" not in captured.err + captured.out
    paths = list((tmp_path / "internal/new").glob("*/error.json"))
    assert len(paths) == 1 and "private-endpoint" in paths[0].read_text()
    assert ex.main(args) == 1
    assert len(list((tmp_path / "internal/new").iterdir())) == 1


def test_workflow_is_manual_protected_and_uploads_only_scanned_summary():
    text = (REPO_ROOT / ".github/workflows/travel-experiment.yml").read_text()
    workflow = yaml.safe_load(text)
    trigger = workflow.get("on", workflow.get(True))
    assert set(trigger) == {"workflow_dispatch"}
    assert trigger["workflow_dispatch"]["inputs"]["run"]["default"] is False
    job = workflow["jobs"]["compare"]
    assert job["environment"] == "contoso-agents"
    assert job["concurrency"]["group"] == "azure-rg-contoso-agents"
    assert "refs/heads/main" in job["if"]
    upload = next(s for s in job["steps"] if "actions/upload-artifact@" in s.get("uses", ""))
    assert upload["with"]["path"] == "reports/travel-experiment/summary.json"
    assert "steps.scan.outcome == 'success'" in upload["if"]
    assert "foundry scan" in text
    for step in job["steps"]:
        if "uses" in step:
            assert len(step["uses"].split("@")[1]) == 40
