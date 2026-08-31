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
            "plan:modules",
            "plan:resource-group-name",
            "plan:relative-scopes",
            "plan:scope-uniqueness",
            "plan:no-reuse",
            "plan:role-assignment-scopes",
            "plan:diagnostic-targets",
            "plan:teardown-completeness",
        ]

    def test_rejects_an_unknown_entry_module(self):
        report = boundary.check_plan(
            plan(resources=[{"name": "x", "scope": "providers/X", "module": "missing"}])
        )
        assert "plan:modules" in failed_checks(report)

    def test_rejects_duplicate_resource_scopes_statically(self):
        report = boundary.check_plan(
            plan(
                resources=[
                    {"name": "first", "scope": "providers/X/items/one"},
                    {"name": "second", "scope": "providers/X/items/one"},
                ]
            )
        )
        assert "plan:scope-uniqueness" in failed_checks(report)

    def test_scope_wildcards_never_cross_resource_segments(self):
        pattern = "providers/Microsoft.Storage/storageAccounts/contosoagents*".lower()
        child = (
            "providers/Microsoft.Storage/storageAccounts/contosoagentsdemo/"
            "blobServices/default/containers/ledger"
        ).lower()
        assert not boundary._scope_matches(child, pattern)

    def test_enabled_modules_can_be_supplied_by_environment(self, monkeypatch):
        monkeypatch.setenv(
            "FOUNDRY_ENABLED_MODULES",
            "optional-control-plane, second-module",
        )
        assert boundary.enabled_modules_from_environment() == {
            "optional-control-plane",
            "second-module",
        }

    def test_shipped_boundary_resource_patterns_do_not_overlap(self, repo_root):
        shipped = boundary.load_plan(repo_root / "config" / "boundary.yaml")
        entries = [
            (
                f"{section}/{entry['name']}",
                str(entry["scope"]).lower().rstrip("/"),
            )
            for section in ("resources", "identities")
            for entry in shipped[section]
        ]

        def sample(pattern):
            return pattern.replace("*", "sample").replace("?", "x")

        for index, (left_name, left) in enumerate(entries):
            for right_name, right in entries[index + 1 :]:
                assert not boundary._scope_matches(sample(left), right), (
                    left_name,
                    right_name,
                )
                assert not boundary._scope_matches(sample(right), left), (
                    right_name,
                    left_name,
                )

    def test_shipped_boundary_allows_exactly_one_versioned_travel_tool_release(self, repo_root):
        shipped = boundary.load_plan(repo_root / "config" / "boundary.yaml")
        travel_services = [
            entry
            for entry in shipped["resources"]
            if entry["kind"] == "Microsoft.App/containerApps"
            and "travel-tool" in entry["scope"]
        ]
        travel_connections = [
            entry
            for entry in shipped["resources"]
            if entry["kind"] == "Microsoft.CognitiveServices/accounts/projects/connections"
            and "travel-openapi" in entry["scope"]
        ]
        travel_identities = [
            entry
            for entry in shipped["identities"]
            if "travel-tool" in entry["scope"]
        ]

        assert len(travel_services) == len(travel_connections) == len(travel_identities) == 1
        for entry in (*travel_services, *travel_connections, *travel_identities):
            assert entry["scope"].endswith("-v*")
            assert entry["expected_live_count"] == 1
            assert entry["deployment_max_live_count"] == 2


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
                    {
                        "name": "apim-diagnostics",
                        "scope": "providers/Microsoft.ApiManagement",
                    }
                ]
            ),
            expected_resource_group=RG,
        )
        assert "plan:diagnostic-targets" in failed_checks(report)

    def test_accepts_a_workspace_declared_in_the_same_plan(self):
        report = boundary.check_plan(
            plan(
                resources=[
                    {
                        "name": "foundry",
                        "kind": "ai-foundry",
                        "scope": "providers/Microsoft.CognitiveServices",
                    },
                    {
                        "name": "logs",
                        "kind": "workspace",
                        "scope": "providers/Microsoft.OperationalInsights",
                    },
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
    OWNERSHIP_TAGS = {
        "project": "contoso-agents",
        "managed-by": "microsoft-foundry-demo",
        "boundary": RG,
    }

    @staticmethod
    def _patch_live(
        monkeypatch,
        *,
        tags,
        resources,
        identities=None,
        role_assignments=None,
        resource_details=None,
    ):
        def run(arguments):
            if arguments[:2] == ["group", "list"]:
                return [{"name": RG, "tags": tags}]
            if arguments[:2] == ["resource", "list"]:
                return resources
            if arguments[:2] == ["identity", "list"]:
                return identities or []
            if arguments[:2] == ["resource", "show"]:
                return (resource_details or {}).get(arguments[-1], {})
            if arguments[:3] == ["role", "assignment", "list"]:
                assert "--include-inherited" in arguments
                return role_assignments or []
            raise AssertionError(f"unexpected Azure CLI read: {arguments}")

        monkeypatch.setattr(boundary.azure_cli, "run", run)

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
            identities=[],
            role_assignments=[],
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

    def test_rejects_a_missing_declared_resource(self, monkeypatch):
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=[])
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())
        assert "live:declared-resource-inventory" in failed_checks(report)
        assert "resources/foundry" in str(report.violations[-1])

    def test_deployment_readiness_allows_missing_declared_resources(self, monkeypatch):
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=[])
        report = boundary.check_live(
            boundary.check_plan(self.owned_plan()),
            self.owned_plan(),
            allow_missing_declared=True,
        )
        assert report.ok

    def test_deployment_readiness_fails_closed_when_role_inventory_fails(self, monkeypatch):
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=[])
        original = boundary.azure_cli.run

        def fail_role_inventory(arguments):
            if arguments[:3] == ["role", "assignment", "list"]:
                raise boundary.azure_cli.AzureCliError("role inventory unavailable")
            return original(arguments)

        monkeypatch.setattr(boundary.azure_cli, "run", fail_role_inventory)

        with pytest.raises(boundary.azure_cli.AzureCliError, match="inventory unavailable"):
            boundary.check_live(
                boundary.check_plan(self.owned_plan()),
                self.owned_plan(),
                allow_missing_declared=True,
            )

    def test_deployment_readiness_still_rejects_unexpected_resources(self, monkeypatch):
        resources = [{
            "id": (
                "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                "Microsoft.Storage/storageAccounts/not-declared"
            )
        }]
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=resources)
        report = boundary.check_live(
            boundary.check_plan(self.owned_plan()),
            self.owned_plan(),
            allow_missing_declared=True,
        )
        assert "live:declared-resource-inventory" in failed_checks(report)

    def test_rejects_multiple_resources_absorbed_by_one_wildcard(self, monkeypatch):
        plan_data = plan(
            allow_existing_resource_group=True,
            tags=self.OWNERSHIP_TAGS,
            resources=[
                {
                    "name": "registry",
                    "scope": "providers/Microsoft.ContainerRegistry/registries/contosoagents*",
                }
            ],
            identities=[],
            role_assignments=[],
        )
        resources = [
            {
                "id": (
                    "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                    "Microsoft.ContainerRegistry/registries/contosoagentsone"
                )
            },
            {
                "id": (
                    "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                    "Microsoft.ContainerRegistry/registries/contosoagentstwo"
                )
            },
        ]
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=resources)

        report = boundary.check_live(boundary.check_plan(plan_data), plan_data)

        assert "live:declared-resource-inventory" in failed_checks(report)
        assert "resources/registry" in str(report.violations[-1])

    def test_deployment_readiness_allows_bounded_release_overlap(self, monkeypatch):
        plan_data = plan(
            allow_existing_resource_group=True,
            tags=self.OWNERSHIP_TAGS,
            resources=[
                {
                    "name": "release",
                    "scope": "providers/Microsoft.App/containerApps/travel-v*",
                    "expected_live_count": 1,
                    "deployment_max_live_count": 2,
                }
            ],
            identities=[],
            role_assignments=[],
        )
        resources = [
            {
                "id": (
                    "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                    f"Microsoft.App/containerApps/travel-v{version}"
                )
            }
            for version in (2, 3)
        ]
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=resources)

        readiness = boundary.check_live(
            boundary.check_plan(plan_data),
            plan_data,
            allow_missing_declared=True,
        )
        strict = boundary.check_live(boundary.check_plan(plan_data), plan_data)

        assert readiness.ok
        assert "live:declared-resource-inventory" in failed_checks(strict)

    def test_rejects_overlapping_declarations_for_one_resource(self, monkeypatch):
        plan_data = plan(
            allow_existing_resource_group=True,
            tags=self.OWNERSHIP_TAGS,
            resources=[
                {
                    "name": "registry-pattern",
                    "scope": "providers/Microsoft.ContainerRegistry/registries/contosoagents*",
                },
                {
                    "name": "registry-exact",
                    "scope": "providers/Microsoft.ContainerRegistry/registries/contosoagentsone",
                },
            ],
            identities=[],
            role_assignments=[],
        )
        resource = {
            "id": (
                "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                "Microsoft.ContainerRegistry/registries/contosoagentsone"
            )
        }
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=[resource])

        report = boundary.check_live(boundary.check_plan(plan_data), plan_data)

        assert "live:declared-resource-inventory" in failed_checks(report)
        assert "resources/registry-pattern" in str(report.violations[-1])
        assert "resources/registry-exact" in str(report.violations[-1])

    def test_rejects_duplicate_declarations_even_when_names_match(self, monkeypatch):
        declaration = {
            "name": "registry",
            "scope": "providers/Microsoft.ContainerRegistry/registries/contosoagentsone",
        }
        plan_data = plan(
            allow_existing_resource_group=True,
            tags=self.OWNERSHIP_TAGS,
            resources=[declaration, declaration],
            identities=[],
            role_assignments=[],
        )
        resource = {
            "id": (
                "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                "Microsoft.ContainerRegistry/registries/contosoagentsone"
            )
        }
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=[resource])

        report = boundary.check_live(boundary.check_plan(plan_data), plan_data)

        assert "live:declared-resource-inventory" in failed_checks(report)
        assert "resources/registry[0]" in str(report.violations[-1])
        assert "resources/registry[1]" in str(report.violations[-1])

    def test_allows_one_declared_resource_to_require_an_exact_live_count(self, monkeypatch):
        plan_data = plan(
            allow_existing_resource_group=True,
            tags=self.OWNERSHIP_TAGS,
            resources=[
                {
                    "name": "apis",
                    "scope": "providers/Microsoft.ApiManagement/service/gateway/apis/foundry-*",
                    "expected_live_count": 2,
                }
            ],
            identities=[],
            role_assignments=[],
        )
        resources = [
            {
                "id": (
                    "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
                    f"Microsoft.ApiManagement/service/gateway/apis/foundry-{name}"
                )
            }
            for name in ("travel", "support")
        ]
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=resources)
        assert boundary.check_live(boundary.check_plan(plan_data), plan_data).ok

    def test_allows_a_declared_planned_resource_to_be_absent(self, monkeypatch):
        plan_data = plan(
            allow_existing_resource_group=True,
            tags=self.OWNERSHIP_TAGS,
            resources=[
                {
                    "name": "planned",
                    "scope": "providers/Microsoft.App/containerApps/planned",
                    "required_live": False,
                }
            ],
            identities=[],
            role_assignments=[],
        )
        self._patch_live(monkeypatch, tags=self.OWNERSHIP_TAGS, resources=[])
        assert boundary.check_live(boundary.check_plan(plan_data), plan_data).ok

    def test_expands_wildcard_inventory_collection_from_live_parent(self):
        parent = "providers/microsoft.storage/storageaccounts/contosoagentsgenerated"
        assert boundary._expand_inventory_scope(
            "providers/microsoft.storage/storageaccounts/contosoagents*/blobservices/default/containers",
            {parent: {"id": "redacted"}},
        ) == [f"{parent}/blobservices/default/containers"]

    def test_arm_collection_inventory_includes_undeclared_siblings(self, monkeypatch):
        account_scope = "providers/microsoft.cognitiveservices/accounts/foundry"
        collection = f"{account_scope}/deployments"
        declared_scope = f"{collection}/travel-model"
        unexpected_scope = f"{collection}/undeclared-model"
        plan_data = plan(
            resources=[
                {"name": "account", "scope": account_scope},
                {
                    "name": "model",
                    "scope": declared_scope,
                    "inventory_api_version": "2024-10-01",
                    "inventory_collection": collection,
                },
            ]
        )
        live_resources = {
            account_scope: {
                "id": (
                    "/subscriptions/redacted/resourceGroups/rg-contoso-agents/"
                    f"{account_scope}"
                )
            }
        }
        next_link = (
            "https://management.azure.com/subscriptions/redacted/resourceGroups/"
            f"rg-contoso-agents/{collection}?page=2"
        )

        def run(arguments):
            if arguments[:2] == ["account", "show"]:
                return {"id": "redacted"}
            if arguments[:2] == ["rest", "--method"]:
                if arguments[-1] == next_link:
                    return {
                        "value": [
                            {
                                "id": (
                                    "/subscriptions/redacted/resourceGroups/"
                                    f"rg-contoso-agents/{unexpected_scope}"
                                )
                            }
                        ]
                    }
                return {
                    "value": [
                        {
                            "id": (
                                "/subscriptions/redacted/resourceGroups/"
                                f"rg-contoso-agents/{declared_scope}"
                            )
                        }
                    ],
                    "nextLink": next_link,
                }
            pytest.fail(f"unexpected Azure CLI read: {arguments}")

        monkeypatch.setattr(boundary.azure_cli, "run", run)

        boundary._augment_declared_inventory(
            boundary.check_plan(plan_data),
            plan_data,
            live_resources,
        )

        assert declared_scope in live_resources
        assert unexpected_scope in live_resources

    def test_foundry_agent_inventory_lists_undeclared_siblings(self, monkeypatch):
        agent_parent = (
            "providers/microsoft.cognitiveservices/accounts/foundry/projects/travel/agents"
        )
        declared_scope = f"{agent_parent}/contoso-travel"
        plan_data = plan(
            resources=[
                {
                    "name": "travel-agent",
                    "scope": declared_scope,
                    "inventory_data_plane": "foundry_agent",
                }
            ]
        )
        monkeypatch.setattr(
            boundary.azure_cli,
            "run",
            lambda arguments: {"accessToken": "redacted"}
            if arguments[:3] == ["account", "get-access-token", "--scope"]
            else pytest.fail(f"unexpected Azure CLI read: {arguments}"),
        )

        class Response:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "data": [
                        {"name": "contoso-travel"},
                        {"name": "undeclared-agent"},
                    ],
                    "has_more": False,
                }

        monkeypatch.setattr(boundary.requests, "get", lambda *_args, **_kwargs: Response())
        live_resources = {}

        boundary._augment_declared_inventory(
            boundary.check_plan(plan_data),
            plan_data,
            live_resources,
        )

        assert declared_scope in live_resources
        assert f"{agent_parent}/undeclared-agent" in live_resources

    def test_rejects_failed_subscription_inventory(self, monkeypatch):
        def fail(_args):
            raise boundary.azure_cli.AzureCliError("authentication failed")

        monkeypatch.setattr(boundary.azure_cli, "run", fail)
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())

        assert not report.ok
        assert "live:subscription-inventory" in failed_checks(report)

    def test_rejects_failed_resource_inventory(self, monkeypatch):
        def fail(arguments):
            if arguments[:2] == ["group", "list"]:
                return [{"name": RG, "tags": self.OWNERSHIP_TAGS}]
            if arguments[:2] == ["resource", "list"]:
                raise boundary.azure_cli.AzureCliError("resource inventory unavailable")
            raise AssertionError(f"unexpected Azure CLI read: {arguments}")

        monkeypatch.setattr(boundary.azure_cli, "run", fail)
        report = boundary.check_live(boundary.check_plan(self.owned_plan()), self.owned_plan())

        assert not report.ok
        assert "live:resource-inventory" in failed_checks(report)


