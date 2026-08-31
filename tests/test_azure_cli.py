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
