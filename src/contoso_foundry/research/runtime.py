"""Fail-closed construction of the canonical research runtime."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from contoso_foundry.data.build import build, compare_lock, read_lock
from contoso_foundry.toolbox.identity import principal_from_fixture
from contoso_foundry.toolbox.tools import Toolbox

from .workflow import AGENT_VERSION

PERSONA_ROUTES = {
    "americas-supply-planner": ("OID-AMER-PLANNER-01", "TID-CONTOSO-01"),
    "americas-support-lead": ("OID-AMER-SUPLEAD-01", "TID-CONTOSO-01"),
}


@dataclass
class ResearchRuntime:
    connection: sqlite3.Connection
    toolbox: Toolbox

    def close(self) -> None:
        self.connection.close()


def build_runtime(repo_root: Path, *, persona_route: str, expected_version: str) -> ResearchRuntime:
    """Rebuild and verify the canonical data before exposing any tool."""
    if expected_version != AGENT_VERSION:
        raise RuntimeError(f"agent route requires version {AGENT_VERSION}, not {expected_version!r}")
    principal = PERSONA_ROUTES.get(persona_route)
    if principal is None:
        raise RuntimeError(f"unknown research persona route {persona_route!r}")

    runtime_root = Path(tempfile.mkdtemp(prefix="contoso-research-"))
    result = build(
        config_path=repo_root / "config" / "data-spine.yaml",
        seed_dir=repo_root / "data" / "seed",
        out_dir=runtime_root,
        fixtures_dir=repo_root / "data" / "fixtures",
    )
    differences = compare_lock(read_lock(repo_root / "data" / "build.lock.json"), result.lock)
    if differences:
        raise RuntimeError("canonical data verification failed: " + "; ".join(differences))

    connection = sqlite3.connect(result.root / "contoso.db", check_same_thread=False)
    connection.execute("PRAGMA foreign_keys = ON")
    toolbox = Toolbox(
        connection,
        principal_from_fixture(*principal),
        contracts_dir=repo_root / "config" / "toolbox",
    )
    return ResearchRuntime(connection=connection, toolbox=toolbox)


def runtime_from_environment(repo_root: Path) -> ResearchRuntime:
    route = os.environ.get("CONTOSO_RESEARCH_PERSONA_ROUTE", "")
    version = os.environ.get("CONTOSO_RESEARCH_VERSION", "")
    if not route or not version:
        raise RuntimeError("CONTOSO_RESEARCH_PERSONA_ROUTE and CONTOSO_RESEARCH_VERSION are required")
    return build_runtime(repo_root, persona_route=route, expected_version=version)
