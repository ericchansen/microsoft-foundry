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
from contoso_foundry.support_agent.deployment import (
    DeploymentVerificationError,
    verify_deployment,
)
from contoso_foundry.support_agent.evaluation import SupportEvaluationError
from contoso_foundry.support_agent.evaluation import evaluate as evaluate_support
from contoso_foundry.support_agent.identity import PrincipalAllowlist, RequestIdentityBinding
from contoso_foundry.support_agent.tools import CanonicalDataStore, ScopedToolSessionFactory, SupportToolDispatcher
from contoso_foundry.toolbox.identity import UnknownPrincipalError

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "config" / "toolbox"
FOUNDRY_TEST_ENDPOINT = (
    "https://" + "unit-test" + ".services.ai.azure.com/api/projects/contoso"
)


@dataclass(frozen=True)
class FakeRequestContext:
    user_id: str | None
    call_id: str | None = "call-0001"


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
    binding = RequestIdentityBinding.from_allowlist(
        allowlist,
        lambda: FakeRequestContext(current_user.get(), call_id=f"call-{current_user.get()}"),
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
    assert config["services"]["support-project"]["endpoint"] == "${AZURE_AI_PROJECT_ENDPOINT}"
    assert agent["uses"] == ["support-project"]
    assert agent["env"]["CONTOSO_SUPPORT_PRINCIPAL_OID"] == "OID-AMER-SUPLEAD-01"
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" not in agent["env"]
    assert config["infra"] == {"provider": "bicep", "path": "./infra"}
    assert config["requiredVersions"]["extensions"]["azure.ai.agents"] == ">=1.0.0-beta.8"
    assert "azure.ai.connections" not in config["requiredVersions"]["extensions"]
    assert agent["docker"] == {
        "path": "./Dockerfile",
        "context": ".",
        "platform": "linux/amd64",
        "remoteBuild": False,
    }
    assert agent["image"] == "${CONTOSO_SUPPORT_IMAGE}"
    assert (REPO_ROOT / "Dockerfile").is_file()
    assert not (REPO_ROOT / "agents" / "contoso-support" / "Dockerfile").exists()
    assert not (REPO_ROOT / "agent.yaml").exists()
    assert not (REPO_ROOT / "agent.manifest.yaml").exists()


def test_hosted_config_emits_distinct_nonsensitive_genai_telemetry() -> None:
    agent = yaml.safe_load((REPO_ROOT / "azure.yaml").read_text(encoding="utf-8"))["services"]["contoso-support"]
    assert agent["env"]["OTEL_SERVICE_NAME"] == "contoso-support"
    assert agent["env"]["ENABLE_INSTRUMENTATION"] == "true"
    assert agent["env"]["ENABLE_SENSITIVE_DATA"] == "false"


def test_container_packages_digest_checked_read_only_canonical_data() -> None:
    runtime = (REPO_ROOT / "src" / "contoso_foundry" / "support_agent" / "runtime.py").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "Path.home" not in runtime
    assert "CONTOSO_DATA_DIR" not in runtime
    assert 'Path("/opt/contoso-support/contoso.db")' in runtime
    assert "contoso.db.sha256" in runtime
    assert "data build --out /tmp/contoso-build" in dockerfile
    assert "chmod 0444" in dockerfile
    assert "HOME=/var/lib/contoso-support" in dockerfile
    assert "chown 65532:65532 /var/lib/contoso-support" in dockerfile


def test_live_workflow_pins_shared_registry_image_and_never_supplies_identity_header() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "support-agent.yml").read_text(encoding="utf-8")
    ci_workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "runs-on: ubuntu-latest" in workflow
    assert "foundry boundary" in workflow
    assert "foundry support verify-deployment" in workflow
    assert "AGENT_CONTOSO_SUPPORT_RESPONSES_ENDPOINT" in workflow
    assert "docker push" in workflow
    assert "buildx imagetools inspect" in workflow
    assert "CONTOSO_SUPPORT_IMAGE" in workflow
    assert "AZD_AGENT_SKIP_ACR true" in workflow
    assert "x-agent-user-id" not in workflow.lower()
    assert '${{ inputs.confirm_resource_group }}' not in workflow.split("run: |", maxsplit=1)[1]
    assert "cloud_RoleName == 'contoso-support'" in workflow
    assert 'echo "FOUNDRY_LOCATION=' in workflow
    assert 'azd env set AZURE_LOCATION "$FOUNDRY_LOCATION"' in workflow
    assert "azd deploy contoso-support --no-prompt" in workflow
    assert "--enable-module optional-control-plane" in workflow
    assert "azd ai agent doctor --local-only" in ci_workflow
    assert "azd env new support-agent-doctor --no-prompt" in ci_workflow
    assert "azure.ai.connections" not in ci_workflow
    assert "CONTOSO_PRINCIPAL_MAP_JSON" not in workflow


