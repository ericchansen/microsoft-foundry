"""Resolve and verify the Research deployment target inside its ARM boundary."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contoso_foundry import azure_cli
from contoso_foundry.boundary import (
    BoundaryReport,
    check_live,
    check_plan,
    enabled_modules_from_environment,
    load_plan,
)

_PROJECT_SCOPE = re.compile(
    r"^providers/Microsoft\.CognitiveServices/accounts/"
    r"(?P<account>[a-z0-9][a-z0-9-]{1,62})/projects/"
    r"(?P<project>[a-z0-9][a-z0-9-]{0,63})$"
)
_ACCOUNT_API_VERSION = "2025-06-01"
_PROJECT_API_VERSION = "2026-07-01"


class DeploymentBoundaryError(RuntimeError):
    """Raised when the hosted deployment target is not proven in-boundary."""


@dataclass(frozen=True, slots=True)
class ResearchDeploymentTarget:
    resource_group: str
    account_name: str
    project_name: str

    @property
    def endpoint(self) -> str:
        return (
            f"https://{self.account_name}.services.ai.azure.com"
            f"/api/projects/{self.project_name}"
        )


def target_from_boundary(boundary_path: Path) -> ResearchDeploymentTarget:
    """Derive the exact account/project names from the reviewed relative scope."""
    plan = load_plan(boundary_path)
    report = check_plan(plan)
    if not report.ok:
        raise DeploymentBoundaryError("the declared ownership boundary is invalid")

    matches = [
        resource
        for resource in plan.get("resources", [])
        if resource.get("name") == "research-project"
        and str(resource.get("kind", "")).lower()
        == "microsoft.cognitiveservices/accounts/projects"
    ]
    if len(matches) != 1:
        raise DeploymentBoundaryError(
            "the boundary must declare exactly one Research project"
        )
    scope = str(matches[0].get("scope", ""))
    match = _PROJECT_SCOPE.fullmatch(scope)
    if match is None:
        raise DeploymentBoundaryError(
            "the Research project must use an exact relative account/project scope"
        )
    return ResearchDeploymentTarget(
        resource_group=report.resource_group,
        account_name=match.group("account"),
        project_name=match.group("project"),
    )


def _require_arm_identity(
    resource: dict[str, Any],
    *,
    target: ResearchDeploymentTarget,
    expected_type: str,
    expected_suffix: str,
) -> None:
    resource_group = str(resource.get("resourceGroup", ""))
    resource_type = str(resource.get("type", ""))
    resource_id = str(resource.get("id", "")).rstrip("/")
    if resource_group.casefold() != target.resource_group.casefold():
        raise DeploymentBoundaryError("ARM resolved the deployment target outside the owned resource group")
    if resource_type.casefold() != expected_type.casefold():
        raise DeploymentBoundaryError("ARM resolved an unexpected deployment resource type")
    normalized_suffix = expected_suffix.replace("\\", "/").casefold()
    if not resource_id.replace("\\", "/").casefold().endswith(normalized_suffix):
        raise DeploymentBoundaryError("ARM resolved an unexpected deployment resource identity")


def verify_arm_target(
    target: ResearchDeploymentTarget,
    account_resource: dict[str, Any],
    project_resource: dict[str, Any],
) -> None:
    """Verify both nested ARM resources resolve to the declared group and names."""
    _require_arm_identity(
        account_resource,
        target=target,
        expected_type="Microsoft.CognitiveServices/accounts",
        expected_suffix=(
            f"/resourceGroups/{target.resource_group}/providers/"
            f"Microsoft.CognitiveServices/accounts/{target.account_name}"
        ),
    )
    _require_arm_identity(
        project_resource,
        target=target,
        expected_type="Microsoft.CognitiveServices/accounts/projects",
        expected_suffix=(
            f"/resourceGroups/{target.resource_group}/providers/"
            f"Microsoft.CognitiveServices/accounts/{target.account_name}/projects/"
            f"{target.project_name}"
        ),
    )


def verify_deployment_target(
    repo_root: Path,
    *,
    configured_endpoint: str | None,
) -> ResearchDeploymentTarget:
    """Resolve the target through ARM and optionally verify the configured endpoint."""
    target = target_from_boundary(repo_root / "config" / "boundary.yaml")
    if configured_endpoint is not None and configured_endpoint != target.endpoint:
        raise DeploymentBoundaryError(
            "CONTOSO_RESEARCH_PROJECT_ENDPOINT does not match the declared Research project"
        )

    account_resource = azure_cli.run(
        [
            "resource",
            "show",
            "--resource-group",
            target.resource_group,
            "--resource-type",
            "Microsoft.CognitiveServices/accounts",
            "--name",
            target.account_name,
            "--api-version",
            _ACCOUNT_API_VERSION,
        ]
    )
    project_resource = azure_cli.run(
        [
            "resource",
            "show",
            "--resource-group",
            target.resource_group,
            "--namespace",
            "Microsoft.CognitiveServices",
            "--parent",
            f"accounts/{target.account_name}",
            "--resource-type",
            "projects",
            "--name",
            target.project_name,
            "--api-version",
            _PROJECT_API_VERSION,
        ]
    )
    if not isinstance(account_resource, dict) or not isinstance(project_resource, dict):
        raise DeploymentBoundaryError("ARM did not return both declared Research resources")
    verify_arm_target(target, account_resource, project_resource)
    return target


def require_clean_live_boundary(
    boundary_path: Path,
    *,
    enabled_modules: set[str] | None = None,
    deployment_readiness: bool = False,
) -> BoundaryReport:
    """Require the exact live inventory gate used by mutation entry points."""
    plan = load_plan(boundary_path)
    report = check_plan(plan)
    if report.ok:
        report = check_live(
            report,
            plan,
            enabled_modules=enabled_modules,
            allow_missing_declared=deployment_readiness,
        )
    if not report.ok:
        raise DeploymentBoundaryError("the live ownership boundary is not clean")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="print the ARM-verified in-boundary endpoint for azd env set",
    )
    parser.add_argument(
        "--enable-module",
        action="append",
        default=[],
        help="include a disabled-by-default module in the exact live boundary",
    )
    args = parser.parse_args()
    configured_endpoint = (
        None
        if args.resolve
        else os.environ.get("CONTOSO_RESEARCH_PROJECT_ENDPOINT", "")
    )
    if configured_endpoint == "":
        raise DeploymentBoundaryError(
            "CONTOSO_RESEARCH_PROJECT_ENDPOINT is required for deployment"
        )
    repo_root = args.repo_root.resolve()
    enabled_modules = set(args.enable_module) | enabled_modules_from_environment()
    require_clean_live_boundary(
        repo_root / "config" / "boundary.yaml",
        enabled_modules=enabled_modules,
        deployment_readiness=True,
    )
    target = verify_deployment_target(
        repo_root,
        configured_endpoint=configured_endpoint,
    )
    if args.resolve:
        print(target.endpoint)
    else:
        print("Research deployment target verified inside the declared resource group.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
