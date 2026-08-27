"""Bind platform-owned request identity to the existing scoped identity model."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from contoso_foundry import patterns
from contoso_foundry.toolbox.identity import Principal, UnknownPrincipalError


class PlatformRequestContext(Protocol):
    """The identity-bearing subset of AgentServer's request context."""

    user_id: str | None
    call_id: str | None


def _valid_platform_user_id(value: str) -> bool:
    return 0 < len(value) <= 256 and not any(character.isspace() or ord(character) < 32 for character in value)


@dataclass(frozen=True)
class PrincipalAllowlist:
    """Evaluation-only mapping from harness user IDs to canonical principals."""

    principals: Mapping[str, str]
    tenant_key: str

    @classmethod
    def from_json(cls, raw: str, *, tenant_key: str) -> PrincipalAllowlist:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as error:
            raise UnknownPrincipalError("the server principal mapping is malformed") from error

        if not isinstance(document, dict) or not document:
            raise UnknownPrincipalError("the server principal mapping is malformed")
        if not patterns.CANONICAL_TID.fullmatch(tenant_key):
            raise UnknownPrincipalError("the server tenant key is malformed")

        principals: dict[str, str] = {}
        for platform_user_id, canonical_oid in document.items():
            if not isinstance(platform_user_id, str) or not _valid_platform_user_id(platform_user_id):
                raise UnknownPrincipalError("the server principal mapping is malformed")
            if not isinstance(canonical_oid, str) or not patterns.CANONICAL_OID.fullmatch(canonical_oid):
                raise UnknownPrincipalError("the server principal mapping is malformed")
            principals[platform_user_id] = canonical_oid
        return cls(principals=principals, tenant_key=tenant_key)

    def resolve(self, platform_user_id: str | None) -> Principal:
        if not isinstance(platform_user_id, str) or not _valid_platform_user_id(platform_user_id):
            raise UnknownPrincipalError("the principal could not be resolved to a scope")
        oid = self.principals.get(platform_user_id)
        if oid is None:
            raise UnknownPrincipalError("the principal could not be resolved to a scope")
        return Principal(oid=oid, tid=self.tenant_key)


class RequestIdentityBinding:
    """Resolve one request without retaining identity in process-global state."""

    def __init__(
        self,
        context_getter: Callable[[], PlatformRequestContext],
        resolver: Callable[[PlatformRequestContext], Principal],
    ) -> None:
        self._context_getter = context_getter
        self._resolver = resolver

    @classmethod
    def from_allowlist(
        cls,
        allowlist: PrincipalAllowlist,
        context_getter: Callable[[], PlatformRequestContext],
    ) -> RequestIdentityBinding:
        """Build the deterministic test/evaluation adapter, never the hosted runtime."""
        return cls(context_getter, lambda context: allowlist.resolve(context.user_id))

    @classmethod
    def from_environment(
        cls,
        context_getter: Callable[[], PlatformRequestContext],
    ) -> RequestIdentityBinding:
        oid = os.environ.get("CONTOSO_SUPPORT_PRINCIPAL_OID", "")
        tid = os.environ.get("CONTOSO_TENANT_KEY", "")
        if not patterns.CANONICAL_OID.fullmatch(oid) or not patterns.CANONICAL_TID.fullmatch(tid):
            raise UnknownPrincipalError("the server-bound support principal is malformed")
        principal = Principal(oid=oid, tid=tid)
        # AgentServer user_id is a partitioning hint, not an authorization claim.
        # The hosted adapter therefore ignores it and binds every request to the
        # least-privilege principal fixed in deployment configuration.
        return cls(context_getter, lambda _context: principal)

    def resolve(self) -> Principal:
        context = self._context_getter()
        if not isinstance(context.call_id, str) or not _valid_platform_user_id(context.call_id):
            raise UnknownPrincipalError("the request did not arrive with trusted hosted-agent identity context")
        return self._resolver(context)
