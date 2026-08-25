"""``foundry`` — the Phase 0 verification CLI.

Every subcommand is safe to run against the live subscription. The only one that
can write to Azure is ``boundary --create-resource-group --confirm``, and it
refuses to run unless the ownership check passes first.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from . import boundary as boundary_mod
from . import control_plane as control_plane_mod
from . import costs as costs_mod
from . import discovery as discovery_mod
from . import regions as regions_mod
from . import scan as scan_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INTERNAL = REPO_ROOT / "internal"
DEFAULT_REPORTS = REPO_ROOT / "reports"
DEFAULT_CONFIG = REPO_ROOT / "config"
DEFAULT_ESTIMATE = REPO_ROOT / "costs" / "v1-estimate.yaml"
DEFAULT_CACHE = REPO_ROOT / "costs" / "price-cache.json"
DEFAULT_CONTROL_PLANE = DEFAULT_CONFIG / "control-plane-platforms.yaml"

#: Hostnames the published site is *expected* to link to. These are Microsoft's
#: own portals and documentation, not tenant-specific endpoints.
ALLOWED_HOSTS = frozenset(
    {
        "learn.microsoft.com",
        "azure.microsoft.com",
        "portal.azure.com",
        "make.powerapps.com",
        "admin.powerplatform.microsoft.com",
        "copilotstudio.microsoft.com",
        "ai.azure.com",
        "prices.azure.com",
        "github.com",
        "docs.github.com",
    }
)


def _client(args: argparse.Namespace) -> costs_mod.PriceClient:
    return costs_mod.PriceClient(
        currency=os.environ.get("FOUNDRY_CURRENCY", "USD"),
        cache_path=Path(args.cache) if args.cache else None,
        offline=args.offline,
    )


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _selected_region(config_dir: Path) -> str | None:
    """Read the committed region decision, if `foundry regions` has produced one."""
    path = config_dir / "selected-region.yaml"
    if not path.is_file():
        return None
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return document.get("region")


# --------------------------------------------------------------------------- #


def cmd_discover(args: argparse.Namespace) -> int:
    rg = os.environ.get("FOUNDRY_RESOURCE_GROUP", args.resource_group)
    result = discovery_mod.collect(rg)
    written = discovery_mod.write_reports(result, Path(args.internal), Path(args.reports))
    for path in written:
        print(f"wrote {path}")
    if result.errors:
        print("\nGaps encountered:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)
    return 0 if result.account else 1


def cmd_regions(args: argparse.Namespace) -> int:
    config = regions_mod.load_config(Path(args.config) / "region-requirements.yaml")
    matrix = regions_mod.load_config(Path(args.config) / "capability-matrix.yaml")
    client = _client(args)

    selection = regions_mod.select(config, matrix, client)
    client.save_cache()

    internal = Path(args.internal)
    _write(internal / "region-selection.md", regions_mod.render_internal_markdown(selection))
    _write(internal / "region-selection.json", regions_mod.to_json(selection))
    public = _write(Path(args.reports) / "region-selection.public.md", regions_mod.render_public_markdown(selection))
    if args.publish:
        _write(Path(args.publish), regions_mod.render_public_markdown(selection))

    # The decision itself is committed, so CI and later phases can consume it
    # without Azure credentials. It stays derived: re-running this command
    # rewrites the file, and a drifting answer shows up as a diff in review.
    if selection.ranked:
        _write(Path(args.config) / "selected-region.yaml", regions_mod.render_decision_yaml(selection))

    print(f"evaluated {len(selection.regions)} regions, {len(selection.ranked)} qualified")
    for i, r in enumerate(selection.ranked[: args.top], start=1):
        cost = f"${r.monthly_basket_usd:,.2f}" if r.monthly_basket_usd is not None else "unpriced"
        print(f"  {i}. {r.name:<20} basket={cost:<12} quota={r.quota_headroom:,.0f} dist={r.distance_km or 0:,.0f}km")
    print(f"\nsanitized summary: {public}")
    print(f"full trace (internal, not published): {internal / 'region-selection.md'}")

    if not selection.ranked:
        print("\nNo region satisfies every requirement.", file=sys.stderr)
        return 1
    return 0


def cmd_costs(args: argparse.Namespace) -> int:
    estimate = costs_mod.load_estimate(Path(args.estimate))
    region = (
        args.region
        or os.environ.get("FOUNDRY_LOCATION")
        or _selected_region(Path(args.config))
        or estimate.get("default_region")
    )
    if not region:
        print(
            "No region given. Pass --region, set FOUNDRY_LOCATION, or run `foundry regions` first.",
            file=sys.stderr,
        )
        return 2

    client = _client(args)
    budget = args.budget if args.budget is not None else costs_mod.budget_from_env()
    report = costs_mod.evaluate(estimate, region=region, client=client, budget_usd=budget)
    client.save_cache()

    path = _write(Path(args.report), costs_mod.render_markdown(report))

    print(f"region: {region}")
    print(f"Azure incremental: ${report.azure_monthly_total:,.2f}/month (ceiling ${report.budget_usd:,.2f})")
    print(f"Non-Azure (verified separately): ${report.external_monthly_total:,.2f}/month")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    print(f"report: {path}")

    if not report.within_budget:
        print(
            f"\nFAIL: ${report.azure_monthly_total:,.2f}/month exceeds the "
            f"${report.budget_usd:,.2f}/month ceiling.",
            file=sys.stderr,
        )
        return 1
    print("\nPASS: within budget.")
    return 0


def cmd_boundary(args: argparse.Namespace) -> int:
    plan = boundary_mod.load_plan(Path(args.config) / "boundary.yaml")
    report = boundary_mod.check_plan(plan)
    if not args.no_live:
        report = boundary_mod.check_live(report, plan)

    _write(Path(args.internal) / "boundary.json", boundary_mod.to_json(report))
    path = _write(Path(args.reports) / "boundary.md", boundary_mod.render_markdown(report))

    print(boundary_mod.render_markdown(report))
    print(f"report: {path}")

    if not report.ok:
        return 1

    if args.create_resource_group:
        # --no-live skips the live adoption check, which is precisely the check
        # that proves the group does not already exist and belong to someone
        # else. Creating on the strength of a static plan review alone would
        # defeat the gate, so the combination is refused outright.
        if args.no_live:
            print(
                "--create-resource-group cannot be combined with --no-live: creating requires "
                "the live adoption check that --no-live skips.",
                file=sys.stderr,
            )
            return 2
        location = args.region or os.environ.get("FOUNDRY_LOCATION") or _selected_region(Path(args.config))
        if not location:
            print("--create-resource-group needs --region or FOUNDRY_LOCATION", file=sys.stderr)
            return 2
        message = boundary_mod.ensure_resource_group(
            report, location, plan.get("tags", {}), dry_run=not args.confirm
        )
        print(message)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    exit_code = 0

    problems = scan_mod.check_internal_is_excluded(REPO_ROOT)
    for problem in problems:
        print(f"isolation: {problem}", file=sys.stderr)
        exit_code = 1

    for raw in args.path:
        root = Path(raw)
        if not root.exists():
            # A requested path that is missing is a failure, not a pass. Silently
            # skipping it is how a renamed build directory turns into a green
            # build that scanned nothing.
            print(f"{root} does not exist, so it was not scanned", file=sys.stderr)
            exit_code = 1
            continue
        result = scan_mod.scan_path(root, allowed_hosts=ALLOWED_HOSTS)
        print(f"{root}: scanned {result.scanned_files} file(s), {len(result.findings)} finding(s)")
        for finding in result.findings:
            print(f"  {finding}", file=sys.stderr)
        if not result.ok:
            exit_code = 1

    message = ("\nPASS: nothing publishable leaks an identifier." if exit_code == 0
               else "\nFAIL: publishable content contains identifiers or secrets.")
    print(message, file=sys.stderr if exit_code else sys.stdout)
    return exit_code


def cmd_platform_inventory(args: argparse.Namespace) -> int:
    config = control_plane_mod.load_config(Path(args.platform_config))
    report = control_plane_mod.verify(config, live=not args.no_live)
    _write(Path(args.internal) / "control-plane-inventory.json", control_plane_mod.to_json(report))
    path = _write(Path(args.reports) / "control-plane-inventory.md", control_plane_mod.render_markdown(report))
    print(control_plane_mod.render_markdown(report))
    print(f"report: {path}")
    return 0 if report.ok else 1


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foundry", description=__doc__)
    parser.add_argument("--internal", default=str(DEFAULT_INTERNAL), help="directory for identifier-bearing evidence")
    parser.add_argument("--reports", default=str(DEFAULT_REPORTS), help="directory for sanitized reports")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="configuration directory")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="retail price cache path ('' to disable)")
    parser.add_argument("--offline", action="store_true", help="use only cached retail prices")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="read-only tenant, subscription and licence inspection")
    p.add_argument("--resource-group", default="rg-contoso-agents")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("regions", help="eliminate and rank candidate Azure regions")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--publish", help="also write the sanitized summary to this path")
    p.set_defaults(func=cmd_regions)

    p = sub.add_parser("costs", help="price the v1 estimate and enforce the budget ceiling")
    p.add_argument("--estimate", default=str(DEFAULT_ESTIMATE))
    p.add_argument("--region")
    p.add_argument("--budget", type=float)
    p.add_argument("--report", default=str(DEFAULT_REPORTS / "cost-report.md"))
    p.set_defaults(func=cmd_costs)

    p = sub.add_parser("boundary", help="validate that everything mutable stays in one resource group")
    p.add_argument("--no-live", action="store_true", help="static checks only")
    p.add_argument("--create-resource-group", action="store_true")
    p.add_argument("--confirm", action="store_true", help="actually create it (default is a dry run)")
    p.add_argument("--region")
    p.set_defaults(func=cmd_boundary)

    p = sub.add_parser("scan", help="fail if publishable content contains identifiers or secrets")
    p.add_argument("path", nargs="*", default=[str(REPO_ROOT / "site")])
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("platform-inventory", help="verify SRE Agent and Logic Apps Control Plane coverage")
    p.add_argument("--platform-config", default=str(DEFAULT_CONTROL_PLANE))
    p.add_argument("--no-live", action="store_true", help="validate declarations without querying Azure")
    p.set_defaults(func=cmd_platform_inventory)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cache == "":
        args.cache = None
    try:
        return int(args.func(args))
    except (costs_mod.CostModelError, PermissionError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
