"""Security and deployment contracts for the Contoso Support hosted agent."""

from __future__ import annotations

import asyncio
import contextvars
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from contoso_foundry.data import build as build_mod
from contoso_foundry.support_agent.identity import PrincipalAllowlist, RequestIdentityBinding
from contoso_foundry.support_agent.tools import CanonicalDataStore, ScopedToolSessionFactory, SupportToolDispatcher
from contoso_foundry.toolbox.identity import UnknownPrincipalError

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "config" / "toolbox"


@dataclass(frozen=True)
class FakeRequestContext:
    user_id: str | None


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    result = build_mod.build(
        config_path=REPO_ROOT / "config" / "data-spine.yaml",
        seed_dir=REPO_ROOT / "data" / "seed",
        out_dir=tmp_path_factory.mktemp("support-agent"),
        fixtures_dir=REPO_ROOT / "data" / "fixtures",
    )
    return result.root / "contoso.db"


def _sessions(
    database: Path,
    current_user: contextvars.ContextVar[str | None],
    mapping: dict[str, str],
) -> ScopedToolSessionFactory:
    allowlist = PrincipalAllowlist.from_json(json.dumps(mapping), tenant_key="TID-CONTOSO-01")
    binding = RequestIdentityBinding(
        allowlist,
        lambda: FakeRequestContext(current_user.get()),
        trust_getter=lambda: True,
    )
    return ScopedToolSessionFactory(
        CanonicalDataStore(database_path=database),
        binding,
        contracts_dir=CONTRACTS_DIR,
    )


def test_unified_config_uses_current_hosted_responses_contract() -> None:
    config = yaml.safe_load((REPO_ROOT / "azure.yaml").read_text(encoding="utf-8"))
    agent = config["services"]["contoso-support"]
    assert agent["host"] == "azure.ai.agent"
    assert agent["kind"] == "hosted"
    assert agent["protocols"] == [{"protocol": "responses", "version": "2.0.0"}]
    assert agent["agentEndpoint"]["authorizationSchemes"] == [{"type": "Entra"}]
    assert not (REPO_ROOT / "agent.yaml").exists()
    assert not (REPO_ROOT / "agent.manifest.yaml").exists()


def test_hosted_config_emits_distinct_nonsensitive_genai_telemetry() -> None:
    agent = yaml.safe_load((REPO_ROOT / "azure.yaml").read_text(encoding="utf-8"))["services"]["contoso-support"]
    assert agent["env"]["OTEL_SERVICE_NAME"] == "contoso-support"
    assert agent["env"]["ENABLE_INSTRUMENTATION"] == "true"
    assert agent["env"]["ENABLE_SENSITIVE_DATA"] == "false"


@pytest.mark.parametrize("raw", ["", "[]", "{", '{"bad user": "OID-AMER-SUPLEAD-01"}', '{"user": "bad"}'])
def test_malformed_server_principal_mapping_fails_closed(raw: str) -> None:
    with pytest.raises(UnknownPrincipalError, match="server principal mapping is malformed"):
        PrincipalAllowlist.from_json(raw, tenant_key="TID-CONTOSO-01")


@pytest.mark.parametrize("user_id", [None, "", "unknown-user", "bad user", "OID-APAC-HRBP-01"])
def test_missing_unknown_and_forged_identity_fail_closed(user_id: str | None) -> None:
    allowlist = PrincipalAllowlist.from_json(
        '{"foundry-support-user":"OID-AMER-SUPLEAD-01"}',
        tenant_key="TID-CONTOSO-01",
    )
    binding = RequestIdentityBinding(
        allowlist,
        lambda: FakeRequestContext(user_id),
        trust_getter=lambda: True,
    )
    with pytest.raises(UnknownPrincipalError, match="principal could not be resolved"):
        binding.resolve()


def test_untrusted_direct_context_is_rejected_even_for_allowlisted_user() -> None:
    allowlist = PrincipalAllowlist.from_json(
        '{"foundry-support-user":"OID-AMER-SUPLEAD-01"}',
        tenant_key="TID-CONTOSO-01",
    )
    binding = RequestIdentityBinding(
        allowlist,
        lambda: FakeRequestContext("foundry-support-user"),
        trust_getter=lambda: False,
    )
    with pytest.raises(UnknownPrincipalError, match="trusted hosted-agent identity"):
        binding.resolve()


def test_support_dispatcher_exposes_only_support_dependencies(database: Path) -> None:
    current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)
    sessions = _sessions(database, current_user, {"foundry-support-user": "OID-AMER-SUPLEAD-01"})
    dispatcher = SupportToolDispatcher(sessions)
    token = current_user.set("foundry-support-user")
    try:
        assert isinstance(dispatcher.call("support_search_cases", {"limit": 5}), list)
        with pytest.raises(PermissionError, match="not allowed"):
            dispatcher.call("hr_search_roster", {"limit": 5})
    finally:
        current_user.reset(token)


def test_cross_region_case_id_remains_invisible(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        case_id = connection.execute(
            "SELECT sc.case_id FROM support_cases sc "
            "JOIN customers c ON c.customer_id = sc.customer_id WHERE c.region = 'APAC' ORDER BY sc.case_id LIMIT 1"
        ).fetchone()[0]

    current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)
    dispatcher = SupportToolDispatcher(
        _sessions(database, current_user, {"foundry-support-user": "OID-AMER-SUPLEAD-01"})
    )
    token = current_user.set("foundry-support-user")
    try:
        assert dispatcher.call("support_lookup_case", {"case_id": case_id}) is None
    finally:
        current_user.reset(token)


def test_concurrent_emea_and_apac_requests_never_share_principal_state(database: Path) -> None:
    current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)
    sessions = _sessions(
        database,
        current_user,
        {
            "foundry-emea-user": "OID-EMEA-HRBP-01",
            "foundry-apac-user": "OID-APAC-HRBP-01",
        },
    )

    async def invoke(user_id: str) -> set[str]:
        token = current_user.set(user_id)
        try:
            await asyncio.sleep(0)
            rows = await asyncio.to_thread(sessions.call, "hr_search_roster", {"limit": 200})
            return {str(row["region"]) for row in rows}
        finally:
            current_user.reset(token)

    async def run() -> tuple[set[str], set[str]]:
        emea, apac = await asyncio.gather(
            invoke("foundry-emea-user"),
            invoke("foundry-apac-user"),
        )
        return emea, apac

    assert asyncio.run(run()) == ({"EMEA"}, {"APAC"})


def test_identity_is_resolved_for_every_tool_call(database: Path) -> None:
    current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user", default=None)
    sessions = _sessions(database, current_user, {"foundry-support-user": "OID-AMER-SUPLEAD-01"})
    token = current_user.set("foundry-support-user")
    try:
        assert sessions.call("support_search_cases", {"limit": 1})
        current_user.set("forged-other-region")
        with pytest.raises(UnknownPrincipalError):
            sessions.call("support_search_cases", {"limit": 1})
    finally:
        current_user.reset(token)
