from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from contoso_foundry import demo


def test_evidence_stays_private_and_refuses_overwrite(tmp_path):
    with pytest.raises(demo.DemoError, match="inside internal"):
        demo.evidence_directory(tmp_path, "reports/demo")
    path = demo.evidence_directory(tmp_path, "internal/demo")
    assert path.is_dir()
    with pytest.raises(FileExistsError):
        demo.evidence_directory(tmp_path, "internal/demo")
    assert demo.evidence_directory(tmp_path, "internal/demo", existing=True) == path
    with pytest.raises(demo.DemoError, match="not found"):
        demo.evidence_directory(tmp_path, "internal/missing", existing=True)


def test_log_must_match_exact_request():
    rows = [{"CorrelationId": "different", "ResponseCode": 200, "BackendResponseCode": 200}]
    assert not demo.verify_gateway_log(rows, "expected", 200)
    rows[0]["CorrelationId"] = "EXPECTED"
    assert demo.verify_gateway_log(rows, "expected", 200)


@pytest.mark.parametrize("missing_backend", [None, "", "None", 0])
def test_denial_must_happen_before_backend(missing_backend):
    row = {"CorrelationId": "expected", "ResponseCode": "401", "BackendResponseCode": missing_backend}
    assert demo.verify_gateway_log([row], "expected", 401)
    row["BackendResponseCode"] = 401
    with pytest.raises(demo.DemoError, match="reached the backend"):
        demo.verify_gateway_log([row], "expected", 401)


def test_log_disagreement_is_not_success():
    row = {"CorrelationId": "expected", "ResponseCode": 401}
    with pytest.raises(demo.DemoError, match="disagrees"):
        demo.verify_gateway_log([row], "expected", 200)


def test_request_does_not_follow_redirects_or_persist_credentials():
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.headers = {"x-contoso-gateway-request-id": "correlation"}
    session.post.return_value.json.return_value = {"choices": []}
    result = demo._request(session, "https://gateway.example.invalid", {}, key="private-key")
    assert session.post.call_args.kwargs["allow_redirects"] is False
    assert "private-key" not in str(result)


def test_both_policy_paths_return_gateway_request_id(repo_root):
    source = (repo_root / "infra" / "modules" / "gateway-association.bicep").read_text()
    assert 'name="x-contoso-gateway-request-id"' in source
    assert "@(context.RequestId.ToString())" in source
    assert source.count("<outbound><base />${requestIdHeader}</outbound>") == 2
    assert source.count("<on-error><base />${requestIdHeader}</on-error>") == 2


def test_field_console_requires_one_complete_result():
    payload = '{"correlation_id":"run","revision":"revision","output":"answer","started_at":"now"}'
    assert demo.parse_field_console("Connecting\n" + payload)["output"] == "answer"
    with pytest.raises(demo.DemoError, match="exactly one"):
        demo.parse_field_console("Connected but no result")
    with pytest.raises(demo.DemoError, match="exactly one"):
        demo.parse_field_console(payload + "\n" + payload)


def test_field_restores_scale_even_when_execution_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        demo.boundary, "require_clean_live",
        lambda *_args, **_kwargs: SimpleNamespace(resource_group="owned"),
    )
    monkeypatch.setattr(demo, "_wait_for_replica", lambda *_args, **_kwargs: "new-revision")
    monkeypatch.setattr(demo, "_wait_for_restoration", lambda *_args: "restored-revision")
    updates = []

    def run(args, **kwargs):
        if args[:2] == ["containerapp", "show"]:
            return {"properties": {
                "template": {"scale": {"minReplicas": 0, "maxReplicas": 1}},
                "configuration": {"ingress": {"external": False}, "activeRevisionsMode": "Single"},
                "latestReadyRevisionName": "original-revision",
            }}
        if args[:2] == ["deployment", "group"]:
            return []
        if args[:2] == ["containerapp", "update"]:
            assert kwargs["allow_write"] is True
            updates.append(args[-1])
            return {}
        if args[:2] == ["containerapp", "exec"]:
            assert kwargs["parse_json"] is False
            assert "FIELD_DATA_DIR=/tmp/contoso-field-smoke-" in args[-1]
            raise demo.DemoError("execution failed")
        raise AssertionError(args)

    monkeypatch.setattr(demo.azure_cli, "run", run)
    with pytest.raises(demo.DemoError, match="execution failed"):
        demo.rehearse_field(tmp_path, tmp_path, enabled_modules=[])
    assert updates == ["1", "0"]
    assert (tmp_path / "field-restored.json").exists()
    assert not (tmp_path / "summary.json").exists()