def test_every_workflow_supplies_the_model_deployment_azure_yaml_expects() -> None:
    """`azure.yaml` reads the deployment name from the azd environment.

    A fresh `azd env new` carries nothing, so a workflow that forgets to set it
    deploys an agent bound to an empty deployment name. The value must also be
    the deployment `azure.yaml` actually creates, or the agent points at one
    that does not exist.
    """
    unified = yaml.safe_load((REPO_ROOT / "azure.yaml").read_text(encoding="utf-8"))
    deployment = unified["services"]["support-project"]["deployments"][0]["name"]
    assert unified["services"]["contoso-support"]["env"][
        "MICROSOFT_FOUNDRY_MODEL_DEPLOYMENT_NAME"
    ] == "${MICROSOFT_FOUNDRY_MODEL_DEPLOYMENT_NAME}"

    for name in ("support-agent.yml", "ci.yml"):
        workflow = (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert f"MICROSOFT_FOUNDRY_MODEL_DEPLOYMENT_NAME {deployment}" in workflow, name


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
    binding = RequestIdentityBinding.from_allowlist(
        allowlist,
        lambda: FakeRequestContext(user_id),
    )
    with pytest.raises(UnknownPrincipalError, match="principal could not be resolved"):
        binding.resolve()


def test_hosted_binding_ignores_ambient_user_id(monkeypatch) -> None:
    monkeypatch.setenv("CONTOSO_SUPPORT_PRINCIPAL_OID", "OID-AMER-SUPLEAD-01")
    monkeypatch.setenv("CONTOSO_TENANT_KEY", "TID-CONTOSO-01")
    binding = RequestIdentityBinding.from_environment(
        lambda: FakeRequestContext("OID-APAC-HRBP-01")
    )
    principal = binding.resolve()
    assert principal.oid == "OID-AMER-SUPLEAD-01"
    assert principal.tid == "TID-CONTOSO-01"


@pytest.mark.parametrize("oid", ["", "bad", "OID bad"])
def test_hosted_binding_requires_a_canonical_server_principal(monkeypatch, oid: str) -> None:
    monkeypatch.setenv("CONTOSO_SUPPORT_PRINCIPAL_OID", oid)
    monkeypatch.setenv("CONTOSO_TENANT_KEY", "TID-CONTOSO-01")
    with pytest.raises(UnknownPrincipalError, match="server-bound support principal"):
        RequestIdentityBinding.from_environment(lambda: FakeRequestContext("ignored"))


@pytest.mark.parametrize("call_id", [None, "", "bad call", "\x00"])
def test_missing_or_malformed_platform_call_id_fails_closed(call_id: str | None) -> None:
    allowlist = PrincipalAllowlist.from_json(
        '{"foundry-support-user":"OID-AMER-SUPLEAD-01"}',
        tenant_key="TID-CONTOSO-01",
    )
    binding = RequestIdentityBinding.from_allowlist(
        allowlist,
        lambda: FakeRequestContext("foundry-support-user", call_id=call_id),
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
        assert dispatcher.call("customer_lookup", {"customer_id": "CUST-00002"}) is not None
        assert dispatcher.call("catalog_lookup_product", {"product_id": "PROD-0001"}) is not None
        with pytest.raises(PermissionError, match="not allowed"):
            dispatcher.call("support_update_case", {"case_id": "CASE-00005", "status": "closed"})
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


def test_canonical_database_is_opened_read_only(database: Path) -> None:
    store = CanonicalDataStore(database_path=database)
    with store.connect() as connection, pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute("UPDATE support_cases SET status = 'closed'")


def test_canonical_database_digest_mismatch_is_fatal(database: Path) -> None:
    store = CanonicalDataStore(database_path=database, expected_sha256="0" * 64)
    with pytest.raises(RuntimeError, match="integrity check"):
        store.connect()


def test_deterministic_evaluation_proves_scoped_outcomes(database: Path) -> None:
    results = evaluate_support(
        database_path=database,
        config_path=REPO_ROOT / "config" / "support-agent" / "evaluations.yaml",
        contracts_dir=CONTRACTS_DIR,
    )
    assert [result.id for result in results] == [
        "support-can-read-visible-amer-case",
        "support-cannot-read-apac-case-by-id",
        "emea-and-apac-remain-distinct",
        "apac-does-not-inherit-prior-request-scope",
        "unknown-platform-principal-fails-closed",
    ]


def test_evaluation_dependency_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="canonical database does not exist"):
        evaluate_support(
            database_path=tmp_path / "missing.db",
            config_path=REPO_ROOT / "config" / "support-agent" / "evaluations.yaml",
            contracts_dir=CONTRACTS_DIR,
        )


def test_evaluation_assertion_failure_is_fatal(database: Path, tmp_path: Path) -> None:
    config = yaml.safe_load(
        (REPO_ROOT / "config" / "support-agent" / "evaluations.yaml").read_text(encoding="utf-8")
    )
    config["scenarios"] = [config["scenarios"][0]]
    config["scenarios"][0]["expect"]["value"] = "CASE-99999"
    path = tmp_path / "evaluations.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(SupportEvaluationError, match="unexpected evidence"):
        evaluate_support(
            database_path=database,
            config_path=path,
            contracts_dir=CONTRACTS_DIR,
        )


def test_deployment_verifier_requires_active_exclusive_responses_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        {
            "agent_endpoint": {
                "version_selector": {
                    "version_selection_rules": [
                        {
                            "agent_version": "7",
                            "traffic_percentage": 100,
                            "type": "FixedRatio",
                        }
                    ]
                },
                "protocol_configuration": {"responses": {}},
            }
        },
        {
            "status": "active",
            "definition": {
                "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}]
            },
        },
    ]
    monkeypatch.setattr(
        "contoso_foundry.support_agent.deployment.azure_cli.run",
        lambda _args: responses.pop(0),
    )

    evidence = verify_deployment(
        project_endpoint=FOUNDRY_TEST_ENDPOINT,
        agent_name="contoso-support",
        expected_version="7",
    )
    assert evidence.version == "7"
    assert evidence.traffic_percentage == 100