class TestExistingGroupRoleAssignments:
    OWNERSHIP_TAGS = TestExistingGroupOwnership.OWNERSHIP_TAGS
    IDENTITY_ID = (
        "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
        "Microsoft.ManagedIdentity/userAssignedIdentities/field-runtime"
    )
    FOUNDRY_ID = (
        "/subscriptions/redacted/resourceGroups/rg-contoso-agents/providers/"
        "Microsoft.CognitiveServices/accounts/contoso-agents-foundry"
    )

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
            identities=[
                {
                    "name": "field-runtime",
                    "scope": "providers/Microsoft.ManagedIdentity/userAssignedIdentities/field-runtime",
                }
            ],
            role_assignments=[
                {
                    "name": "field-uses-foundry",
                    "principal": "field-runtime",
                    "role": "Foundry User",
                    "scope": "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry",
                }
            ],
        )

    def resources(self):
        return [
            {"id": self.FOUNDRY_ID},
            {
                "id": self.IDENTITY_ID,
                "identity": {"principalId": "principal-field"},
            },
        ]

    def assignment(self, *, role="Foundry User", scope=None, principal="principal-field"):
        return {
            "principalId": principal,
            "roleDefinitionName": role,
            "scope": scope or self.FOUNDRY_ID,
        }

    def check(self, monkeypatch, assignments):
        TestExistingGroupOwnership._patch_live(
            monkeypatch,
            tags=self.OWNERSHIP_TAGS,
            resources=self.resources(),
            identities=[self.resources()[1]],
            role_assignments=assignments,
        )
        plan_data = self.owned_plan()
        return boundary.check_live(boundary.check_plan(plan_data), plan_data)

    def test_accepts_exact_declared_role_principal_and_scope(self, monkeypatch):
        assert self.check(monkeypatch, [self.assignment()]).ok

    def test_accepts_an_explicit_external_principal(self, monkeypatch):
        monkeypatch.setenv("SRE_OPERATOR_GROUP_OBJECT_ID", "external-group-principal")
        plan_data = self.owned_plan()
        plan_data["external_principals"] = [
            {
                "name": "operator-group",
                "object_id_environment": "SRE_OPERATOR_GROUP_OBJECT_ID",
            }
        ]
        plan_data["role_assignments"] = [
            {
                "name": "operator-reads-foundry",
                "principal": "operator-group",
                "role": "Reader",
                "scope": "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry",
            }
        ]
        TestExistingGroupOwnership._patch_live(
            monkeypatch,
            tags=self.OWNERSHIP_TAGS,
            resources=self.resources(),
            identities=[self.resources()[1]],
            role_assignments=[
                self.assignment(role="Reader", principal="external-group-principal")
            ],
        )
        assert boundary.check_live(boundary.check_plan(plan_data), plan_data).ok

    def test_accepts_an_absent_optional_external_principal(self, monkeypatch):
        monkeypatch.delenv("SRE_OPERATOR_GROUP_OBJECT_ID", raising=False)
        plan_data = self.owned_plan()
        plan_data["external_principals"] = [
            {
                "name": "operator-group",
                "object_id_environment": "SRE_OPERATOR_GROUP_OBJECT_ID",
                "required_live": False,
            }
        ]
        plan_data["role_assignments"] = [
            {
                "name": "operator-reads-foundry",
                "principal": "operator-group",
                "role": "Reader",
                "scope": "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry",
                "required_live": False,
            }
        ]
        TestExistingGroupOwnership._patch_live(
            monkeypatch,
            tags=self.OWNERSHIP_TAGS,
            resources=self.resources(),
            identities=[self.resources()[1]],
            role_assignments=[],
        )
        assert boundary.check_live(boundary.check_plan(plan_data), plan_data).ok

    def test_accepts_an_absent_optional_role_assignment(self, monkeypatch):
        plan_data = self.owned_plan()
        plan_data["role_assignments"][0]["required_live"] = False
        TestExistingGroupOwnership._patch_live(
            monkeypatch,
            tags=self.OWNERSHIP_TAGS,
            resources=self.resources(),
            identities=[self.resources()[1]],
            role_assignments=[],
        )

        assert boundary.check_live(boundary.check_plan(plan_data), plan_data).ok

    def test_ignores_assignments_in_a_prefix_named_sibling_group(self, monkeypatch):
        sibling_scope = (
            "/subscriptions/redacted/resourceGroups/rg-contoso-agents-staging/providers/"
            "Microsoft.CognitiveServices/accounts/unrelated"
        )
        assignments = [
            self.assignment(),
            {
                "principalId": "unrelated-principal",
                "roleDefinitionName": "Reader",
                "scope": sibling_scope,
            },
        ]
        assert self.check(monkeypatch, assignments).ok

    def test_resolves_a_system_assigned_principal_from_resource_details(self, monkeypatch):
        plan_data = plan(
            allow_existing_resource_group=True,
            tags=self.OWNERSHIP_TAGS,
            resources=[
                {
                    "name": "foundry",
                    "scope": "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry",
                }
            ],
            identities=[],
            role_assignments=[
                {
                    "name": "foundry-reads-self",
                    "principal": "foundry",
                    "role": "Reader",
                    "scope": "providers/Microsoft.CognitiveServices/accounts/contoso-agents-foundry",
                }
            ],
        )
        TestExistingGroupOwnership._patch_live(
            monkeypatch,
            tags=self.OWNERSHIP_TAGS,
            resources=[{"id": self.FOUNDRY_ID}],
            role_assignments=[
                {
                    "principalId": "principal-foundry",
                    "roleDefinitionName": "Reader",
                    "scope": self.FOUNDRY_ID,
                }
            ],
            resource_details={
                self.FOUNDRY_ID: {
                    "id": self.FOUNDRY_ID,
                    "identity": {"principalId": "principal-foundry"},
                }
            },
        )
        assert boundary.check_live(boundary.check_plan(plan_data), plan_data).ok

    @pytest.mark.parametrize(
        "assignments",
        [
            [],
            [{"principalId": "principal-field", "roleDefinitionName": "Reader", "scope": FOUNDRY_ID}],
            [{"principalId": "principal-field", "roleDefinitionName": "Foundry User", "scope": "/"}],
            [{"principalId": "other-principal", "roleDefinitionName": "Foundry User", "scope": FOUNDRY_ID}],
        ],
    )
    def test_rejects_missing_or_inexact_role_assignments(self, monkeypatch, assignments):
        report = self.check(monkeypatch, assignments)
        assert "live:declared-role-assignments" in failed_checks(report)