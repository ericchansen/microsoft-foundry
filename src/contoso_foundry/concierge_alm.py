"""Validation and synthetic acceptance gates for the Concierge ALM workflow."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

SOLUTION_NAME = "ContosoConcierge"
ACCEPTED_VERSION = "1.0.0.0"
WORKFLOW_PATH = ".github/workflows/concierge-alm.yml"
TRUSTED_BRANCH = "main"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")


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
    for name, value in expected.get("arguments", {}).items():
        arguments = delegation.get("arguments")
        actual = arguments.get(name) if isinstance(arguments, dict) else None
        _assert_expected(actual, value, case_id, f"argument {name}")

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
