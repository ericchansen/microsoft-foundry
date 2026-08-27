"""Shared fixtures.

Nothing in the test suite may touch the network or the Azure CLI. Everything that
would is either mocked or driven by fixture data, so CI is deterministic and a
contributor without an Azure login can still run the full suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_dir(repo_root: Path) -> Path:
    return repo_root / "config"
