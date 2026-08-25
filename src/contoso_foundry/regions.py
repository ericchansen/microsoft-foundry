"""Region selection by elimination, then cost-first ranking.

No region is hard-coded anywhere in this module or in the config it reads. The
candidate set starts as *every physical Azure region the subscription can see*
and is narrowed by gates. Each gate records why it removed a region and what the
evidence was, so the resulting report is an audit trail rather than an opinion.

Evidence comes from two places:

* **live** — ARM and the Azure CLI (region metadata, resource-type availability,
  model catalogue, quota). Always preferred.
* **matrix** — ``config/capability-matrix.yaml``, for capabilities Microsoft
  publishes only as documentation tables. Every entry carries a first-party
  source URL and an ``as_of`` date.
"""

from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from . import azure_cli, costs

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. A proxy for network latency, not a substitute."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class ProbeFailedError(RuntimeError):
    """A live Azure probe did not answer, so its result must not be interpreted."""


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str
    evidence: str


@dataclass
class Region:
    name: str
    display_name: str
    geography: str
    geography_group: str
    physical_location: str
    latitude: float
    longitude: float
    paired_regions: list[str] = field(default_factory=list)
    availability_zones: int = 0

    distance_km: float | None = None
    monthly_basket_usd: float | None = None
    quota_headroom: float = 0.0
    gates: list[GateResult] = field(default_factory=list)

    @property
    def eliminated(self) -> bool:
        return any(not g.passed for g in self.gates)

    @property
    def eliminated_by(self) -> list[str]:
        return [g.gate for g in self.gates if not g.passed]

    def record(self, gate: str, passed: bool, detail: str, evidence: str) -> None:
        self.gates.append(GateResult(gate, passed, detail, evidence))


def load_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def discover_regions() -> list[Region]:
    """Every physical region visible to the subscription. The starting universe."""
    rows = azure_cli.run(["account", "list-locations"]) or []
    regions = []
    for row in rows:
        meta = row.get("metadata") or {}
        if meta.get("regionType") != "Physical":
            continue
        regions.append(
            Region(
                name=row["name"],
                display_name=row.get("displayName", row["name"]),
                geography=meta.get("geography", ""),
                geography_group=meta.get("geographyGroup", ""),
                physical_location=meta.get("physicalLocation", "") or "",
                latitude=float(meta.get("latitude", 0) or 0),
                longitude=float(meta.get("longitude", 0) or 0),
                paired_regions=[p.get("name", "") for p in meta.get("pairedRegion") or []],
                availability_zones=len(row.get("availabilityZoneMappings") or []),
            )
        )
    return sorted(regions, key=lambda r: r.name)


