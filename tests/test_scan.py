"""The publishing gate.

These tests encode what must never reach a public site. A regression here is a
disclosure, so each rule is asserted positively (it catches the bad thing) and
negatively (it does not flag the legitimate thing), because a scanner everybody
disables for false positives protects nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contoso_foundry import patterns, scan

REAL_LOOKING_GUID = "3f2a91c4-7b0e-4d18-9c55-1ea6b83d70af"


def names(findings) -> set[str]:
    return {f.rule for f in findings}


class TestIdentifierRules:
    def test_catches_a_tenant_guid(self):
        assert "guid" in names(scan.scan_text(f"tenantId: {REAL_LOOKING_GUID}"))

    def test_catches_an_onmicrosoft_domain(self):
        found = names(scan.scan_text("signed in as admin@contosodemo.onmicrosoft.com"))
        assert "entra-default-domain" in found

    def test_catches_an_azure_resource_id(self):
        text = f"/subscriptions/{REAL_LOOKING_GUID}/resourceGroups/rg-x/providers/Microsoft.App/x"
        assert "azure-resource-id" in names(scan.scan_text(text))

    def test_resource_id_match_covers_the_whole_path(self):
        """The pattern must span the full resource ID, not just the subscription segment.

        Redaction replaces the matched span, so a pattern that stopped at the next
        `/` would leave the resource group and resource name in the published
        output while looking like it had been sanitized.
        """
        rule = next(r for r in patterns.IDENTIFIER_RULES if r.name == "azure-resource-id")
        text = f"/subscriptions/{REAL_LOOKING_GUID}/resourceGroups/rg-secret/providers/Microsoft.App/app-secret"
        matched = rule.pattern.search(text).group(0)
        assert "rg-secret" in matched
        assert "app-secret" in matched

    def test_allows_documentation_placeholder_guids(self):
        """The all-zero and all-f GUIDs are Microsoft's own doc placeholders.

        Flagging them makes it impossible to document an API response shape.
        """
        for placeholder in ("00000000-0000-0000-0000-000000000000",
                            "ffffffff-ffff-ffff-ffff-ffffffffffff"):
            assert scan.scan_text(f"id: {placeholder}") == []

    def test_allows_a_relative_scope(self):
        text = "scope: providers/Microsoft.App/managedEnvironments/contoso-agents-env"
        assert scan.scan_text(text) == []


class TestSecretRules:
    @pytest.mark.parametrize(
        "text",
        [
            "AccountKey=abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWX==",
            "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=y;",
            "-----BEGIN RSA PRIVATE KEY-----",
            "client_secret=8Kq9ZxLm2QwErTyUiOpAsDfGhJkLzXcVbNm3Q4",
        ],
    )
    def test_catches_secret_shapes(self, text: str):
        assert scan.scan_text(text), f"expected a finding for {text!r}"

    def test_excerpts_are_redacted(self):
        """A scanner that prints the secret into a public build log has leaked it."""
        secret = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWX=="
        findings = scan.scan_text(f"AccountKey={secret}")
        assert findings
        for finding in findings:
            assert secret not in finding.excerpt


class TestPersonalData:
    def test_allows_synthetic_example_addresses(self):
        assert scan.scan_text("Mail ada@example.com for details.") == []

    def test_catches_a_non_synthetic_address(self):
        assert "email" in names(scan.scan_text("Mail ada.lovelace@realcorp.io."))


class TestEndpointRules:
    def test_catches_a_tenant_specific_endpoint(self):
        text = "https://contoso-agents.openai.azure.com/openai/deployments/x"
        assert scan.scan_text(text), "a live tenant endpoint must be caught"

    def test_allows_first_party_documentation_links(self):
        text = "See https://learn.microsoft.com/azure/foundry/agents/overview for details."
        allowed = frozenset({"learn.microsoft.com"})
        assert scan.scan_text(text, allowed_hosts=allowed) == []


class TestScanPath:
    def test_walks_a_directory_and_reports_line_numbers(self, tmp_path: Path):
        (tmp_path / "a.md").write_text("clean\n", encoding="utf-8")
        (tmp_path / "b.html").write_text(f"ok\nleak {REAL_LOOKING_GUID}\n", encoding="utf-8")

        result = scan.scan_path(tmp_path)

        assert result.scanned_files == 2
        assert len(result.findings) == 1
        assert result.findings[0].line_number == 2
        assert not result.ok

    def test_skips_theme_assets(self, tmp_path: Path):
        """Minified theme bundles are noise, and we did not author them."""
        assets = tmp_path / "assets" / "javascripts"
        assets.mkdir(parents=True)
        (assets / "bundle.js").write_text(REAL_LOOKING_GUID, encoding="utf-8")

        result = scan.scan_path(tmp_path)

        assert result.scanned_files == 0
        assert result.ok

    def test_scans_a_directory_named_internal_inside_the_site(self, tmp_path: Path):
        """`internal` must not be a skip-list entry when walking build output.

        The name earns trust in the *source* tree because it is gitignored. In
        `site/` it earns nothing — anything there is about to be published, so
        skipping it would create a silent hole in the publishing gate.
        """
        nested = tmp_path / "internal"
        nested.mkdir()
        (nested / "leak.html").write_text(REAL_LOOKING_GUID, encoding="utf-8")

        result = scan.scan_path(tmp_path)

        assert result.scanned_files == 1
        assert not result.ok


class TestInternalExclusion:
    """Gate 6: identifier-bearing evidence must be structurally unpublishable."""

    def test_the_real_repository_excludes_internal(self, repo_root: Path):
        assert scan.check_internal_is_excluded(repo_root) == []

    def test_fails_when_internal_is_not_gitignored(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("site/\n", encoding="utf-8")
        (tmp_path / "mkdocs.yml").write_text("docs_dir: docs\n", encoding="utf-8")

        assert scan.check_internal_is_excluded(tmp_path)

    def test_fails_when_internal_is_inside_the_docs_tree(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("internal/*\n", encoding="utf-8")
        (tmp_path / "mkdocs.yml").write_text("docs_dir: docs\n", encoding="utf-8")
        (tmp_path / "docs" / "internal").mkdir(parents=True)

        assert scan.check_internal_is_excluded(tmp_path)
