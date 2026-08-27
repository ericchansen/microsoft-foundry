"""Validation and synthetic acceptance gates for the Concierge ALM workflow."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

from . import patterns

SOLUTION_NAME = "ContosoConcierge"
ACCEPTED_VERSION = "1.0.0.0"
WORKFLOW_PATH = ".github/workflows/concierge-alm.yml"
TRUSTED_BRANCH = "main"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
EXPECTED_CONNECTION_REFERENCE = "ccs_FoundrySpecialist"
EXPECTED_AGENT_NAMES = frozenset({"ContosoConcierge", "contoso-concierge", "Contoso Concierge"})
SOLUTION_COMPONENT_ID_FIELDS = frozenset(
    {
        "attributeid",
        "botcomponentid",
        "botid",
        "componentid",
        "connectionreferenceid",
        "entityid",
        "environmentvariabledefinitionid",
        "formid",
        "processid",
        "publisherid",
        "relationshipid",
        "roleid",
        "rootcomponentid",
        "solutionid",
        "templateid",
        "workflowid",
    }
)
SENSITIVE_ID_FIELDS = frozenset(
    {
        "applicationid",
        "connectionid",
        "environmentid",
        "objectid",
        "principalid",
        "subscriptionid",
        "tenantid",
    }
)
GENERIC_ID_COMPONENTS = frozenset(
    {
        "bot",
        "botcomponent",
        "connectionreference",
        "entity",
        "environmentvariabledefinition",
        "rootcomponent",
        "workflow",
    }
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    return value


def validate_source_run(
    run: dict[str, Any],
    artifacts_response: dict[str, Any],
    *,
    run_id: int,
    expected_sha: str,
    expected_artifact: str,
    expected_repository: str,
) -> tuple[int, str]:
    """Validate immutable GitHub run and artifact metadata before download."""
    if not SHA_PATTERN.fullmatch(expected_sha):
        raise ValueError("Expected source SHA must be a lowercase 40-character SHA.")

    expected_run = {
        "id": run_id,
        "path": WORKFLOW_PATH,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_branch": TRUSTED_BRANCH,
        "head_sha": expected_sha,
    }
    for field, expected in expected_run.items():
        if run.get(field) != expected:
            raise ValueError(f"Source workflow run has an untrusted {field}.")

    repository = run.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != expected_repository:
        raise ValueError("Source workflow run belongs to an untrusted repository.")

    artifacts = artifacts_response.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("Artifact metadata response is malformed.")
    matches = [item for item in artifacts if item.get("name") == expected_artifact]
    if len(matches) != 1:
        raise ValueError("Source run must contain exactly one expected artifact.")

    artifact = matches[0]
    if artifact.get("expired") is not False:
        raise ValueError("Source artifact is expired.")
    workflow_run = artifact.get("workflow_run")
    if (
        not isinstance(workflow_run, dict)
        or workflow_run.get("id") != run_id
        or workflow_run.get("head_sha") != expected_sha
    ):
        raise ValueError("Artifact metadata is not bound to the trusted source run.")

    digest_match = DIGEST_PATTERN.fullmatch(str(artifact.get("digest", "")))
    if not digest_match:
        raise ValueError("Artifact metadata is missing its SHA-256 digest.")
    artifact_id = artifact.get("id")
    if not isinstance(artifact_id, int):
        raise ValueError("Artifact metadata is missing its numeric ID.")
    return artifact_id, digest_match.group(1)


def validate_solution_package(path: Path, *, managed: bool) -> None:
    """Validate identity, version, and package type inside a solution ZIP."""
    if not path.is_file():
        raise ValueError(f"Solution package {path.name} does not exist.")

    try:
        with zipfile.ZipFile(path) as archive:
            manifests = [
                name for name in archive.namelist() if name.lower() == "solution.xml"
            ]
            if manifests != ["solution.xml"]:
                raise ValueError("Solution package must contain one root solution.xml.")
            root = ElementTree.fromstring(archive.read(manifests[0]))
    except zipfile.BadZipFile as exc:
        raise ValueError("Solution package is not a valid ZIP file.") from exc
    except ElementTree.ParseError as exc:
        raise ValueError("Solution package manifest is invalid XML.") from exc

    manifest = root.find("SolutionManifest")
    if manifest is None:
        raise ValueError("Solution package manifest is missing SolutionManifest.")
    actual = {
        "UniqueName": SOLUTION_NAME,
        "Version": ACCEPTED_VERSION,
        "Managed": "1" if managed else "0",
    }
    for element_name, expected in actual.items():
        element = manifest.find(element_name)
        if element is None or element.text != expected:
            raise ValueError(f"Solution package has an unexpected {element_name}.")


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].replace("_", "").replace("-", "").casefold()


def _guid_values(value: str | None) -> list[str]:
    return [match.group(0).casefold() for match in patterns.GUID.finditer(value or "")]


def validate_solution_identifier_fields(source: Path) -> None:
    """Allow solution component GUIDs only in recognized component-ID fields."""
    for path in sorted(source.rglob("*.xml")):
        text = path.read_text(encoding="utf-8", errors="strict")
        raw_guids = Counter(_guid_values(text))
        if not raw_guids:
            continue
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise ValueError(f"{path.name} is invalid XML.") from exc

        classified: Counter[str] = Counter()
        for element in root.iter():
            element_name = _local_name(element.tag)
            for guid in _guid_values(element.text):
                classified[guid] += 1
                if element_name in SENSITIVE_ID_FIELDS or element_name not in SOLUTION_COMPONENT_ID_FIELDS:
                    raise ValueError(f"{path.name} contains a GUID in unapproved field {element_name!r}.")
            for attribute, value in element.attrib.items():
                field_name = _local_name(attribute)
                for guid in _guid_values(value):
                    classified[guid] += 1
                    allowed = field_name in SOLUTION_COMPONENT_ID_FIELDS or (
                        field_name == "id" and element_name in GENERIC_ID_COMPONENTS
                    )
                    if field_name in SENSITIVE_ID_FIELDS or not allowed:
                        raise ValueError(f"{path.name} contains a GUID in unapproved field {field_name!r}.")

        if classified != raw_guids:
            raise ValueError(f"{path.name} contains a GUID outside a parsed component-ID field.")


def _component_identity(path: Path) -> tuple[set[str], set[str]]:
    text = path.read_text(encoding="utf-8", errors="strict")
    root = ElementTree.fromstring(text)
    normalized: set[str] = set()
    ids: set[str] = set()
    for element in root.iter():
        normalized.add(_local_name(element.tag))
        if element.text:
            normalized.add(_local_name(element.text))
            ids.update(_guid_values(element.text))
        for attribute, value in element.attrib.items():
            normalized.add(_local_name(attribute))
            normalized.add(_local_name(value))
            ids.update(_guid_values(value))
    return normalized, ids


def validate_release_source(source: Path) -> None:
    """Require the exact Concierge and specialist connection export plus root bindings."""
    solution_path = source / "Other" / "Solution.xml"
    if not solution_path.is_file():
        raise ValueError("Release source is missing Other/Solution.xml.")
    validate_solution_identifier_fields(source)

    connection_files = sorted((source / "connectionreferences").rglob("connectionreference.xml"))
    bot_files = sorted(
        path
        for folder in ("bots", "botcomponents")
        if (source / folder).is_dir()
        for path in (source / folder).rglob("*.xml")
    )
    if not connection_files or not bot_files:
        raise ValueError("Release source is missing the Concierge or specialist connection component.")

    expected_connection = _local_name(EXPECTED_CONNECTION_REFERENCE)
    connection_matches = []
    connection_ids: set[str] = set()
    for path in connection_files:
        values, ids = _component_identity(path)
        if expected_connection in values:
            connection_matches.append(path)
            connection_ids.update(ids)
    if len(connection_matches) != 1:
        raise ValueError("Release source must contain exactly one ccs_FoundrySpecialist connection reference.")

    expected_agents = {_local_name(name) for name in EXPECTED_AGENT_NAMES}
    bot_matches = []
    bot_ids: set[str] = set()
    for path in bot_files:
        values, ids = _component_identity(path)
        if values & expected_agents:
            bot_matches.append(path)
            bot_ids.update(ids)
    if not bot_matches:
        raise ValueError("Release source does not contain the Contoso Concierge agent component.")

    solution_root = ElementTree.parse(solution_path).getroot()
    root_components = solution_root.findall("./SolutionManifest/RootComponents/RootComponent")
    root_names = {_local_name(item.attrib.get("schemaName", "")) for item in root_components}
    root_ids = {
        guid
        for item in root_components
        for guid in _guid_values(item.attrib.get("id"))
    }
    connection_bound = expected_connection in root_names or bool(connection_ids & root_ids)
    agent_bound = bool(expected_agents & root_names) or bool(bot_ids & root_ids)
    if not connection_bound or not agent_bound:
        raise ValueError("Release source components are not both bound as solution root components.")


def _assert_expected(actual: Any, expected: Any, case_id: str, field: str) -> None:
    if actual != expected:
        raise ValueError(f"Synthetic case {case_id} failed its {field} assertion.")


def _validate_runtime_response(
    case: dict[str, Any], response: dict[str, Any]
) -> None:
    case_id = str(case["id"])
    expected = case["expect"]
    _assert_expected(response.get("test_id"), case_id, case_id, "test ID")

    if "error" in expected:
        error = response.get("error")
        actual_error = error.get("code") if isinstance(error, dict) else None
        _assert_expected(actual_error, expected["error"], case_id, "error")
        if expected.get("no_delegation"):
            _assert_expected(
                response.get("delegation"), None, case_id, "no-delegation"
            )
        if expected.get("no_result_rows"):
            _assert_expected(response.get("results"), [], case_id, "empty-results")
        return

    delegation = response.get("delegation")
    if not isinstance(delegation, dict):
        raise ValueError(f"Synthetic case {case_id} returned no delegation.")
    for field in ("specialist", "tool"):
        if field in expected:
            _assert_expected(
                delegation.get(field), expected[field], case_id, field
            )
    expected_arguments = expected.get("arguments", {})
    arguments = delegation.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError(f"Synthetic case {case_id} returned invalid delegation arguments.")
    _assert_expected(arguments, expected_arguments, case_id, "delegation arguments")

    if "result_regions" in expected:
        results = response.get("results")
        if not isinstance(results, list):
            raise ValueError(f"Synthetic case {case_id} returned invalid results.")
        actual_regions = sorted(
            {
                row.get("region")
                for row in results
                if isinstance(row, dict) and isinstance(row.get("region"), str)
            }
        )
        _assert_expected(
            actual_regions,
            sorted(expected["result_regions"]),
            case_id,
            "RLS regions",
        )


def run_synthetic_acceptance(
    suite_path: Path, *, harness_url: str, token: str, timeout: float = 30
) -> None:
    """Run synthetic-only TEST requests against the protected harness."""
    import requests
    import yaml

    parsed_url = urlparse(harness_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("TEST harness URL must be an absolute HTTPS URL.")
    if not token:
        raise ValueError("TEST harness bearer token is required.")

    suite = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    if not isinstance(suite, dict):
        raise ValueError("Synthetic acceptance suite is malformed.")
    if suite.get("data_classification") != "synthetic":
        raise ValueError("Only a synthetic-classified acceptance suite may run.")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Synthetic acceptance suite contains no cases.")

    for case in cases:
        payload = {
            "schema_version": 1,
            "test_id": case["id"],
            "persona": {
                "oid": case["persona"]["oid"],
                "tid": case["persona"]["tid"],
            },
            "prompt": case["prompt"],
        }
        try:
            response = requests.post(
                harness_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise ValueError(
                f"Synthetic case {case['id']} request failed."
            ) from exc
        if response.status_code != 200:
            raise ValueError(
                f"Synthetic case {case['id']} returned HTTP {response.status_code}."
            )
        try:
            body = response.json()
        except requests.JSONDecodeError as exc:
            raise ValueError(
                f"Synthetic case {case['id']} returned invalid JSON."
            ) from exc
        if not isinstance(body, dict):
            raise ValueError(f"Synthetic case {case['id']} returned invalid JSON.")
        _validate_runtime_response(case, body)
        print(f"PASS {case['id']}")


def _write_github_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    source = subparsers.add_parser("validate-source")
    source.add_argument("--run-json", type=Path, required=True)
    source.add_argument("--artifacts-json", type=Path, required=True)
    source.add_argument("--run-id", type=int, required=True)
    source.add_argument("--expected-sha", required=True)
    source.add_argument("--expected-artifact", required=True)
    source.add_argument("--expected-repository", required=True)
    source.add_argument("--github-output", type=Path, required=True)

    package = subparsers.add_parser("validate-package")
    package.add_argument("--path", type=Path, required=True)
    package.add_argument(
        "--managed", choices=("true", "false"), required=True
    )

    source = subparsers.add_parser("validate-release-source")
    source.add_argument("--path", type=Path, required=True)

    runtime = subparsers.add_parser("runtime-test")
    runtime.add_argument("--suite", type=Path, required=True)
    runtime.add_argument("--url", required=True)
    runtime.add_argument(
        "--token-environment",
        default="CONCIERGE_TEST_HARNESS_TOKEN",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "validate-source":
            artifact_id, digest = validate_source_run(
                _read_json(args.run_json),
                _read_json(args.artifacts_json),
                run_id=args.run_id,
                expected_sha=args.expected_sha,
                expected_artifact=args.expected_artifact,
                expected_repository=args.expected_repository,
            )
            _write_github_outputs(
                args.github_output,
                {"artifact_id": str(artifact_id), "artifact_digest": digest},
            )
        elif args.command == "validate-package":
            validate_solution_package(args.path, managed=args.managed == "true")
        elif args.command == "validate-release-source":
            validate_release_source(args.path)
        else:
            run_synthetic_acceptance(
                args.suite,
                harness_url=args.url,
                token=os.environ.get(args.token_environment, ""),
            )
    except (KeyError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