def _normalise(display_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", display_name.lower())


def resource_type_locations(namespace: str, resource_type: str) -> set[str]:
    """Live ARM answer to 'where can I create this?', normalised for matching."""
    payload = azure_cli.try_run(["provider", "show", "-n", namespace], default=None)
    if not payload:
        return set()
    for entry in payload.get("resourceTypes", []):
        if entry.get("resourceType", "").lower() == resource_type.lower():
            return {_normalise(loc) for loc in entry.get("locations", [])}
    return set()


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


def gate_residency(regions: list[Region], config: dict[str, Any]) -> None:
    rules = config.get("residency", {})
    groups = {g.lower() for g in rules.get("allowed_geography_groups", [])}
    geos = {g.lower() for g in rules.get("allowed_geographies", [])}
    blocked = {g.lower() for g in rules.get("blocked_geographies", [])}
    for r in regions:
        if blocked and r.geography.lower() in blocked:
            r.record("residency", False, f"geography {r.geography!r} is explicitly blocked", "config")
            continue
        if groups and r.geography_group.lower() not in groups:
            r.record(
                "residency", False,
                f"geography group {r.geography_group!r} is not in the allowed set", "live: ARM region metadata",
            )
            continue
        if geos and r.geography.lower() not in geos:
            r.record(
                "residency", False,
                f"geography {r.geography!r} is not in the allowed set", "live: ARM region metadata",
            )
            continue
        r.record("residency", True, f"{r.geography} / {r.geography_group}", "live: ARM region metadata")


def gate_reliability(regions: list[Region], config: dict[str, Any]) -> None:
    rules = config.get("reliability", {})
    require_pair = rules.get("require_paired_region", True)
    min_zones = int(rules.get("minimum_availability_zones", 0))
    for r in regions:
        if r.eliminated:
            continue
        if require_pair and not r.paired_regions:
            r.record(
                "reliability", False,
                "region has no Azure paired region for geo-redundancy", "live: ARM region metadata",
            )
            continue
        if r.availability_zones < min_zones:
            r.record(
                "reliability", False,
                f"{r.availability_zones} availability zone(s), minimum is {min_zones}",
                "live: ARM availabilityZoneMappings",
            )
            continue
        r.record(
            "reliability", True,
            f"paired with {', '.join(r.paired_regions) or 'n/a'}; {r.availability_zones} availability zone(s)",
            "live: ARM region metadata",
        )


def gate_latency(regions: list[Region], config: dict[str, Any]) -> None:
    rules = config.get("latency", {})
    origin = rules.get("origin")
    if not origin:
        return
    max_km = float(rules.get("max_km", math.inf))
    for r in regions:
        r.distance_km = haversine_km(
            float(origin["latitude"]), float(origin["longitude"]), r.latitude, r.longitude
        )
        if r.eliminated:
            continue
        passed = r.distance_km <= max_km
        r.record(
            "latency",
            passed,
            f"{r.distance_km:,.0f} km from {origin.get('name', 'origin')} (limit {max_km:,.0f} km)",
            "live: ARM region latitude/longitude",
        )


def gate_resource_types(regions: list[Region], config: dict[str, Any]) -> None:
    """Every required ARM resource type must be creatable in the region."""
    for spec in config.get("resource_types", []):
        namespace, rtype = spec["namespace"], spec["type"]
        available = resource_type_locations(namespace, rtype)
        label = spec.get("label", f"{namespace}/{rtype}")
        for r in regions:
            if r.eliminated:
                continue
            if not available:
                r.record(
                    f"resource-type:{label}", False,
                    f"could not read supported locations for {namespace}/{rtype}",
                    "live: az provider show",
                )
                continue
            ok = _normalise(r.display_name) in available or _normalise(r.name) in available
            r.record(
                f"resource-type:{label}", ok,
                f"{namespace}/{rtype} is {'available' if ok else 'NOT available'}",
                "live: az provider show",
            )


def _models_in_region(region: str) -> list[dict[str, Any]] | None:
    """Model catalogue for a region, or None if the probe itself failed.

    The distinction matters: `[]` means Azure answered and the region genuinely
    offers nothing, while `None` means we never got an answer. Collapsing the two
    would silently eliminate a viable region because of a throttled request.
    """
    return azure_cli.try_run(["cognitiveservices", "model", "list", "-l", region], default=None)


def _usage_in_region(region: str) -> list[dict[str, Any]] | None:
    return azure_cli.try_run(["cognitiveservices", "usage", "list", "-l", region], default=None)


def gate_models_and_quota(regions: list[Region], config: dict[str, Any], *, workers: int = 8) -> None:
    """Probe the live model catalogue and quota for the surviving candidates.

    Deliberately runs *after* the cheap metadata gates so we only pay for the
    round-trips on regions that are still in the running.
    """
    survivors = [r for r in regions if not r.eliminated]
    if not survivors:
        return

    names = [r.name for r in survivors]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        models = dict(zip(names, pool.map(_models_in_region, names), strict=True))
        usages = dict(zip(names, pool.map(_usage_in_region, names), strict=True))

    # A probe that never answered cannot be read as "this region has nothing".
    # Abort rather than publish a ranking derived from partial data.
    unreachable = sorted(n for n in names if models.get(n) is None or usages.get(n) is None)
    if unreachable:
        raise ProbeFailedError(
            "could not read the model catalogue or quota for: "
            f"{', '.join(unreachable)}. Refusing to eliminate regions on the strength of a failed probe. "
            "Check `az login` and retry."
        )

    for spec in config.get("models", {}).get("required", []):
        pattern = re.compile(spec["name_pattern"])
        sku = spec.get("sku")
        allowed_lifecycle = set(spec.get("lifecycle_status", [])) or None
        for r in survivors:
            if r.eliminated:
                continue
            matches = []
            for entry in models.get(r.name, []):
                model = entry.get("model") or {}
                if not pattern.search(str(model.get("name", ""))):
                    continue
                if allowed_lifecycle and model.get("lifecycleStatus") not in allowed_lifecycle:
                    continue
                if sku and not any(s.get("name") == sku for s in model.get("skus") or []):
                    continue
                matches.append(f"{model.get('name')}:{model.get('version')}")
            r.record(
                f"model:{spec['id']}",
                bool(matches),
                (f"{len(matches)} matching deployment(s), e.g. {matches[0]}" if matches
                 else f"no model matching /{spec['name_pattern']}/ with SKU {sku}"),
                "live: az cognitiveservices model list",
            )

    for spec in config.get("quota", {}).get("required", []):
        pattern = re.compile(spec["usage_name_pattern"])
        minimum = float(spec["min_available"])
        for r in survivors:
            rows = [u for u in usages.get(r.name, []) if pattern.search(str((u.get("name") or {}).get("value", "")))]
            headroom = sum(float(u.get("limit") or 0) - float(u.get("currentValue") or 0) for u in rows)
            r.quota_headroom += headroom
            if r.eliminated:
                continue
            r.record(
                f"quota:{spec['id']}",
                headroom >= minimum,
                f"{headroom:,.0f} units available across {len(rows)} quota row(s); minimum {minimum:,.0f}",
                "live: az cognitiveservices usage list",
            )


def gate_capability_matrix(regions: list[Region], config: dict[str, Any], matrix: dict[str, Any]) -> None:
    """Apply capabilities that Microsoft publishes only as documentation tables."""
    capabilities = matrix.get("capabilities", {})
    for cap_id in config.get("capabilities", {}).get("required", []):
        cap = capabilities.get(cap_id)
        if cap is None:
            raise KeyError(f"required capability {cap_id!r} is missing from the capability matrix")

        source = cap.get("source", "")
        as_of = cap.get("as_of", "")
        evidence = f"matrix: {source} (as of {as_of})"

        if cap.get("derives_from"):
            allowed = {_normalise(x) for x in capabilities[cap["derives_from"]]["regions"]}
            evidence += f"; derived from {cap['derives_from']}"
        else:
            allowed = {_normalise(x) for x in cap.get("regions", [])}
        excluded = {_normalise(x) for x in cap.get("excluded_regions", [])}

        for r in regions:
            if r.eliminated:
                continue
            key = _normalise(r.display_name)
            if key in excluded:
                r.record(
                    f"capability:{cap_id}", False,
                    f"{cap.get('label', cap_id)} explicitly excludes this region", evidence,
                )
                continue
            r.record(
                f"capability:{cap_id}",
                key in allowed,
                f"{cap.get('label', cap_id)} is {'supported' if key in allowed else 'NOT listed as supported'}",
                evidence,
            )


def rank(regions: list[Region], config: dict[str, Any], client: costs.PriceClient) -> list[Region]:
    """Cost first, capacity second, latency third.

    A region that cannot be priced sorts last rather than winning by default.
    """
    basket = config.get("cost_basket", [])
    survivors = [r for r in regions if not r.eliminated]
    evidence = "live: Azure Retail Prices API"
    for r in survivors:
        total, problems = costs.price_basket(basket, region=r.name, client=client)
        r.monthly_basket_usd = None if problems else total
        if problems:
            r.record("cost-basket", True, f"not priceable: {'; '.join(problems)}", evidence)
        else:
            r.record("cost-basket", True, f"${total:,.2f}/month for the comparison basket", evidence)

    if survivors and all(r.monthly_basket_usd is None for r in survivors):
        # Cost is the primary ranking key. If nothing could be priced, the order
        # that comes out is decided entirely by quota and distance -- so the run
        # would commit to a region without the evidence it claims to rank on.
        raise ProbeFailedError(
            "no qualifying region could be priced, so the ranking has no cost signal. "
            "This is a pricing-API failure, not a result: re-run when the Retail "
            "Prices API is reachable rather than trusting this ordering."
        )

    return sorted(
        survivors,
        key=lambda r: (
            r.monthly_basket_usd if r.monthly_basket_usd is not None else math.inf,
            -r.quota_headroom,
            r.distance_km if r.distance_km is not None else math.inf,
            r.name,
        ),
    )


@dataclass
class Selection:
    generated_at: str
    regions: list[Region]
    ranked: list[Region]
    config: dict[str, Any]

    @property
    def winner(self) -> Region | None:
        return self.ranked[0] if self.ranked else None


def select(config: dict[str, Any], matrix: dict[str, Any], client: costs.PriceClient) -> Selection:
    regions = discover_regions()
    restrict = config.get("candidate_regions")
    if restrict:
        allowed = {x.lower() for x in restrict}
        regions = [r for r in regions if r.name.lower() in allowed]

    gate_residency(regions, config)
    gate_reliability(regions, config)
    gate_latency(regions, config)
    gate_resource_types(regions, config)
    gate_capability_matrix(regions, config, matrix)
    gate_models_and_quota(regions, config)
    ranked = rank(regions, config, client)

    return Selection(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        regions=regions,
        ranked=ranked,
        config=config,
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def render_internal_markdown(s: Selection) -> str:
    """Full elimination trace including subscription-scoped quota numbers."""
    lines = [
        "# Region selection (INTERNAL — contains subscription-scoped quota data)",
        "",
        f"Generated: `{s.generated_at}`",
        f"Candidates evaluated: **{len(s.regions)}** physical Azure regions",
        f"Qualifying: **{len(s.ranked)}**",
        "",
        "## Ranking — cost first, capacity second, latency third",
        "",
        "| # | Region | Basket $/mo | Quota headroom | Distance km | AZs | Paired with |",
        "| ---: | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for i, r in enumerate(s.ranked, start=1):
        cost = f"${r.monthly_basket_usd:,.2f}" if r.monthly_basket_usd is not None else "n/a"
        dist = f"{r.distance_km:,.0f}" if r.distance_km is not None else "n/a"
        lines.append(
            f"| {i} | `{r.name}` ({r.display_name}) | {cost} | {r.quota_headroom:,.0f} | "
            f"{dist} | {r.availability_zones} | {', '.join(r.paired_regions) or 'n/a'} |"
        )

    lines += ["", "## Elimination trace", ""]
    for r in sorted(s.regions, key=lambda x: x.name):
        status = "QUALIFIED" if not r.eliminated else f"eliminated by {', '.join(r.eliminated_by)}"
        lines += [
            f"### `{r.name}` — {status}", "",
            "| Gate | Result | Detail | Evidence |", "| --- | --- | --- | --- |",
        ]
        for g in r.gates:
            lines.append(f"| `{g.gate}` | {'pass' if g.passed else 'FAIL'} | {g.detail} | {g.evidence} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_decision_yaml(s: Selection) -> str:
    """The committed decision record.

    This is the one place a region name is written to a tracked file, and it is
    generated rather than typed. Committing it lets CI and later phases consume
    the decision without Azure credentials, while re-running ``foundry regions``
    turns any drift into a reviewable diff.
    """
    winner = s.winner
    if winner is None:
        raise ValueError("no region qualified, so there is no decision to record")

    runners_up = [
        {
            "region": r.name,
            "monthly_basket_usd": round(r.monthly_basket_usd, 2) if r.monthly_basket_usd is not None else None,
            "distance_km": round(r.distance_km) if r.distance_km is not None else None,
        }
        for r in s.ranked[1:6]
    ]
    document = {
        "region": winner.name,
        "display_name": winner.display_name,
        "geography": winner.geography,
        "monthly_basket_usd": round(winner.monthly_basket_usd, 2) if winner.monthly_basket_usd is not None else None,
        "distance_km": round(winner.distance_km) if winner.distance_km is not None else None,
        "availability_zones": winner.availability_zones,
        "evaluated": len(s.regions),
        "qualified": len(s.ranked),
        "runners_up": runners_up,
        "generated_at": s.generated_at,
        "generated_by": "foundry regions",
        "note": (
            "Generated file. Do not edit by hand: change config/region-requirements.yaml "
            "or config/capability-matrix.yaml and re-run `foundry regions`."
        ),
    }
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def render_public_markdown(s: Selection) -> str:
    """Sanitized summary: which regions qualified and why, no quota numbers."""
    winner = s.winner
    lines = [
        "# Region selection",
        "",
        "The deployment region is an **output** of a repeatable elimination process, not a",
        "preference. `foundry regions` starts from every physical Azure region the",
        "subscription can see and removes candidates that fail a hard requirement, then",
        "ranks the survivors by **cost first, capacity second, latency third**.",
        "",
        f"Last run: `{s.generated_at}` — **{len(s.regions)} candidates evaluated, "
        f"{len(s.ranked)} qualified**.",
        "",
    ]
    if winner:
        lines += [
            f"## Selected region: `{winner.name}` ({winner.display_name})",
            "",
            f"- Geography: {winner.geography} ({winner.geography_group})",
            f"- Paired region: {', '.join(winner.paired_regions) or 'n/a'}",
            f"- Availability zones: {winner.availability_zones}",
        ]
        if winner.distance_km is not None:
            lines.append(f"- Distance from the primary user population: {winner.distance_km:,.0f} km")
        if winner.monthly_basket_usd is not None:
            lines.append(f"- Comparison basket: ${winner.monthly_basket_usd:,.2f}/month")
        lines.append("")

    lines += [
        "## Qualifying regions, ranked",
        "",
        "| # | Region | Comparison basket $/mo | Distance km | Availability zones |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    for i, r in enumerate(s.ranked, start=1):
        cost = f"${r.monthly_basket_usd:,.2f}" if r.monthly_basket_usd is not None else "n/a"
        dist = f"{r.distance_km:,.0f}" if r.distance_km is not None else "n/a"
        lines.append(f"| {i} | `{r.name}` | {cost} | {dist} | {r.availability_zones} |")

    counts: dict[str, int] = {}
    for r in s.regions:
        for gate in r.eliminated_by:
            counts[gate] = counts.get(gate, 0) + 1
    lines += ["", "## Why candidates were removed", "", "| Gate | Regions removed |", "| --- | ---: |"]
    for gate, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| `{gate}` | {count} |")

    lines += [
        "",
        "!!! note \"Where the evidence comes from\"",
        "    Gates labelled *live* are answered by ARM and the Azure CLI at run time.",
        "    Gates labelled *matrix* come from `config/capability-matrix.yaml`, where every",
        "    entry carries a first-party source URL and an `as_of` date, because Microsoft",
        "    publishes those capabilities only as documentation tables.",
        "",
        "    Per-region quota numbers are subscription-scoped and are written to `internal/`",
        "    instead of being published here.",
        "",
    ]
    return "\n".join(lines) + "\n"


def to_json(s: Selection) -> str:
    return json.dumps(
        {
            "generated_at": s.generated_at,
            "selected": s.winner.name if s.winner else None,
            "ranked": [r.name for r in s.ranked],
            "regions": [
                {
                    "name": r.name,
                    "display_name": r.display_name,
                    "geography": r.geography,
                    "eliminated": r.eliminated,
                    "eliminated_by": r.eliminated_by,
                    "monthly_basket_usd": r.monthly_basket_usd,
                    "quota_headroom": r.quota_headroom,
                    "distance_km": r.distance_km,
                    "availability_zones": r.availability_zones,
                    "gates": [
                        {"gate": g.gate, "passed": g.passed, "detail": g.detail, "evidence": g.evidence}
                        for g in r.gates
                    ],
                }
                for r in s.regions
            ],
        },
        indent=2,
    )
