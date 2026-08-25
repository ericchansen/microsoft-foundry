"""Redaction: the difference between detecting a secret and removing it.

A detection regex answers "is something here?". A redaction span answers "what
must be deleted?". Conflating the two produces output that *looks* sanitized --
the marker is gone, the value is still on the page -- which is the worst possible
failure mode for a public artifact.
"""

from __future__ import annotations

from contoso_foundry import patterns
from contoso_foundry.sanitize import Sanitizer


class TestSecretRedaction:
    def test_removes_the_storage_account_key_not_just_the_marker(self):
        secret = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ=="
        out = Sanitizer().text(f"AccountKey={secret};EndpointSuffix=core.windows.net")
        assert secret not in out

    def test_removes_the_shared_access_key(self):
        secret = "9Kq2ZxLm4QwErTyUiOpAsDfGhJkLzXcVbNm3Q4RtYu="
        out = Sanitizer().text(f"Endpoint=sb://x;SharedAccessKey={secret}")
        assert secret not in out

    def test_removes_the_private_key_body(self):
        body = "MIIEowIBAAKCAQEAxK9fakekeymaterialdoNOTuse1234567890"
        pem = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
        out = Sanitizer().text(pem)
        assert body not in out

    def test_removes_a_sql_password(self):
        out = Sanitizer().text("Server=tcp:db.example.com;Password=Sup3rSecretValue123")
        assert "Sup3rSecretValue123" not in out

    def test_every_secret_rule_that_matches_also_redacts(self):
        """No rule may detect a value it cannot remove."""
        samples = {
            "storage-connection-string": "AccountKey=QUJDREVGR0hJSktMTU5PUFFSU1Q9PQ==",
            "servicebus-connection-string": "SharedAccessKey=QUJDREVGR0hJSktMTU5P",
            "sql-connection-string": "Password=Sup3rSecretValue123",
        }
        s = Sanitizer()
        for rule in patterns.SECRET_RULES:
            sample = samples.get(rule.name)
            if sample is None:
                continue
            assert rule.pattern.search(sample), f"{rule.name} should detect its own sample"
            assert not rule.pattern.search(s.text(sample)), (
                f"{rule.name} detects {sample!r} but does not fully redact it"
            )


class TestIdentifierRedaction:
    def test_removes_the_whole_resource_id_not_only_the_subscription(self):
        rid = (
            "/subscriptions/00000000-0000-0000-0000-000000000001"
            "/resourceGroups/rg-secret-name/providers/Microsoft.Web/sites/secret-app"
        )
        out = Sanitizer().text(f"id: {rid}")
        assert "rg-secret-name" not in out
        assert "secret-app" not in out

    def test_removes_the_tenant_domain(self):
        assert "contoso" not in Sanitizer().text("contoso.onmicrosoft.com")

    def test_keeps_placeholder_guids_readable(self):
        placeholder = "00000000-0000-0000-0000-000000000000"
        assert placeholder in Sanitizer().text(f"tenant {placeholder}")

    def test_the_same_guid_maps_to_the_same_placeholder(self):
        s = Sanitizer()
        guid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        assert s.text(guid) == s.text(guid)

    def test_different_guids_map_to_different_placeholders(self):
        s = Sanitizer()
        a = s.text("3f2504e0-4f89-11d3-9a0c-0305e82c3301")
        b = s.text("3f2504e0-4f89-11d3-9a0c-0305e82c3302")
        assert a != b


class TestFictionalPhones:
    def test_the_reserved_range_is_allowed(self):
        for line in ("0100", "0150", "0199"):
            match = patterns.PHONE.search(f"call 212-555-{line}")
            assert match and patterns.is_fictional_phone(match)

    def test_a_live_555_number_is_not_allowed(self):
        """Only 555-0100..0199 is reserved; the rest of the exchange is assignable."""
        for line in ("9999", "1212", "0099", "0200"):
            match = patterns.PHONE.search(f"call 212-555-{line}")
            assert match and not patterns.is_fictional_phone(match)
