"""Repository contracts for the tenant-neutral Contoso Concierge ALM scaffold."""

from __future__ import annotations

import json
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
import yaml

from contoso_foundry import patterns, scan
from contoso_foundry.concierge_alm import (
    _validate_runtime_response,
    run_synthetic_acceptance,
    validate_release_source,
    validate_solution_identifier_fields,
    validate_solution_package,
    validate_source_run,
)
from contoso_foundry.data import build as build_mod
from contoso_foundry.toolbox.contracts import FORBIDDEN_PARAMETERS, load_contract
from contoso_foundry.toolbox.identity import (
    IdentityResolver,
    Principal,
    UnknownPrincipalError,
    principal_from_fixture,
)
from contoso_foundry.toolbox.tools import Toolbox


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def concierge_database(repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("concierge-spine")
    result = build_mod.build(
        config_path=repo_root / "config" / "data-spine.yaml",
        seed_dir=repo_root / "data" / "seed",
        out_dir=output,
        fixtures_dir=repo_root / "data" / "fixtures",
    )
    return result.root / "contoso.db"


@pytest.fixture
def concierge_connection(concierge_database: Path):
    connection = sqlite3.connect(concierge_database)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def toolbox_for(repo_root: Path, connection: sqlite3.Connection, oid: str, tid: str) -> Toolbox:
    return Toolbox(
        connection,
        principal_from_fixture(oid, tid),
        contracts_dir=repo_root / "config" / "toolbox",
    )


def test_solution_pins_one_managed_artifact_for_test_and_prod(repo_root: Path):
    alm = load_yaml(repo_root / "config" / "concierge" / "alm.yaml")
    solution = alm["solution"]
    promotion = alm["promotion"]

    assert solution["source_type"] == "unmanaged"
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", solution["accepted_version"])
    assert solution["managed_artifact"] == (
        f"ContosoConcierge_{solution['accepted_version'].replace('.', '_')}_managed.zip"
    )
    assert solution["minimum_retention_days"] == 90
    rollback = solution["rollback"]
    assert rollback["policy"] == "forward-fix-or-authorized-controlled-reinstall"
    assert rollback["automatic_rollback"] is False
    assert rollback["retain_managed_zip_for_evidence"] is True
    assert rollback["require_prior_artifact_attestation"] is True
    assert rollback["require_environment_backup"] is True
    assert rollback["require_dependency_and_data_inventory"] is True
    assert rollback["require_data_loss_acknowledgement"] is True
    assert promotion["managed_artifact_order"] == ["TEST", "PROD"]
    assert promotion["build_once"] is True
    assert promotion["rebuild_between_targets"] is False

    environments = alm["environments"]
    assert environments["DEV"]["solution_type"] == "unmanaged"
    assert environments["TEST"]["solution_type"] == "managed"
    assert environments["PROD"]["solution_type"] == "managed"


def test_solution_source_layout_and_version_topic_are_consistent(repo_root: Path):
    alm = load_yaml(repo_root / "config" / "concierge" / "alm.yaml")
    source = repo_root / alm["solution"]["source_path"]

    required = {
        "Other/Solution.xml",
        "Other/Customizations.xml",
        "environmentvariabledefinitions/ccs_AcceptedVersion/environmentvariabledefinition.xml",
        "environmentvariabledefinitions/ccs_FoundrySpecialistBaseUrl/environmentvariabledefinition.xml",
    }
    assert required <= {path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file()}

    manifest = ET.parse(source / "Other" / "Solution.xml").getroot()
    assert manifest.attrib["generatedBy"] == "CrmLive"
    assert manifest.findtext("./SolutionManifest/UniqueName") == "ContosoConcierge"
    assert manifest.findtext("./SolutionManifest/Version") == alm["solution"]["accepted_version"]
    assert manifest.findtext("./SolutionManifest/Managed") == "0"

    topic = load_yaml(
        repo_root
        / "solutions"
        / "ContosoConcierge"
        / "spec"
        / "agents"
        / "contoso-concierge"
        / "topics"
        / "version.topic.yaml"
    )
    assert topic["kind"] == "AdaptiveDialog"
    assert "/version" in topic["beginDialog"]["intent"]["triggerQueries"]
    activity = topic["beginDialog"]["actions"][0]["activity"]
    assert "Env.ccs_AcceptedVersion" in activity
    assert "environment" not in activity.lower()
    assert "tenant" not in activity.lower()


def test_deployment_settings_hold_placeholders_not_live_values(repo_root: Path):
    alm = load_yaml(repo_root / "config" / "concierge" / "alm.yaml")

    for target in ("DEV", "TEST", "PROD"):
        settings_path = repo_root / alm["promotion"]["deployment_settings"][target]
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        variables = {item["SchemaName"]: item["Value"] for item in settings["EnvironmentVariables"]}
        references = {item["LogicalName"]: item for item in settings["ConnectionReferences"]}

        assert variables["ccs_AcceptedVersion"] == alm["solution"]["accepted_version"]
        assert variables["ccs_FoundrySpecialistBaseUrl"].startswith("<")
        assert references["ccs_FoundrySpecialist"]["ConnectionId"].startswith("<")
        assert references["ccs_FoundrySpecialist"]["ConnectorId"].startswith("<")

    assert alm["configuration"]["live_values_in_git"] is False
    assert alm["configuration"]["secrets_in_deployment_settings"] is False


def test_telemetry_contract_protects_human_conversations(repo_root: Path):
    telemetry = load_yaml(repo_root / "config" / "concierge" / "alm.yaml")["telemetry"]

    for target in ("DEV", "TEST"):
        assert telemetry[target]["environment_level"] == {
            "enabled": True,
            "preview": True,
            "synthetic_only": True,
        }
        assert telemetry[target]["dataverse_transcripts"]["save"] is False

    prod = telemetry["PROD"]
    assert prod["environment_level"]["enabled"] is False
    assert prod["agent_level"]["enabled"] is True
    assert prod["agent_level"]["log_conversation_details"] is False
    assert prod["agent_level"]["log_sensitive_activity_properties"] is False
    assert prod["dataverse_transcripts"]["save"] is False


def test_synthetic_delegation_cases_execute_toolbox_contracts(
    repo_root: Path,
    concierge_connection: sqlite3.Connection,
):
    suite = load_yaml(repo_root / "config" / "concierge" / "delegation-tests.yaml")
    identities = load_yaml(repo_root / "data" / "seed" / "identities.yaml")["principals"]
    known_personas = {persona["principal_oid"]: persona for persona in identities}
    contracts = {
        path.name: load_contract(path)
        for path in (repo_root / "config" / "toolbox").glob("*.yaml")
    }

    cases = {case["id"]: case for case in suite["cases"]}
    support_case = cases["support-case-lookup-delegates"]
    support_contract = contracts["support.yaml"]
    support_tool = support_contract.tool(support_case["expect"]["tool"])
    assert support_case["persona"]["oid"] in known_personas
    assert support_tool.name == "support_lookup_case"
    for case in cases.values():
        arguments = case["expect"].get("arguments", {})
        assert set(arguments).isdisjoint(FORBIDDEN_PARAMETERS)

    support_result = toolbox_for(
        repo_root,
        concierge_connection,
        support_case["persona"]["oid"],
        support_case["persona"]["tid"],
    ).call(support_case["expect"]["tool"], support_case["expect"]["arguments"])
    assert support_result["case_id"] == support_case["expect"]["arguments"]["case_id"]

    emea = cases["emea-roster-is-scoped"]
    apac = cases["apac-roster-is-scoped"]
    assert emea["prompt"] == apac["prompt"]
    assert emea["expect"]["tool"] == apac["expect"]["tool"] == "hr_search_roster"
    assert set(emea["expect"]["result_regions"]).isdisjoint(apac["expect"]["result_regions"])
    assert emea["persona"]["oid"] in known_personas
    assert apac["persona"]["oid"] in known_personas

    observed = {}
    for case in (emea, apac):
        rows = toolbox_for(
            repo_root,
            concierge_connection,
            case["persona"]["oid"],
            case["persona"]["tid"],
        ).call(case["expect"]["tool"], case["expect"]["arguments"])
        assert rows
        assert {row["region"] for row in rows} == set(case["expect"]["result_regions"])
        observed[case["id"]] = {row["employee_id"] for row in rows}
    assert observed[emea["id"]].isdisjoint(observed[apac["id"]])

    unknown = cases["unknown-persona-fails-closed"]["expect"]
    assert unknown == {
        "error": "unknown-principal",
        "no_delegation": True,
        "no_result_rows": True,
    }
    with pytest.raises(UnknownPrincipalError):
        IdentityResolver(concierge_connection).resolve(
            Principal(
                cases["unknown-persona-fails-closed"]["persona"]["oid"],
                cases["unknown-persona-fails-closed"]["persona"]["tid"],
            )
        )


def test_runtime_publish_and_tenant_approval_stay_manual(repo_root: Path):
    alm = load_yaml(repo_root / "config" / "concierge" / "alm.yaml")
    workflow = (repo_root / ".github" / "workflows" / "concierge-alm.yml").read_text(encoding="utf-8")
    script = (repo_root / "scripts" / "concierge-alm.ps1").read_text(encoding="utf-8")

    assert yaml.safe_load(workflow)
    assert alm["promotion"]["automatic_runtime_publish"] is False
    assert alm["promotion"]["runtime_publish_gate"] == "manual-agent-owner"
    assert alm["promotion"]["tenant_availability_gate"] == "microsoft-365-admin-approval"
    assert "workflow_dispatch:" in workflow
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in workflow
    assert "environment: concierge-prod-approval" in workflow
    assert "accept-test:" in workflow
    assert "Run synthetic TEST delegation and RLS acceptance" in workflow
    assert workflow.index("runtime-test") < workflow.index(
        "Retain TEST-accepted package for PROD"
    )
    assert "ACCEPTED_ARTIFACT_NAME" in workflow
    assert "-ChecksumPath" not in workflow
    assert "retention-days: 90" in workflow
    assert "--publish-changes" not in workflow
    assert "manual TEST runtime publication" in workflow
    assert "environment: concierge-prod-approval" in workflow
    assert "ConvertFrom-Json" in script
    assert "validate-package" in script
    assert "ConnectionId" in script
    assert "ConnectorId" in script


def test_teardown_inventory_covers_every_non_azure_surface(repo_root: Path):
    inventory = set(load_yaml(repo_root / "config" / "concierge" / "alm.yaml")["teardown_inventory"])
    required = {
        "channels",
        "tenant-availability-pins",
        "connection-references",
        "connector-connections",
        "custom-connectors",
        "agent-flows",
        "application-insights-agent-settings",
        "application-insights-environment-export",
        "dataverse-transcript-settings",
        "deployment-service-principal",
        "power-platform-application-users",
        "deployment-client-secrets",
        "deployment-federated-credentials",
        "power-platform-environment-role-assignments",
        "github-environment-secrets",
        "github-deployment-environments",
        "managed-solutions",
        "unmanaged-dev-solution",
        "copilot-credit-capacity-allocations",
        "power-platform-environments",
    }
    assert inventory == required


def test_pac_pack_smoke_and_release_source_gate_are_wired(repo_root: Path):
    workflow = (repo_root / ".github/workflows/concierge-alm.yml").read_text(
        encoding="utf-8"
    )
    script = (repo_root / "scripts/concierge-alm.ps1").read_text(encoding="utf-8")
    tools = json.loads(
        (repo_root / ".config/dotnet-tools.json").read_text(encoding="utf-8")
    )

    pac = tools["tools"]["microsoft.powerapps.cli.tool"]
    assert pac["version"] == "2.11.2"
    assert pac["commands"] == ["pac"]
    assert "generatedBy" in (
        repo_root / "solutions/ContosoConcierge/src/Other/Solution.xml"
    ).read_text(encoding="utf-8")
    assert "PAC pack smoke gate" in workflow
    assert "dotnet tool restore" in workflow
    assert "--managed false" in workflow
    assert "AssertReleaseSource" in workflow
    assert "validate-release-source" in script
    assert "packagetype Unmanaged" in script


def test_promotion_requires_trusted_run_digest_attestation_and_manifest(
    repo_root: Path,
):
    workflow = (repo_root / ".github/workflows/concierge-alm.yml").read_text(
        encoding="utf-8"
    )
    verifier = (
        repo_root
        / ".github/actions/verify-concierge-artifact/action.yml"
    ).read_text(encoding="utf-8")
    assert yaml.safe_load(verifier)
    implementation = workflow + verifier
    alm = load_yaml(repo_root / "config/concierge/alm.yaml")
    promotion = alm["promotion"]

    assert promotion["trusted_branch"] == "main"
    assert promotion["require_exact_source_sha"] is True
    assert promotion["require_artifact_archive_digest"] is True
    assert promotion["require_github_attestation"] is True
    assert promotion["validate_internal_solution_identity"] is True
    assert "artifact_source_sha" in implementation
    assert "validate-source" in implementation
    assert "sha256sum --check" in implementation
    assert "gh attestation verify" in implementation
    assert "actions/attest@" in workflow
    assert "validate-package" in implementation
    assert "actions/download-artifact" not in implementation
    assert workflow.count("uses: ./.github/actions/verify-concierge-artifact") == 3


def test_source_run_validator_binds_artifact_to_exact_successful_main_run():
    run_id = 12345
    sha = "a" * 40
    run = {
        "id": run_id,
        "path": ".github/workflows/concierge-alm.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": sha,
        "repository": {"full_name": "example/repository"},
    }
    artifacts = {
        "artifacts": [
            {
                "id": 67890,
                "name": "expected-artifact",
                "expired": False,
                "digest": f"sha256:{'b' * 64}",
                "workflow_run": {"id": run_id, "head_sha": sha},
            }
        ]
    }

    assert validate_source_run(
        run,
        artifacts,
        run_id=run_id,
        expected_sha=sha,
        expected_artifact="expected-artifact",
        expected_repository="example/repository",
    ) == (67890, "b" * 64)

    for field, untrusted in (
        ("path", ".github/workflows/other.yml"),
        ("event", "pull_request"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("head_branch", "feature"),
        ("head_sha", "c" * 40),
    ):
        changed = {**run, field: untrusted}
        with pytest.raises(ValueError, match=field):
            validate_source_run(
                changed,
                artifacts,
                run_id=run_id,
                expected_sha=sha,
                expected_artifact="expected-artifact",
                expected_repository="example/repository",
            )


def _write_solution_zip(path: Path, *, name: str, version: str, managed: str):
    manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<ImportExportXml>
  <SolutionManifest>
    <UniqueName>{name}</UniqueName>
    <Version>{version}</Version>
    <Managed>{managed}</Managed>
  </SolutionManifest>
</ImportExportXml>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("solution.xml", manifest)


def test_internal_solution_package_identity_and_type_are_enforced(tmp_path: Path):
    package = tmp_path / "ContosoConcierge_1_0_0_0_managed.zip"
    _write_solution_zip(
        package,
        name="ContosoConcierge",
        version="1.0.0.0",
        managed="1",
    )
    validate_solution_package(package, managed=True)

    for name, version, managed in (
        ("OtherSolution", "1.0.0.0", "1"),
        ("ContosoConcierge", "2.0.0.0", "1"),
        ("ContosoConcierge", "1.0.0.0", "0"),
    ):
        _write_solution_zip(
            package,
            name=name,
            version=version,
            managed=managed,
        )
        with pytest.raises(ValueError):
            validate_solution_package(package, managed=True)


def test_runtime_acceptance_validates_delegation_rls_and_fail_closed():
    support = {
        "id": "support",
        "expect": {
            "specialist": "support",
            "tool": "support_lookup_case",
            "arguments": {"case_id": "CASE-00005"},
        },
    }
    _validate_runtime_response(
        support,
        {
            "test_id": "support",
            "delegation": {
                "specialist": "support",
                "tool": "support_lookup_case",
                "arguments": {"case_id": "CASE-00005"},
            },
            "results": [],
        },
    )
    with pytest.raises(ValueError, match="delegation arguments"):
        _validate_runtime_response(
            support,
            {
                "test_id": "support",
                "delegation": {
                    "specialist": "support",
                    "tool": "support_lookup_case",
                    "arguments": {
                        "case_id": "CASE-00005",
                        "oid": "OID-UNTRUSTED",
                    },
                },
                "results": [],
            },
        )
    rls = {
        "id": "emea",
        "expect": {
            "specialist": "hr",
            "tool": "hr_search_roster",
            "result_regions": ["EMEA"],
        },
    }
    _validate_runtime_response(
        rls,
        {
            "test_id": "emea",
            "delegation": {
                "specialist": "hr",
                "tool": "hr_search_roster",
            },
            "results": [{"region": "EMEA"}],
        },
    )
    with pytest.raises(ValueError, match="RLS regions"):
        _validate_runtime_response(
            rls,
            {
                "test_id": "emea",
                "delegation": {
                    "specialist": "hr",
                    "tool": "hr_search_roster",
                },
                "results": [{"region": "APAC"}],
            },
        )

    denied = {
        "id": "unknown",
        "expect": {
            "error": "unknown-principal",
            "no_delegation": True,
            "no_result_rows": True,
        },
    }
    _validate_runtime_response(
        denied,
        {
            "test_id": "unknown",
            "delegation": None,
            "results": [],
            "error": {"code": "unknown-principal"},
        },
    )


def test_runtime_acceptance_sends_protected_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        """
data_classification: synthetic
cases:
  - id: support
    persona:
      oid: OID-SYNTHETIC-01
      tid: TID-SYNTHETIC-01
    prompt: Summarize synthetic case.
    expect:
      specialist: support
      tool: support_lookup_case
""",
        encoding="utf-8",
    )
    observed = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "test_id": "support",
                "delegation": {
                    "specialist": "support",
                    "tool": "support_lookup_case",
                },
                "results": [],
            }

    def fake_post(url, *, headers, json, timeout):
        observed.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return Response()

    monkeypatch.setattr("requests.post", fake_post)
    credential = "synthetic-test-token"
    run_synthetic_acceptance(
        suite,
        harness_url="https://test-harness.invalid/acceptance",
        token=credential,
    )

    assert observed["headers"]["Authorization"] == " ".join(
        ("Bearer", credential)
    )
    assert observed["json"]["persona"] == {
        "oid": "OID-SYNTHETIC-01",
        "tid": "TID-SYNTHETIC-01",
    }


