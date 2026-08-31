"""Trusted request identity resolution for the hosted Research protocol."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

CANONICAL_PERSONA_ROUTES = frozenset(
    {
        "americas-supply-planner",
        "americas-support-lead",
        "emea-travel-coordinator",
    }
)

EVALUATION_USER_ROUTES = MappingProxyType(
    {
        "contoso-user-americas-supply-planner": "americas-supply-planner",
        "contoso-user-americas-support-lead": "americas-support-lead",
        "contoso-user-emea-travel-coordinator": "emea-travel-coordinator",
    }
)


class PlatformIdentityContext(Protocol):
    """Subset of the first-party hosted context used for authorization."""

    user_id_key: str | None
    call_id: str | None


class HostedIdentityError(RuntimeError):
    """Raised when platform context cannot map to a synthetic principal."""


@dataclass(frozen=True, slots=True)
class TrustedRequestContext:
    """Server-resolved context; callers cannot directly select a persona."""

    caller_route: str
    call_id: str


def load_trusted_user_routes(raw: str) -> Mapping[str, str]:
    """Parse an immutable server allow-list without logging its opaque keys."""
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HostedIdentityError(
            "hosted user-route allow-list must be valid JSON"
        ) from exc
    if not isinstance(document, dict) or not document:
        raise HostedIdentityError(
            "hosted user-route allow-list must be a nonempty object"
        )

    routes: dict[str, str] = {}
    for user_id_key, caller_route in document.items():
        if not isinstance(user_id_key, str) or not user_id_key.strip():
            raise HostedIdentityError(
                "hosted user-route allow-list contains an invalid user key"
            )
        if caller_route not in CANONICAL_PERSONA_ROUTES:
            raise HostedIdentityError(
                "hosted user-route allow-list contains an unknown persona route"
            )
        routes[user_id_key] = caller_route
    return MappingProxyType(routes)


def trusted_user_routes_from_environment() -> Mapping[str, str]:
    """Load the deployment-owned allow-list once during server startup."""
    raw = os.environ.get("CONTOSO_RESEARCH_USER_ROUTE_ALLOWLIST", "")
    if not raw:
        raise HostedIdentityError(
            "CONTOSO_RESEARCH_USER_ROUTE_ALLOWLIST is required"
        )
    return load_trusted_user_routes(raw)


def resolve_trusted_request(
    context: PlatformIdentityContext,
    trusted_user_routes: Mapping[str, str],
) -> TrustedRequestContext:
    """Map platform-minted context through the immutable server allow-list."""
    user_id_key = context.user_id_key
    call_id = context.call_id
    if not isinstance(user_id_key, str) or not user_id_key.strip():
        raise HostedIdentityError("hosted request identity is required")
    if not isinstance(call_id, str) or not call_id.strip():
        raise HostedIdentityError("hosted request call context is required")

    caller_route = trusted_user_routes.get(user_id_key)
    if caller_route is None:
        raise HostedIdentityError("hosted request identity is not authorized")
    return TrustedRequestContext(caller_route=caller_route, call_id=call_id)


def resolve_fixed_request(
    context: PlatformIdentityContext,
    caller_route: str,
) -> TrustedRequestContext:
    """Require platform identity context while binding scope only from deployment."""
    if caller_route not in CANONICAL_PERSONA_ROUTES:
        raise HostedIdentityError("hosted deployment route is invalid")
    if not isinstance(context.user_id_key, str) or not context.user_id_key.strip():
        raise HostedIdentityError("hosted request identity is required")
    if not isinstance(context.call_id, str) or not context.call_id.strip():
        raise HostedIdentityError("hosted request call context is required")
    return TrustedRequestContext(caller_route=caller_route, call_id=context.call_id)