@pytest.mark.parametrize(
    ("status", "rules", "message"),
    [
        ("failed", [{"agent_version": "7", "traffic_percentage": 100, "type": "FixedRatio"}], "not active"),
        ("active", [{"agent_version": "6", "traffic_percentage": 100, "type": "FixedRatio"}], "route exactly"),
        ("active", [{"agent_version": "7", "traffic_percentage": 50, "type": "FixedRatio"}], "route exactly"),
    ],
)
def test_deployment_verifier_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    rules: list[dict[str, object]],
    message: str,
) -> None:
    responses = [
        {
            "agent_endpoint": {
                "version_selector": {"version_selection_rules": rules},
                "protocol_configuration": {"responses": {}},
            }
        },
        {
            "status": status,
            "definition": {
                "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}]
            },
        },
    ]
    monkeypatch.setattr(
        "contoso_foundry.support_agent.deployment.azure_cli.run",
        lambda _args: responses.pop(0),
    )
    with pytest.raises(DeploymentVerificationError, match=message):
        verify_deployment(
            project_endpoint=FOUNDRY_TEST_ENDPOINT,
            agent_name="contoso-support",
            expected_version="7",
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.example/api/projects/contoso",
        FOUNDRY_TEST_ENDPOINT + "?redirect=https://attacker.example",
        "https://" + "unit-test" + ".services.ai.azure.com.evil.example/api/projects/contoso",
    ],
)
def test_deployment_verifier_rejects_untrusted_endpoints(endpoint: str) -> None:
    with pytest.raises(DeploymentVerificationError, match="Foundry project endpoint"):
        verify_deployment(
            project_endpoint=endpoint,
            agent_name="contoso-support",
            expected_version="7",
        )