def test_rollback_matches_direct_import_with_data_safeguards(
    repo_root: Path,
):
    runbook = (repo_root / "docs/operations/concierge-alm.md").read_text(
        encoding="utf-8"
    )
    assert "Power Platform Pipeline deployment record" in runbook
    assert "There is no automatic rollback button" in runbook
    assert "target-environment backup" in runbook
    assert "forward fix" in runbook
    assert "controlled uninstall and reinstall" in runbook


def _write_release_source(root: Path, *, agent_name: str = "Contoso Concierge") -> Path:
    source = root / "src"
    (source / "Other").mkdir(parents=True)
    (source / "connectionreferences" / "ccs_FoundrySpecialist").mkdir(parents=True)
    (source / "bots" / "contoso-concierge").mkdir(parents=True)
    (source / "Other" / "Solution.xml").write_text(
        """<ImportExportXml><SolutionManifest><RootComponents>
<RootComponent type="371" schemaName="ccs_FoundrySpecialist" />
<RootComponent type="10134" schemaName="ContosoConcierge" />
</RootComponents></SolutionManifest></ImportExportXml>""",
        encoding="utf-8",
    )
    (source / "connectionreferences" / "ccs_FoundrySpecialist" / "connectionreference.xml").write_text(
        '<connectionreference logicalname="ccs_FoundrySpecialist" />',
        encoding="utf-8",
    )
    (source / "bots" / "contoso-concierge" / "bot.xml").write_text(
        f"<bot><name>{agent_name}</name></bot>",
        encoding="utf-8",
    )
    return source


