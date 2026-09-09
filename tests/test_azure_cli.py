from __future__ import annotations

import pytest

from contoso_foundry import azure_cli


@pytest.mark.parametrize(
    "method_args",
    [
        ["--method", "delete"],
        ["--method", "PATCH"],
        ["-m", "post"],
        ["--method=Put"],
    ],
)
def test_rest_mutations_require_explicit_write_authorization(method_args):
    with pytest.raises(azure_cli.AzureCliError):
        azure_cli.run(["rest", *method_args, "--url", "https://example.invalid"])


def test_rest_reads_remain_allowed(monkeypatch):
    monkeypatch.setattr(azure_cli, "executable", lambda: "az")
    monkeypatch.setattr(
        azure_cli.subprocess,
        "run",
        lambda *_, **__: type(
            "Result",
            (),
            {"returncode": 0, "stdout": "{}\n", "stderr": ""},
        )(),
    )

    assert azure_cli.run(
        ["rest", "--method", "get", "--url", "https://example.invalid"]
    ) == {}


def test_raw_console_still_requires_write_authorization():
    with pytest.raises(azure_cli.AzureCliError):
        azure_cli.run(["containerapp", "update"], parse_json=False)


def test_raw_console_preserves_output_and_process_errors(monkeypatch):
    monkeypatch.setattr(azure_cli, "executable", lambda: "az")
    result = type("Result", (), {"returncode": 0, "stdout": "console\n{}\n", "stderr": ""})()
    monkeypatch.setattr(azure_cli.subprocess, "run", lambda *_, **__: result)
    assert azure_cli.run(["containerapp", "exec"], parse_json=False) == "console\n{}\n"
    result.returncode = 1
    result.stderr = "exec failed"
    with pytest.raises(azure_cli.AzureCliError, match="exec failed"):
        azure_cli.run(["containerapp", "exec"], parse_json=False)
