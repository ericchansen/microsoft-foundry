"""Bind platform-owned request identity to the existing scoped identity model."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from contoso_foundry.toolbox.identity import Principal, UnknownPrincipalError

_CANONICAL_OID = re.compile(r"^OID-[A-Z0-9][A-Z0-9-]{2,79}$")
_PLATFORM_ENVIRONMENT = (
    "FOUNDRY_PROJECT_ENDPOINT",
    "FOUNDRY_AGENT_NAME",
    "FOUNDRY_AGENT_VERSION",
    "FOUNDRY_AGENT_SESSION_ID",
)


class PlatformRequestContext(Protocol):
    """The identity-bearing subset of AgentServer's request context."""

    user_id: str | None


def _valid_platform_user_id(value: str) -> bool:
    return 0 < len(value) <= 256 and not any(character.isspace() or ord(character) < 32 for character in value)


@dataclass(frozen=True)
class PrincipalAllowlist:
    """Server-managed mapping from opaque Foundry user IDs to canonical principals."""

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
        if not tenant_key or not tenant_key.startswith("TID-"):
            raise UnknownPrincipalError("the server tenant key is malformed")

        principals: dict[str, str] = {}
        for platform_user_id, canonical_oid in document.items():
            if not isinstance(platform_user_id, str) or not _valid_platform_user_id(platform_user_id):
                raise UnknownPrincipalError("the server principal mapping is malformed")
            if not isinstance(canonical_oid, str) or not _CANONICAL_OID.fullmatch(canonical_oid):
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
        allowlist: PrincipalAllowlist,
        context_getter: Callable[[], PlatformRequestContext],
        *,
        trust_getter: Callable[[], bool],
    ) -> None:
        self._allowlist = allowlist
        self._context_getter = context_getter
        self._trust_getter = trust_getter

    @classmethod
    def from_environment(
        cls,
        context_getter: Callable[[], PlatformRequestContext],
    ) -> RequestIdentityBinding:
        allowlist = PrincipalAllowlist.from_json(
            os.environ.get("CONTOSO_PRINCIPAL_MAP_JSON", ""),
            tenant_key=os.environ.get("CONTOSO_TENANT_KEY", ""),
        )

        def hosted_request_is_trusted() -> bool:
            return all(os.environ.get(name) for name in _PLATFORM_ENVIRONMENT)

        return cls(allowlist, context_getter, trust_getter=hosted_request_is_trusted)

    def resolve(self) -> Principal:
        if not self._trust_getter():
            raise UnknownPrincipalError("the request did not arrive with trusted hosted-agent identity context")
        return self._allowlist.resolve(self._context_getter().user_id)
