"""Request-scoped adapters from the hosted agent to the canonical Toolbox."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Any

from contoso_foundry.data import build as build_mod
from contoso_foundry.support_agent.identity import RequestIdentityBinding
from contoso_foundry.toolbox.tools import Toolbox

SUPPORT_TOOL_NAMES = frozenset(
    {
        "support_lookup_case",
        "support_search_cases",
        "customer_lookup",
        "catalog_lookup_product",
        "catalog_check_stock",
    }
)


class CanonicalDataStore:
    """Build the immutable synthetic spine once and open a connection per call."""

    def __init__(
        self,
        *,
        database_path: Path,
        spine_config: Path | None = None,
        seed_dir: Path | None = None,
        fixtures_dir: Path | None = None,
        expected_sha256: str | None = None,
    ) -> None:
        self._database_path = database_path
        self._spine_config = spine_config
        self._seed_dir = seed_dir
        self._fixtures_dir = fixtures_dir
        self._expected_sha256 = expected_sha256
        self._build_lock = threading.Lock()

    def _verify_digest(self) -> None:
        if self._expected_sha256 is None:
            return
        actual = hashlib.sha256(self._database_path.read_bytes()).hexdigest()
        if actual != self._expected_sha256:
            raise RuntimeError("the canonical support database failed its packaged integrity check")

    def ensure_database(self) -> Path:
        if self._database_path.is_file():
            self._verify_digest()
            return self._database_path
        if self._spine_config is None or self._seed_dir is None or self._fixtures_dir is None:
            raise FileNotFoundError(f"canonical database does not exist: {self._database_path}")

        with self._build_lock:
            if not self._database_path.is_file():
                result = build_mod.build(
                    config_path=self._spine_config,
                    seed_dir=self._seed_dir,
                    out_dir=self._database_path.parent,
                    fixtures_dir=self._fixtures_dir,
                )
                if result.root / "contoso.db" != self._database_path:
                    raise RuntimeError("the canonical data build wrote an unexpected database path")
        self._verify_digest()
        return self._database_path

    def connect(self) -> sqlite3.Connection:
        database_path = self.ensure_database().resolve().as_posix()
        connection = sqlite3.connect(f"file:{database_path}?mode=ro&immutable=1", uri=True)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        return connection


class ScopedToolSessionFactory:
    """Create a fresh identity-bound Toolbox for every request or tool call."""

    def __init__(
        self,
        data_store: CanonicalDataStore,
        identity_binding: RequestIdentityBinding,
        *,
        contracts_dir: Path,
        minimum_cohort: int = 5,
    ) -> None:
        self._data_store = data_store
        self._identity_binding = identity_binding
        self._contracts_dir = contracts_dir
        self._minimum_cohort = minimum_cohort

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        principal = self._identity_binding.resolve()
        connection = self._data_store.connect()
        try:
            toolbox = Toolbox(
                connection,
                principal,
                contracts_dir=self._contracts_dir,
                minimum_cohort=self._minimum_cohort,
            )
            return toolbox.call(name, arguments or {})
        finally:
            connection.close()


class SupportToolDispatcher:
    """Expose only the canonical capabilities needed by Contoso Support."""

    def __init__(self, sessions: ScopedToolSessionFactory) -> None:
        self._sessions = sessions

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in SUPPORT_TOOL_NAMES:
            raise PermissionError(f"the Contoso Support agent is not allowed to call {name!r}")
        return self._sessions.call(name, arguments)