def test_release_source_requires_exact_bound_concierge_components(tmp_path: Path):
    source = _write_release_source(tmp_path)
    validate_release_source(source)

    foreign = _write_release_source(tmp_path / "foreign", agent_name="Other Agent")
    with pytest.raises(ValueError, match="Contoso Concierge"):
        validate_release_source(foreign)


def test_solution_guid_allowlist_is_structure_aware(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    guid = "11111111-2222-3333-4444-555555555555"
    (allowed / "component.xml").write_text(
        f'<RootComponent id="{guid}" />',
        encoding="utf-8",
    )
    validate_solution_identifier_fields(allowed)

    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    (forbidden / "tenant.xml").write_text(
        f"<TenantId>{guid}</TenantId>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unapproved field"):
        validate_solution_identifier_fields(forbidden)


def test_concierge_sources_contain_no_live_identifiers_or_secrets(repo_root: Path):
    targets = [
        repo_root / "config" / "concierge",
        repo_root / "deployment" / "concierge",
        repo_root / "solutions" / "ContosoConcierge",
        repo_root / "scripts" / "concierge-alm.ps1",
        repo_root / "src" / "contoso_foundry" / "concierge_alm.py",
        repo_root / ".github" / "workflows" / "concierge-alm.yml",
        repo_root / ".github" / "actions" / "verify-concierge-artifact",
    ]
    solution_source = repo_root / "solutions" / "ContosoConcierge" / "src"
    validate_solution_identifier_fields(solution_source)
    findings = []
    for target in targets:
        for finding in scan.scan_path(target).findings:
            if (
                finding.rule == "guid"
                and solution_source in finding.path.parents
            ):
                continue
            findings.append(finding)

    assert findings == [], "\n".join(str(finding) for finding in findings)


def test_exported_solution_guids_are_only_component_ids(repo_root: Path):
    source = repo_root / "solutions" / "ContosoConcierge" / "src"
    sensitive_names = {
        "applicationid",
        "connectionid",
        "environmentid",
        "objectid",
        "principalid",
        "subscriptionid",
        "tenantid",
    }

    for path in source.rglob("*"):
        if not path.is_file():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not list(patterns.GUID.finditer(line)):
                continue
            lowered = line.lower().replace("_", "").replace("-", "")
            found = {name for name in sensitive_names if name in lowered}
            assert not found, f"{path}:{line_number}: sensitive identifier field contains a GUID: {sorted(found)}"


def test_powershell_is_part_of_the_public_secret_scan(tmp_path: Path):
    script = tmp_path / "deploy.ps1"
    script.write_text("client_secret=8Kq9ZxLm2QwErTyUiOpAsDfGhJkLzXcVbNm3Q4", encoding="utf-8")

    result = scan.scan_path(tmp_path)

    assert result.scanned_files == 1
    assert result.findings
