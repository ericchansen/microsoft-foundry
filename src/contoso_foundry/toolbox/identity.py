"""Server-side identity resolution.

The single most important property of this module is what it *refuses* to do:
it will not take a scope, a region, a principal, or an employee id from a tool
argument. A tool argument is, ultimately, text that a language model produced,
and a model can be persuaded to produce different text. Scope is resolved here
from the authenticated principal's immutable directory keys and from nowhere
else.

The keys are ``oid`` (the object id of the principal) and ``tid`` (the object
id of the tenant). Both are immutable in Entra ID, which is exactly why policy
hangs off them rather than off a UPN or a mail address: those get reassigned
when someone changes their name or leaves, and a policy keyed to a recycled
identifier is a policy that silently grants the wrong person access.

Locally the fixtures use opaque ``OID-*``/``TID-*`` strings rather than real
GUIDs, because a GUID in a public repository is an identifier whether or not it
happens to be synthetic, and the repository scanner rightly refuses to
distinguish. The *contract* still names the fields ``oid`` and ``tid``, so the
shape carries over unchanged when a real token issuer replaces the fixture.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


class UnknownPrincipalError(Exception):
    """Raised when a principal cannot be resolved to a scope.

    This is the fail-closed path. It is an error rather than an empty scope so
    that it cannot be mistaken for "resolved, but entitled to nothing" - the two
    are indistinguishable in a result set and very different in a security
    review.
    """


@dataclass(frozen=True)
class Principal:
    """An authenticated caller, as the server understands them.

    Constructed from validated token claims in a deployment; constructed from a
    fixture row in a test. Either way it is never constructed from tool
    arguments.
    """

    oid: str
    tid: str

    def __post_init__(self) -> None:
        if not self.oid or not self.tid:
            # An empty key is the most likely shape of an unauthenticated call
            # slipping through, so reject it at construction rather than letting
            # it reach a query that would match nothing and look merely empty.
            raise UnknownPrincipalError("a principal requires both an oid and a tid")


@dataclass(frozen=True)
class RequestScope:
    """What one authenticated principal is allowed to see, for one request.

    Derived, never supplied. Held immutable so that a tool implementation cannot
    widen its own scope halfway through a call.
    """

    principal: Principal
    employee_id: str
    persona: str
    roles: frozenset[str] = field(default_factory=frozenset)
    regions: frozenset[str] = field(default_factory=frozenset)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def require_role(self, role: str) -> None:
        if not self.has_role(role):
            raise PermissionError(f"principal is not granted the role {role!r} required for this tool")

    def region_list(self) -> list[str]:
        """Regions in a stable order, so that generated SQL and tests are deterministic."""
        return sorted(self.regions)


class IdentityResolver:
    """Resolves a principal to a scope against the identity fixture table.

    In a deployment this class is replaced by one that reads validated claims
    and a directory group membership. The interface is the interesting part: it
    accepts a ``Principal`` and returns a ``RequestScope``, with no parameter
    through which a caller could express a preference about the answer.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def resolve(self, principal: Principal) -> RequestScope:
        row = self._connection.execute(
            "SELECT principal_oid, tenant_key, employee_id, roles, scope_regions, persona "
            "FROM identities WHERE principal_oid = ? AND tenant_key = ?",
            (principal.oid, principal.tid),
        ).fetchone()

        if row is None:
            # Deliberately identical wording whether the oid is unknown, the tid
            # is unknown, or the pair does not match: a differentiated message
            # would let a caller enumerate valid identifiers.
            raise UnknownPrincipalError("the principal could not be resolved to a scope")

        roles = frozenset(part for part in str(row[3]).split("|") if part)
        regions = frozenset(part for part in str(row[4]).split("|") if part)

        if not regions:
            raise UnknownPrincipalError("the principal resolved to an empty scope")

        return RequestScope(
            principal=principal,
            employee_id=str(row[2]),
            persona=str(row[5]),
            roles=roles,
            regions=regions,
        )


def principal_from_fixture(oid: str, tid: str) -> Principal:
    """Build a principal the way a test or the smoke client does.

    Exists so that the one place fixtures become principals is named and
    greppable, rather than scattered as ``Principal(...)`` literals that might
    later be tempted to read an argument.
    """
    return Principal(oid=oid, tid=tid)