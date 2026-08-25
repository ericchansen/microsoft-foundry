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
    """Where telemetry lands decides whether it leaves the boundary."""

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


class TestExistingGroupOwnership:
    GROUP_ID = "/subscriptions/redacted/resourceGroups/rg-contoso-agents"
    OWNERSHIP_TAGS = {
        "project": "contoso-agents",
        "managed-by": "microsoft-foundry-demo",
        "boundary": RG,
    }

    def _patch_live(self, monkeypatch, *, tags, resources, roles=None, resource_details=None):
        def fake_run(args):
            if args == ["group", "list"]:
                return [{"id": self.GROUP_ID, "name": RG, "tags": tags}]
            if args == ["resource", "list", "-g", RG]:
                return resources
            if args == ["role", "assignment", "list", "--all"]:
                return roles or []
            if args[:3] == ["resource", "show", "--ids"]:
                detail = (resource_details or {}).get(args[3])
                if detail is not None:
                    return detail
                resource = next(
                    (item for item in resources if str(item.get("id")) == args[3]),
                    {},
                )
                if str(resource.get("type", "")).lower() == "microsoft.cognitiveservices/accounts":
                    return {"identity": {"principalId": "foundry-principal"}}
                return {}
            if args[:3] == ["rest", "--method", "get"]:
                return {"value": []}
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
            "type": "Microsoft.CognitiveServices/accounts",
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
            "type": "Microsoft.Storage/storageAccounts",
            "id": (
                "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                "Microsoft.Storage/storageAccounts/not-declared"
            )
        }]
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=resources)
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())
        assert "live:declared-resource-inventory" in failed_checks(report)

    def test_fails_closed_when_resource_enumeration_fails(self, monkeypatch):
        def fake_run(args):
            if args == ["group", "list"]:
                return [{"id": self.GROUP_ID, "name": RG, "tags": self.OWNERSHIP_TAGS}]
            raise boundary.azure_cli.AzureCliError("throttled")

        monkeypatch.setattr(boundary.azure_cli, "run", fake_run)
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())
        assert "live:resource-inventory" in failed_checks(report)

    def test_fails_closed_when_resource_group_enumeration_fails(self, monkeypatch):
        monkeypatch.setattr(
            boundary.azure_cli,
            "run",
            lambda args: (_ for _ in ()).throw(boundary.azure_cli.AzureCliError("denied")),
        )

        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())

        assert "live:protected-resource-groups" in failed_checks(report)

    def test_rejects_multiple_resources_matching_one_wildcard(self, monkeypatch):
        owned = self.owned_plan()
        owned["resources"] = [
            {
                "name": "storage",
                "kind": "Microsoft.Storage/storageAccounts",
                "scope": "providers/Microsoft.Storage/storageAccounts/contosoagents*",
            }
        ]
        resources = [
            {
                "id": f"{self.GROUP_ID}/providers/Microsoft.Storage/storageAccounts/contosoagentsone",
                "type": "Microsoft.Storage/storageAccounts",
            },
            {
                "id": f"{self.GROUP_ID}/providers/Microsoft.Storage/storageAccounts/contosoagentstwo",
                "type": "Microsoft.Storage/storageAccounts",
            },
        ]

        def fake_run(args):
            if args == ["group", "list"]:
                return [{"id": self.GROUP_ID, "name": RG, "tags": self.OWNERSHIP_TAGS}]
            if args == ["resource", "list", "-g", RG]:
                return resources
            if args[0:3] == ["rest", "--method", "get"]:
                return {"value": []}
            if args == ["role", "assignment", "list", "--all"]:
                return []
            raise AssertionError(f"unexpected Azure CLI call: {args}")

        monkeypatch.setattr(boundary.azure_cli, "run", fake_run)
        report = boundary.check_live(boundary.check_plan(owned), owned)
        assert "live:declared-resource-cardinality" in failed_checks(report)

    def test_rejects_an_undeclared_project_connection(self, monkeypatch):
        project_id = (
            f"{self.GROUP_ID}/providers/Microsoft.CognitiveServices/accounts/"
            "contoso-agents-foundry/projects/travel"
        )
        owned = self.owned_plan()
        owned["resources"] = [{
            "name": "travel-project",
            "kind": "Microsoft.CognitiveServices/accounts/projects",
            "scope": "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry/projects/travel",
        }]

        def fake_run(args):
            if args == ["group", "list"]:
                return [{"id": self.GROUP_ID, "name": RG, "tags": self.OWNERSHIP_TAGS}]
            if args == ["resource", "list", "-g", RG]:
                return [{
                    "id": project_id,
                    "type": "Microsoft.CognitiveServices/accounts/projects",
                    "identity": {"principalId": "travel-principal"},
                }]
            if args[:3] == ["rest", "--method", "get"]:
                return {
                    "value": [{
                        "id": f"{project_id}/connections/foreign",
                        "type": "Microsoft.CognitiveServices/accounts/projects/connections",
                    }]
                }
            if args == ["role", "assignment", "list", "--all"]:
                return []
            raise AssertionError(f"unexpected Azure CLI call: {args}")

        monkeypatch.setattr(boundary.azure_cli, "run", fake_run)
        report = boundary.check_live(boundary.check_plan(owned), owned)
        assert "live:declared-resource-inventory" in failed_checks(report)

    def test_enumerates_projects_and_connections_from_the_account(self, monkeypatch):
        account_id = (
            f"{self.GROUP_ID}/providers/Microsoft.CognitiveServices/accounts/"
            "contoso-agents-foundry"
        )
        project_id = f"{account_id}/projects/travel"
        connection_id = f"{project_id}/connections/appinsights-travel"
        owned = self.owned_plan()
        owned["resources"] = [
            {
                "name": "foundry-account",
                "kind": "Microsoft.CognitiveServices/accounts",
                "scope": "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry",
            },
            {
                "name": "travel-project",
                "kind": "Microsoft.CognitiveServices/accounts/projects",
                "scope": "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry/projects/travel",
            },
            {
                "name": "travel-connection",
                "kind": "Microsoft.CognitiveServices/accounts/projects/connections",
                "scope": (
                    "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry/"
                    "projects/travel/connections/appinsights-travel"
                ),
            },
        ]

        def fake_run(args):
            if args == ["group", "list"]:
                return [{"id": self.GROUP_ID, "name": RG, "tags": self.OWNERSHIP_TAGS}]
            if args == ["resource", "list", "-g", RG]:
                return [{
                    "id": account_id,
                    "type": "Microsoft.CognitiveServices/accounts",
                    "identity": {"principalId": "foundry-principal"},
                }]
            if args[:3] == ["rest", "--method", "get"]:
                if "/projects?" in args[4]:
                    return {
                        "value": [{
                            "id": project_id,
                            "type": "Microsoft.CognitiveServices/accounts/projects",
                            "identity": {"principalId": "travel-principal"},
                        }]
                    }
                if "/connections?" in args[4]:
                    return {
                        "value": [{
                            "id": connection_id,
                            "type": "Microsoft.CognitiveServices/accounts/projects/connections",
                        }]
                    }
            if args == ["role", "assignment", "list", "--all"]:
                return []
            raise AssertionError(f"unexpected Azure CLI call: {args}")

        monkeypatch.setattr(boundary.azure_cli, "run", fake_run)
        report = boundary.check_live(boundary.check_plan(owned), owned)
        assert report.ok, report.violations

    def test_rejects_an_undeclared_direct_role_assignment(self, monkeypatch):
        resource_id = f"{self.GROUP_ID}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-agent"
        owned = self.owned_plan()
        owned["role_definitions"] = {"Reader": "reader-role-id"}
        owned["identities"] = [
            {
                "name": "id-agent",
                "kind": "Microsoft.ManagedIdentity/userAssignedIdentities",
                "scope": "providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-agent",
            }
        ]
        owned["role_assignments"] = [
            {"name": "expected", "principal": "id-agent", "role": "Reader", "scope": "."}
        ]
        resources = [{"id": resource_id, "type": "Microsoft.ManagedIdentity/userAssignedIdentities"}]
        roles = [{
            "principalId": "foreign-principal",
            "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/reader-role-id",
            "scope": self.GROUP_ID,
        }]
        self._patch_live(
            monkeypatch,
            tags=self.OWNERSHIP_TAGS,
            resources=resources,
            roles=roles,
            resource_details={resource_id: {"properties": {"principalId": "expected-principal"}}},
        )
        report = boundary.check_live(boundary.check_plan(owned), owned)
        assert "live:declared-role-assignments" in failed_checks(report)

    def test_rejects_an_owned_principal_assignment_above_the_boundary(self, monkeypatch):
        resource_id = f"{self.GROUP_ID}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-agent"
        owned = self.owned_plan()
        owned["role_definitions"] = {"Reader": "reader-role-id"}
        owned["identities"] = [
            {
                "name": "id-agent",
                "kind": "Microsoft.ManagedIdentity/userAssignedIdentities",
                "scope": "providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-agent",
            }
        ]
        owned["role_assignments"] = [
            {"name": "expected", "principal": "id-agent", "role": "Reader", "scope": "."}
        ]
        resources = [{"id": resource_id, "type": "Microsoft.ManagedIdentity/userAssignedIdentities"}]
        roles = [{
            "principalId": "expected-principal",
            "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/reader-role-id",
            "scope": "/subscriptions/redacted",
        }]
        self._patch_live(
            monkeypatch,
            tags=self.OWNERSHIP_TAGS,
            resources=resources,
            roles=roles,
            resource_details={resource_id: {"properties": {"principalId": "expected-principal"}}},
        )

        report = boundary.check_live(boundary.check_plan(owned), owned)

        assert "live:declared-role-assignments" in failed_checks(report)

    def test_rejects_an_unreferenced_owned_principal_assignment_above_the_boundary(
        self,
        monkeypatch,
    ):
        account_id = (
            f"{self.GROUP_ID}/providers/Microsoft.CognitiveServices/accounts/"
            "contoso-agents-foundry"
        )
        owned = self.owned_plan()
        resources = [{"id": account_id, "type": "Microsoft.CognitiveServices/accounts"}]
        roles = [{
            "principalId": "foundry-principal",
            "roleDefinitionId": "/providers/Microsoft.Authorization/roleDefinitions/owner-role-id",
            "scope": "/subscriptions/redacted",
        }]
        self._patch_live(
            monkeypatch,
            tags=self.OWNERSHIP_TAGS,
            resources=resources,
            roles=roles,
            resource_details={account_id: {"identity": {"principalId": "foundry-principal"}}},
        )

        report = boundary.check_live(boundary.check_plan(owned), owned)

        assert "live:declared-role-assignments" in failed_checks(report)