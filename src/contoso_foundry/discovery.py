"""Read-only inspection of the tenant, subscription and licence state.

Everything here is a GET. The raw result — which is full of GUIDs, resource IDs
and the tenant's default domain — is written under ``internal/``. A sanitized
twin is written under ``reports/`` so conclusions can be quoted publicly without
a human having to eyeball a JSON blob for identifiers first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import azure_cli
from .sanitize import Sanitizer

#: Resource providers the v1 platform depends on. Registration is a tenant-level
#: prerequisite, not a mutation of anyone else's resource group, but we only
#: report on it here — registering is a later, explicitly-approved step.
REQUIRED_PROVIDERS = (
    "Microsoft.CognitiveServices",
    "Microsoft.App",
    "Microsoft.ApiManagement",
    "Microsoft.Logic",
    "microsoft.insights",
    "Microsoft.OperationalInsights",
    "Microsoft.KeyVault",
    "Microsoft.ManagedIdentity",
    "Microsoft.Storage",
)

#: Licence SKUs that gate the Microsoft 365 side of the demo. Matched against
#: ``skuPartNumber`` from Microsoft Graph.
LICENCE_SKUS_OF_INTEREST = {
    "SPE_E5": "Microsoft 365 E5",
    "SPE_E3": "Microsoft 365 E3",
    "ENTERPRISEPACK": "Office 365 E3",
    "ENTERPRISEPREMIUM": "Office 365 E5",
    "Microsoft_365_Copilot": "Microsoft 365 Copilot",
    "POWER_BI_PRO": "Power BI Pro",
    "FLOW_FREE": "Power Automate Free",
    "POWERAPPS_VIRAL": "Power Apps / Power Platform (viral)",
    "CCIBOTS_PRIVPREV_VIRAL": "Copilot Studio (viral trial)",
    "POWERAUTOMATE_ATTENDED_RPA": "Power Automate Premium",
}


@dataclass
class Discovery:
    """Everything we learned, plus everything we failed to learn and why."""

    collected_at: str
    account: dict[str, Any] = field(default_factory=dict)
    resource_groups: list[dict[str, Any]] = field(default_factory=list)
    providers: dict[str, str] = field(default_factory=dict)
    licences: list[dict[str, Any]] = field(default_factory=list)
    target_resource_group: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "collected_at": self.collected_at,
            "account": self.account,
            "resource_groups": self.resource_groups,
            "providers": self.providers,
            "licences": self.licences,
            "target_resource_group": self.target_resource_group,
            "errors": self.errors,
        }


def collect(target_resource_group: str) -> Discovery:
    """Run the read-only sweep. Never mutates Azure."""
    result = Discovery(collected_at=datetime.now(UTC).isoformat(timespec="seconds"))

    try:
        result.account = azure_cli.run(["account", "show"]) or {}
    except azure_cli.AzureCliError as exc:
        result.errors.append(f"account: {exc}")
        return result

    groups = azure_cli.try_run(["group", "list"], default=[]) or []
    result.resource_groups = [
        {"name": g.get("name"), "location": g.get("location")} for g in groups
    ]

    provider_rows = azure_cli.try_run(["provider", "list"], default=[]) or []
    by_namespace = {p.get("namespace", ""): p.get("registrationState", "") for p in provider_rows}
    result.providers = {ns: by_namespace.get(ns, "Unknown") for ns in REQUIRED_PROVIDERS}

    result.licences = _collect_licences(result)

    existing = next((g for g in result.resource_groups if g["name"] == target_resource_group), None)
    result.target_resource_group = {
        "name": target_resource_group,
        "exists": existing is not None,
        "location": (existing or {}).get("location"),
    }
    return result


def _collect_licences(result: Discovery) -> list[dict[str, Any]]:
    """Read Microsoft 365 subscribed SKUs from Graph.

    Requires the signed-in principal to be able to read ``subscribedSkus``. A
    failure here is recorded and does not abort discovery, because the Azure side
    of Phase 0 does not depend on it.
    """
    payload = azure_cli.try_run(
        ["rest", "--method", "GET", "--url", "https://graph.microsoft.com/v1.0/subscribedSkus"],
        default=None,
    )
    if payload is None:
        result.errors.append(
            "licences: could not read https://graph.microsoft.com/v1.0/subscribedSkus "
            "(needs Organization.Read.All or Directory.Read.All)"
        )
        return []

    rows = []
    for sku in payload.get("value", []):
        part = sku.get("skuPartNumber", "")
        rows.append(
            {
                "skuPartNumber": part,
                "friendlyName": LICENCE_SKUS_OF_INTEREST.get(part, part),
                "ofInterest": part in LICENCE_SKUS_OF_INTEREST,
                "capabilityStatus": sku.get("capabilityStatus"),
                "enabled": (sku.get("prepaidUnits") or {}).get("enabled"),
                "consumed": sku.get("consumedUnits"),
            }
        )
    return sorted(rows, key=lambda r: (not r["ofInterest"], r["skuPartNumber"]))


def render_internal_markdown(d: Discovery) -> str:
    """Full-fidelity report. Contains identifiers — ``internal/`` only."""
    acct = d.account
    lines = [
        "# Tenant and subscription state (INTERNAL — contains identifiers)",
        "",
        f"Collected: `{d.collected_at}`",
        "",
        "## Account",
        "",
        f"- Subscription name: `{acct.get('name')}`",
        f"- Subscription ID: `{acct.get('id')}`",
        f"- Tenant ID: `{acct.get('tenantId')}`",
        f"- Tenant default domain: `{acct.get('tenantDefaultDomain')}`",
        f"- Signed-in user: `{(acct.get('user') or {}).get('name')}`",
        f"- State: `{acct.get('state')}`",
        "",
        "## Resource groups present in the subscription",
        "",
        "| Name | Location | Status |",
        "| --- | --- | --- |",
    ]
    target = d.target_resource_group.get("name")
    for g in d.resource_groups:
        status = "TARGET" if g["name"] == target else "PROTECTED — read-only"
        lines.append(f"| `{g['name']}` | `{g['location']}` | {status} |")
    if not d.resource_groups:
        lines.append("| _none_ | | |")

    lines += ["", "## Resource provider registration", "", "| Namespace | State |", "| --- | --- |"]
    lines += [f"| `{ns}` | `{state}` |" for ns, state in d.providers.items()]

    lines += ["", "## Microsoft 365 licences", "", "| SKU | Enabled | Consumed | Status |", "| --- | --- | --- | --- |"]
    if d.licences:
        lines += [
            f"| `{r['skuPartNumber']}` ({r['friendlyName']}) | {r['enabled']} "
            f"| {r['consumed']} | {r['capabilityStatus']} |"
            for r in d.licences
        ]
    else:
        lines.append("| _not readable_ | | | |")

    if d.errors:
        lines += ["", "## Errors", ""] + [f"- {e}" for e in d.errors]
    return "\n".join(lines) + "\n"


def render_public_markdown(d: Discovery) -> str:
    """Sanitized conclusions only. Safe to paste into a public page."""
    acct = d.account
    target = d.target_resource_group
    interesting = [r for r in d.licences if r["ofInterest"]]

    lines = [
        "# Environment verification (sanitized)",
        "",
        f"Collected: `{d.collected_at}`",
        "",
        "This file is generated by `foundry discover`. Identifiers are redacted at",
        "write time; the unredacted evidence stays in `internal/`.",
        "",
        "## Conclusions",
        "",
        f"- Azure CLI is signed in and targeting a single subscription in state `{acct.get('state', 'unknown')}`.",
        f"- The subscription contains **{len(d.resource_groups)} resource group(s)**, "
        f"of which **{len(d.resource_groups) - (1 if target.get('exists') else 0)}** are outside the "
        "ownership boundary and are therefore read-only for this project.",
        f"- Target resource group `{target.get('name')}`: "
        + ("**already exists**" if target.get("exists") else "**does not exist yet** — the boundary is clean."),
        "",
        "## Resource provider readiness",
        "",
        "| Namespace | State |",
        "| --- | --- |",
    ]
    lines += [f"| `{ns}` | `{state}` |" for ns, state in d.providers.items()]

    lines += ["", "## Microsoft 365 licence capacity", ""]
    if interesting:
        lines += ["| Product | Enabled units | Consumed units | Status |", "| --- | --- | --- | --- |"]
        lines += [
            f"| {r['friendlyName']} | {r['enabled']} | {r['consumed']} | {r['capabilityStatus']} |"
            for r in interesting
        ]
    else:
        lines.append("_No licence data available to the signed-in principal._")

    if d.errors:
        lines += ["", "## Gaps", ""] + [f"- {e}" for e in d.errors]

    sanitizer = Sanitizer()
    return sanitizer.text("\n".join(lines)) + "\n"


def write_reports(d: Discovery, internal_dir: Path, reports_dir: Path) -> list[Path]:
    internal_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    written = []
    raw = internal_dir / "tenant-state.json"
    raw.write_text(json.dumps(d.to_dict(), indent=2), encoding="utf-8")
    written.append(raw)

    detailed = internal_dir / "tenant-state.md"
    detailed.write_text(render_internal_markdown(d), encoding="utf-8")
    written.append(detailed)

    public = reports_dir / "tenant-state.public.md"
    public.write_text(render_public_markdown(d), encoding="utf-8")
    written.append(public)
    return written
