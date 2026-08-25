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
        assert len(report.checks_run) == 5


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

    def test_accepts_apim_subscription_child_resource(self):
        scope = "providers/Microsoft.ApiManagement/service/gateway/subscriptions/project"
        report = boundary.check_plan(plan(resources=[{"name": "x", "scope": scope}]))
        assert report.ok

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


class TestExistingGroupOwnership:
    OWNERSHIP_TAGS = {
        "project": "contoso-agents",
        "managed-by": "microsoft-foundry-demo",
        "boundary": RG,
    }

    @staticmethod
    def _patch_live(monkeypatch, *, tags, resources):
        def fake_run(args):
            if args[:2] == ["group", "list"]:
                return [{"name": RG, "tags": tags}]
            if args[:2] == ["resource", "list"]:
                return resources
            raise AssertionError(f"unexpected Azure CLI call: {args}")

        monkeypatch.setattr(boundary.azure_cli, "run", fake_run)

    def owned_plan(self):
        return plan(
            allow_existing_resource_group=True,
            tags=self.OWNERSHIP_TAGS,
            resources=[
                {
                    "name": "foundry",
                    "scope": "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry",
                }
            ],
        )

    def test_accepts_only_matching_tags_and_declared_resources(self, monkeypatch):
        resources = [{
            "id": (
                "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                "Microsoft.CognitiveServices/accounts/contoso-agents-foundry"
            )
        }]
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=resources)
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())
        assert report.ok

    @pytest.mark.parametrize("tags", [{}, {"boundary": RG}, {"project": "someone-else"}])
    def test_rejects_missing_or_wrong_ownership_tags(self, monkeypatch, tags):
        self._patch_live(monkeypatch, tags=tags, resources=[])
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())
        assert "live:target-ownership-tags" in failed_checks(report)

    def test_rejects_an_unexpected_resource(self, monkeypatch):
        resources = [{
            "id": (
                "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                "Microsoft.Storage/storageAccounts/not-declared"
            )
        }]
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=resources)
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())
        assert "live:declared-resource-inventory" in failed_checks(report)

    def test_rejects_failed_subscription_inventory(self, monkeypatch):
        def fail(_args):
            raise boundary.azure_cli.AzureCliError("authentication failed")

        monkeypatch.setattr(boundary.azure_cli, "run", fail)
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())

        assert not report.ok
        assert "live:subscription-inventory" in failed_checks(report)

    def test_rejects_failed_resource_inventory(self, monkeypatch):
        def fake_run(args):
            if args[:2] == ["group", "list"]:
                return [{"name": RG, "tags": self.OWNERSHIP_TAGS}]
            raise boundary.azure_cli.AzureCliError("resource inventory unavailable")

        monkeypatch.setattr(boundary.azure_cli, "run", fake_run)
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())

        assert not report.ok
        assert "live:resource-inventory" in failed_checks(report)
