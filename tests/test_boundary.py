"""The ownership boundary is the check that stands between a demo and an incident.

These tests are written against the failure modes that would actually cost
something: a plan that quietly addresses another subscription, a role assignment
scoped wider than the resource group, adopting infrastructure someone else owns,
and creating resources with no way to remove them.
"""

from __future__ import annotations

from typing import Any

import pytest

from contoso_foundry import boundary

RG = "rg-contoso-agents"


def plan(**overrides: Any) -> dict[str, Any]:
    """A minimal plan that passes every check, so each test changes one thing."""
    base: dict[str, Any] = {
        "resource_group": RG,
        "resources": [{"name": "foundry", "kind": "ai-foundry", "scope": "providers/Microsoft.CognitiveServices"}],
        "identities": [{"name": "id-agent", "scope": "providers/Microsoft.ManagedIdentity"}],
        "role_assignments": [
            {"name": "agent-reads-foundry", "role": "Cognitive Services User", "scope": "resource-group"}
        ],
        "teardown": [{"name": "rg", "kind": "resource-group", "scope": "resource-group"}],
    }
    base.update(overrides)
    return base


def failed_checks(report: boundary.BoundaryReport) -> set[str]:
    return {v.check for v in report.violations}


class TestHappyPath:
    def test_a_well_formed_plan_passes(self):
        report = boundary.check_plan(plan(), expected_resource_group=RG)
        assert report.ok
        assert report.violations == []

    def test_every_check_is_recorded_even_when_it_passes(self):
        """A silent pass is indistinguishable from a check that never ran."""
        report = boundary.check_plan(plan(), expected_resource_group=RG)
        assert report.checks_run == [
            "plan:resource-group-name",
            "plan:relative-scopes",
            "plan:no-reuse",
            "plan:role-assignment-scopes",
            "plan:diagnostic-targets",
            "plan:teardown-completeness",
        ]


class TestDiagnosticTargets:
    """Where telemetry *lands* decides whether it leaves the boundary."""

    def test_rejects_a_workspace_this_plan_does_not_create(self):
        report = boundary.check_plan(
            plan(
                diagnostic_settings=[
                    {
                        "name": "apim-diagnostics",
                        "target_workspace": "shared-corp-logs",
                        "scope": "providers/Microsoft.ApiManagement",
                    }
                ]
            ),
            expected_resource_group=RG,
        )
        assert "plan:diagnostic-targets" in failed_checks(report)

    def test_rejects_a_setting_with_no_declared_target(self):
        report = boundary.check_plan(
            plan(
                diagnostic_settings=[
                    {"name": "apim-diagnostics", "scope": "providers/Microsoft.ApiManagement"}
                ]
            ),
            expected_resource_group=RG,
        )
        assert "plan:diagnostic-targets" in failed_checks(report)

    def test_accepts_a_workspace_declared_in_the_same_plan(self):
        report = boundary.check_plan(
            plan(
                resources=[
                    {"name": "foundry", "kind": "ai-foundry", "scope": "providers/Microsoft.CognitiveServices"},
                    {"name": "logs", "kind": "workspace", "scope": "providers/Microsoft.OperationalInsights"},
                ],
                diagnostic_settings=[
                    {
                        "name": "apim-diagnostics",
                        "target_workspace": "logs",
                        "scope": "providers/Microsoft.ApiManagement",
                    }
                ],
            ),
            expected_resource_group=RG,
        )
        assert report.ok, report.violations


class TestResourceGroupName:
    def test_rejects_a_plan_that_moves_the_boundary(self):
        """Changing the boundary must be a deliberate, reviewed edit."""
        report = boundary.check_plan(plan(resource_group="rg-something-else"), expected_resource_group=RG)
        assert "plan:resource-group-name" in failed_checks(report)

    @pytest.mark.parametrize("name", ["", "rg contoso agents", "rg/contoso"])
    def test_rejects_malformed_names(self, name: str):
        report = boundary.check_plan(plan(resource_group=name))
        assert "plan:resource-group-name" in failed_checks(report)


