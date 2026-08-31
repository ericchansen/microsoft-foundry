"""Immutable synthetic identity used by every Travel execution surface."""

from contoso_foundry.toolbox.identity import Principal

SYNTHETIC_TRAVEL_PRINCIPAL = Principal(
    oid="OID-EMEA-TRAVEL-01",
    tid="TID-CONTOSO-01",
)
