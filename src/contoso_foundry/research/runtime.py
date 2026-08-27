"""Fail-closed shared data and request-scoped Toolbox construction."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from contoso_foundry.data.build import build, compare_lock, read_lock
from contoso_foundry.toolbox.contracts import load_contracts
from contoso_foundry.toolbox.identity import principal_from_fixture
from contoso_foundry.toolbox.tools import Toolbox

from .request_context import HostedIdentityError, TrustedRequestContext
from .workflow import AGENT_VERSION

PERSONA_ROUTES = MappingProxyType(
    {
        "americas-supply-planner": ("OID-AMER-PLANNER-01", "TID-CONTOSO-01"),
        "americas-support-lead": ("OID-AMER-SUPLEAD-01", "TID-CONTOSO-01"),
        "emea-travel-coordinator": ("OID-EMEA-TRAVEL-01", "TID-CONTOSO-01"),
    }
)


@dataclass
class ResearchRuntime:
    database_path: Path
    contracts_dir: Path
    contract_versions: dict[str, str]
    _temporary_directory: tempfile.TemporaryDirectory[str]

    def close(self) -> None:
        self._temporary_directory.cleanup()

    @contextmanager
    def open_toolbox(self, request: TrustedRequestContext) -> Iterator[Toolbox]:
        """Open an isolated read-only data session for one invocation."""
        fixture_identity = PERSONA_ROUTES.get(request.caller_route)
        if fixture_identity is None:
            raise HostedIdentityError("hosted request identity is not authorized")

        connection = sqlite3.connect(
            f"{self.database_path.resolve().as_uri()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield Toolbox(
                connection,
                principal_from_fixture(*fixture_identity),
                contracts_dir=self.contracts_dir,
            )
        finally:
            connection.close()


def build_runtime(repo_root: Path, *, expected_version: str) -> ResearchRuntime:
    """Rebuild and verify canonical data before accepting hosted requests."""
    if expected_version != AGENT_VERSION:
        raise RuntimeError(f"agent route requires version {AGENT_VERSION}, not {expected_version!r}")

    temporary_directory = tempfile.TemporaryDirectory(prefix="contoso-research-")
    runtime_root = Path(temporary_directory.name)
    try:
        result = build(
            config_path=repo_root / "config" / "data-spine.yaml",
            seed_dir=repo_root / "data" / "seed",
            out_dir=runtime_root,
            fixtures_dir=repo_root / "data" / "fixtures",
        )
        differences = compare_lock(read_lock(repo_root / "data" / "build.lock.json"), result.lock)
        if differences:
            raise RuntimeError("canonical data verification failed: " + "; ".join(differences))

        contracts_dir = repo_root / "config" / "toolbox"
        contract_versions = {
            contract.capability: contract.version for contract in load_contracts(contracts_dir)
        }
        return ResearchRuntime(
            database_path=result.root / "contoso.db",
            contracts_dir=contracts_dir,
            contract_versions=contract_versions,
            _temporary_directory=temporary_directory,
        )
    except Exception:
        temporary_directory.cleanup()
        raise


def runtime_from_environment(repo_root: Path) -> ResearchRuntime:
    version = os.environ.get("CONTOSO_RESEARCH_VERSION", "")
    if not version:
        raise RuntimeError("CONTOSO_RESEARCH_VERSION is required")
    return build_runtime(repo_root, expected_version=version)
