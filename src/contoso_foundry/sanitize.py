"""Redaction used before any discovery output is written outside ``internal/``.

The sanitizer is deliberately *stable but not reversible*: the same GUID always
maps to the same placeholder within a run, so a reader can still follow "identity
A is assigned to resource A", but the placeholder carries no information about
the original value.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import patterns


class Sanitizer:
    """Replaces identifiers with stable placeholders.

    >>> s = Sanitizer()
    >>> s.text("tenant 00000000-0000-0000-0000-000000000000 is live")
    'tenant <guid:...> is live'
    >>> s.text("contoso.onmicrosoft.com")
    '<entra-domain>'
    """

    def __init__(self, salt: str = "contoso-foundry") -> None:
        self._salt = salt

    def _guid_placeholder(self, value: str) -> str:
        if value.lower() in patterns.PLACEHOLDER_GUIDS:
            return value
        digest = hashlib.sha256(f"{self._salt}:{value.lower()}".encode()).hexdigest()
        return f"<guid:{digest[:6]}>"

    def text(self, value: str) -> str:
        """Redact every known identifier class in ``value``."""
        # Resource IDs first: they embed GUIDs and names we do not want to leak.
        value = patterns.IDENTIFIER_RULES[1].redaction_pattern.sub("<azure-resource-id>", value)
        value = patterns.GUID.sub(lambda m: self._guid_placeholder(m.group(0)), value)
        value = patterns.IDENTIFIER_RULES[0].redaction_pattern.sub("<entra-domain>", value)
        value = patterns.IDENTIFIER_RULES[2].redaction_pattern.sub("<subscription-name>", value)
        for rule in patterns.LIVE_ENDPOINT_RULES:
            value = rule.redaction_pattern.sub(f"<{rule.name}>", value)
        for rule in patterns.SECRET_RULES:
            value = rule.redaction_pattern.sub("<redacted-secret>", value)
        value = patterns.EMAIL.sub(self._email, value)
        return value

    @staticmethod
    def _email(match: re.Match[str]) -> str:
        domain = match.group(1).lower()
        if domain in patterns.SYNTHETIC_EMAIL_DOMAINS:
            return match.group(0)
        return "<email>"

    def value(self, value: Any) -> Any:
        """Recursively sanitize JSON-like structures, keys included."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value