class TestRelativeScopes:
    """Relative scopes are what keep the subscription ID out of tracked files.

    An absolute scope would have to name a subscription to be useful, so
    forbidding them removes the opportunity to leak one rather than relying on
    anyone remembering not to.
    """

    @pytest.mark.parametrize(
        "scope",
        [
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-contoso-agents",
            "../rg-someone-elses",
            "resourceGroups/rg-elsewhere",
            "subscriptions/00000000-0000-0000-0000-000000000000",
        ],
    )
    def test_rejects_scopes_that_can_address_something_else(self, scope: str):
        report = boundary.check_plan(plan(resources=[{"name": "x", "scope": scope}]))
        assert "plan:relative-scopes" in failed_checks(report)

    def test_rejects_a_missing_scope(self):
        """An undeclared scope is not 'probably fine'; it is unreviewable."""
        report = boundary.check_plan(plan(resources=[{"name": "x"}]))
        assert "plan:relative-scopes" in failed_checks(report)

    def test_checks_identities_and_teardown_too(self, ):
        """Every section that declares a scope is subject to the same rule."""
        report = boundary.check_plan(plan(identities=[{"name": "id", "scope": "/subscriptions/abc"}]))
        assert "plan:relative-scopes" in failed_checks(report)


class TestNoReuse:
    def test_rejects_adopting_an_existing_resource(self):
        report = boundary.check_plan(plan(resources=[{"name": "x", "scope": "providers/X", "reuse_existing": True}]))
        assert "plan:no-reuse" in failed_checks(report)

    def test_rejects_adopting_an_existing_identity(self):
        report = boundary.check_plan(
            plan(identities=[{"name": "id", "scope": "providers/Y", "reuse_existing": True}])
        )
        assert "plan:no-reuse" in failed_checks(report)


class TestRoleAssignments:
    @pytest.mark.parametrize("scope", ["subscription", "tenant"])
    def test_rejects_assignments_wider_than_the_resource_group(self, scope: str):
        report = boundary.check_plan(
            plan(role_assignments=[{"name": "r", "role": "Contributor", "scope": scope}])
        )
        assert "plan:role-assignment-scopes" in failed_checks(report)

    def test_rejects_an_assignment_with_no_role(self):
        report = boundary.check_plan(plan(role_assignments=[{"name": "r", "scope": "resource-group"}]))
        assert "plan:role-assignment-scopes" in failed_checks(report)


class TestTeardownCompleteness:
    def test_rejects_creating_something_with_no_way_to_remove_it(self):
        report = boundary.check_plan(
            plan(
                resources=[{"name": "orphan", "scope": "providers/X"}],
                teardown=[{"name": "id-agent", "kind": "identity", "scope": "providers/Y"}],
            )
        )
        assert "plan:teardown-completeness" in failed_checks(report)

    def test_deleting_the_resource_group_satisfies_everything_inside_it(self):
        """Enumerating children of a group that is itself deleted is busywork."""
        report = boundary.check_plan(
            plan(resources=[{"name": "a", "scope": "providers/X"}, {"name": "b", "scope": "providers/Y"}]),
            expected_resource_group=RG,
        )
        assert report.ok


class TestProvisioningGate:
    def test_refuses_to_create_the_group_when_the_boundary_is_dirty(self):
        """This is the property that makes the check a gate rather than a report."""
        report = boundary.check_plan(plan(resource_group="rg-wrong"), expected_resource_group=RG)
        assert not report.ok
        with pytest.raises(PermissionError):
            boundary.ensure_resource_group(report, location="northcentralus", tags={}, dry_run=True)

    def test_dry_run_does_not_mutate(self):
        report = boundary.check_plan(plan(), expected_resource_group=RG)
        result = boundary.ensure_resource_group(report, location="northcentralus", tags={"env": "demo"})
        assert "dry" in result.lower() or "would" in result.lower()
