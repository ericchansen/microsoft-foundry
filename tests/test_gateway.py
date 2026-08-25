from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from contoso_foundry import gateway


def test_gateway_config_has_safe_project_defaults(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")

    assert set(config.projects) == {"travel", "support", "research", "platform"}
    assert config.projects["travel"].tokens_per_minute == 20_000
    assert config.projects["platform"].total_token_quota == 500_000
    assert all(limit.total_token_quota >= limit.tokens_per_minute for limit in config.projects.values())


def test_model_allow_list_is_exact_and_blocks_preview(repo_root: Path):
    config = gateway.load_config(repo_root / "config" / "gateway.yaml")

    assert config.allowed_publishers == ()
    assert all(asset.endswith("/") for asset in config.allowed_asset_ids)
    for model in ("gpt-5.4-mini", "gpt-4.1-mini", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra"):
        assert any(f"/{model}/" in asset for asset in config.allowed_asset_ids)
    assert config.only_allow_direct_from_azure
    assert config.deny_preview_models


def test_token_fragment_is_valid_and_contains_both_limits():
    limits = gateway.ProjectLimits(100, 1_000, "Monthly")
    policy = gateway.render_token_fragment("travel", limits)
    node = ET.fromstring(policy).find("llm-token-limit")

    assert node is not None
    assert node.attrib["counter-key"] == "project:travel"
    assert node.attrib["tokens-per-minute"] == "100"
    assert node.attrib["token-quota"] == "1000"
    assert node.attrib["token-quota-period"] == "Monthly"


def test_limit_overrun_status_contract():
    assert gateway.expected_overrun_status("tokens_per_minute") == 429
    assert gateway.expected_overrun_status("total_token_quota") == 403
    with pytest.raises(gateway.GatewayConfigError):
        gateway.expected_overrun_status("requests")


def test_config_rejects_prefix_ambiguous_asset_id(tmp_path: Path):
    path = tmp_path / "gateway.yaml"
    path.write_text(
        """
version: 1
projects:
  travel:
    tokens_per_minute: 1
    total_token_quota: 2
    quota_period: Monthly
model_governance:
  allowed_publishers: []
  allowed_asset_ids:
    - azureml://registries/azure-openai/models/gpt-5
  only_allow_direct_from_azure: true
  deny_preview_models: true
""",
        encoding="utf-8",
    )

    with pytest.raises(gateway.GatewayConfigError, match="end in '/'"):
        gateway.load_config(path)
