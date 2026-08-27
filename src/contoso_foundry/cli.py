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

from . import azure_cli
from . import boundary as boundary_mod
from . import control_plane as control_plane_mod
from . import costs as costs_mod
from . import discovery as discovery_mod
from . import gateway as gateway_mod
from . import regions as regions_mod
from . import scan as scan_mod
from .support_agent.deployment import (
    DeploymentVerificationError,
    verify_deployment,
)
from .support_agent.evaluation import SupportEvaluationError
from .support_agent.evaluation import evaluate as evaluate_support

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INTERNAL = REPO_ROOT / "internal"
DEFAULT_REPORTS = REPO_ROOT / "reports"
DEFAULT_CONFIG = REPO_ROOT / "config"
DEFAULT_ESTIMATE = REPO_ROOT / "costs" / "v1-estimate.yaml"
DEFAULT_CACHE = REPO_ROOT / "costs" / "price-cache.json"
DEFAULT_DATA = REPO_ROOT / "data"
DEFAULT_SPINE_CONFIG = DEFAULT_CONFIG / "data-spine.yaml"
DEFAULT_TOOLBOX = DEFAULT_CONFIG / "toolbox"
DEFAULT_GATEWAY_CONFIG = REPO_ROOT / "config" / "gateway.yaml"
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
    config = regions_mod.enable_optional_modules(config, args.enable_module)
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
    report = costs_mod.evaluate(
        estimate,
        region=region,
        client=client,
        budget_usd=budget,
        enable_modules=set(args.enable_module),
    )
    client.save_cache()

    path = _write(Path(args.report), costs_mod.render_markdown(report))

    print(f"region: {region}")
    print(f"Azure incremental: ${report.azure_monthly_total:,.2f}/month (ceiling ${report.budget_usd:,.2f})")
    print(f"Non-Azure (verified separately): ${report.external_monthly_total:,.2f}/month")
    if report.excluded_items:
        disabled = sorted({item.module for item in report.excluded_items})
        print(f"Disabled optional modules not priced: {', '.join(disabled)}")
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
        report = boundary_mod.check_live(
            report,
            plan,
            enabled_modules=args.enable_module,
        )

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


def cmd_gateway(args: argparse.Namespace) -> int:
    config = gateway_mod.load_config(Path(args.gateway_config))
    if args.gateway_command == "render-policies":
        paths = gateway_mod.write_policy_fragments(config, Path(args.output))
        for path in paths:
            print(f"wrote {path}")
        return 0
    if args.gateway_command == "attest-model-deployments":
        verified = gateway_mod.attest_model_deployment_input(
            args.resource_group,
            args.resource_prefix,
            config,
            args.model_deployments_json,
        )
        print(f"attested existing deployments: {', '.join(verified) or '<none>'}")
        return 0

    expected_location = (
        args.location
        or os.environ.get("FOUNDRY_LOCATION")
        or _selected_region(Path(args.config))
    )
    if not expected_location:
        print(
            "Gateway verification needs --location, FOUNDRY_LOCATION, or config/selected-region.yaml.",
            file=sys.stderr,
        )
        return 2
    status = gateway_mod.collect_status(
        args.resource_group,
        args.resource_prefix,
        config,
        expected_location=expected_location,
    )
    path = _write(Path(args.internal) / "gateway-verification.json", gateway_mod.status_to_json(status))
    print(f"APIM: {status.apim_sku} in {status.apim_location} ({status.apim_state})")
    print(f"managed identity: {status.managed_identity}")
    print(
        "resource-specific gateway logs: "
        f"{status.resource_specific_logs} ({status.diagnostic_workspace_name})"
    )
    print(f"shared default connection: {status.shared_default_connection}")
    print(f"enrolled projects: {', '.join(status.enrolled_projects)}")
    print(f"verified connections: {', '.join(status.verified_connections)}")
    print(f"selectable deployments: {', '.join(status.verified_model_deployments)}")
    print(f"token policies: {', '.join(status.token_policy_projects)}")
    print(f"API policies: {', '.join(status.api_policy_routes)}")
    print(f"model policy assignments: {len(status.policy_assignments)}")
    print(
        "guardrail policy: "
        f"{status.guardrail_policy_name} "
        f"({status.guardrail_policy_mode}, filters valid: {status.guardrail_filters_valid})"
    )
    print(f"evidence: {path}")
    return 0 if status.ok else 1


def cmd_platform_inventory(args: argparse.Namespace) -> int:
    config = control_plane_mod.load_config(Path(args.platform_config))
    report = control_plane_mod.verify(
        config,
        live=not args.no_live,
        include_optional=args.include_optional,
    )
    _write(Path(args.internal) / "control-plane-inventory.json", control_plane_mod.to_json(report))
    path = _write(Path(args.reports) / "control-plane-inventory.md", control_plane_mod.render_markdown(report))
    print(control_plane_mod.render_markdown(report))
    print(f"report: {path}")
    return 0 if report.ok else 1


# --------------------------------------------------------------------------- #


def cmd_data(args: argparse.Namespace) -> int:
    """Build, verify or document the synthetic data spine.

    ``verify`` is the one CI runs. It regenerates from the seed and compares
    against the committed lock file, so a change to the generator that was not
    intended shows up as a failing build rather than as a dataset nobody
    noticed had moved.
    """
    from .data import build as build_mod
    from .data import integrity as integrity_mod
    from .data import pii as pii_mod
    from .data import provenance as prov_mod

    data_root = DEFAULT_DATA
    manifest_path = data_root / "manifest.yaml"
    lock_path = data_root / build_mod.LOCK_FILENAME

    if args.action == "manifest":
        manifest = prov_mod.load_manifest(manifest_path)
        target = _write(data_root / "MANIFEST.md", prov_mod.render_markdown(manifest))
        print(f"wrote {target}")
        return 0

    result = build_mod.build(
        config_path=DEFAULT_SPINE_CONFIG,
        seed_dir=data_root / "seed",
        out_dir=Path(args.out),
        fixtures_dir=data_root / "fixtures",
    )
    print(f"generated {result.lock['total_rows']} row(s) across {len(result.row_counts)} table(s) "
          f"into {args.out}")

    exit_code = 0

    findings = integrity_mod.check_all(result.dataset)
    print(f"integrity: {len(findings)} finding(s)")
    for finding in findings[:20]:
        print(f"  {finding}", file=sys.stderr)
    if findings:
        exit_code = 1

    reference = yaml.safe_load((data_root / "seed" / "reference.yaml").read_text(encoding="utf-8"))
    privacy = pii_mod.check_all(result.dataset, reference, use_presidio=not args.no_presidio)
    detector = "presidio + deterministic" if pii_mod.presidio_available() else "deterministic only"
    print(f"privacy ({detector}): {len(privacy)} finding(s)")
    for finding in privacy[:20]:
        print(f"  {finding}", file=sys.stderr)
    if privacy:
        exit_code = 1

    manifest = prov_mod.load_manifest(manifest_path)
    provenance = prov_mod.check_all(manifest, data_root)
    print(f"provenance: {len(provenance)} finding(s)")
    for finding in provenance[:20]:
        print(f"  {finding}", file=sys.stderr)
    if provenance:
        exit_code = 1

    if args.action == "build" and exit_code == 0:
        build_mod.write_lock(result.lock, lock_path)
        _write(data_root / "MANIFEST.md", prov_mod.render_markdown(manifest))
        print(f"wrote {lock_path} and {data_root / 'MANIFEST.md'}")
    else:
        differences = build_mod.compare_lock(build_mod.read_lock(lock_path), result.lock)
        print(f"lock: {len(differences)} difference(s)")
        for difference in differences[:20]:
            print(f"  {difference}", file=sys.stderr)
        if differences:
            exit_code = 1

    message = ("\nPASS: the data spine rebuilt exactly and every gate is clean."
               if exit_code == 0 else "\nFAIL: the data spine did not satisfy a gate.")
    print(message, file=sys.stderr if exit_code else sys.stdout)
    return exit_code


# --------------------------------------------------------------------------- #


def cmd_toolbox(args: argparse.Namespace) -> int:
    """Validate the tool contracts, or run the smoke client against them.

    ``validate`` is a static gate and needs no dataset. ``smoke`` needs one, so
    it builds the spine first rather than depending on whatever happens to be in
    ``data/build`` - a smoke test that passes against a stale database is not
    evidence of anything.
    """
    from .toolbox import contracts as contracts_mod
    from .toolbox import smoke as smoke_mod

    contracts_dir = DEFAULT_TOOLBOX

    # Load once, keeping the valid contracts even when a sibling file is broken,
    # so one bad contract reports as one finding rather than hiding the rest.
    loaded, findings = contracts_mod.load_contracts_reporting(contracts_dir)
    findings = contracts_mod.validate_loaded_contracts(loaded, findings)

    for contract in loaded:
        print(f"{contract.capability} v{contract.version}: {len(contract.tools)} tool(s)")

    tool_count = sum(len(contract.tools) for contract in loaded)
    print(f"contracts: {len(findings)} finding(s) across {len(loaded)} capability file(s), {tool_count} tool(s)")
    for finding in findings[:20]:
        print(f"  {finding}", file=sys.stderr)
    if findings:
        print("\nFAIL: a tool contract violates an invariant.", file=sys.stderr)
        return 1

    if args.action == "validate":
        print("\nPASS: every tool contract resolves scope server-side and exposes business filters only.")
        return 0

    from .data import build as build_mod

    result = build_mod.build(
        config_path=DEFAULT_SPINE_CONFIG,
        seed_dir=DEFAULT_DATA / "seed",
        out_dir=Path(args.out),
        fixtures_dir=DEFAULT_DATA / "fixtures",
    )
    smoke = smoke_mod.run_smoke(result.root / "contoso.db", contracts_dir)
    for line in smoke.lines:
        print(line)

    print(f"\ntools exercised: {len(smoke.tools_exercised)}")
    if smoke.ok():
        print("\nPASS: the same prompt returned disjoint rows for two principals, "
              "and an unknown principal was refused.")
        return 0
    print("\nFAIL: the smoke client did not demonstrate scope isolation.", file=sys.stderr)
    return 1


def cmd_support(args: argparse.Namespace) -> int:
    if args.action == "evaluate":
        results = evaluate_support(
            database_path=Path(args.database),
            config_path=Path(args.evaluation_config),
            contracts_dir=Path(args.contracts),
        )
        for result in results:
            print(f"PASS {result.id}: {result.outcome}")
        print(f"\nPASS: {len(results)} deterministic support scenario(s) satisfied.")
        return 0

    evidence = verify_deployment(
        project_endpoint=args.project_endpoint,
        agent_name=args.agent_name,
        expected_version=args.version,
    )
    print(
        f"PASS: {evidence.agent_name} version {evidence.version} is {evidence.status}; "
        f"{evidence.traffic_percentage}% traffic; {evidence.protocol}."
    )
    return 0


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
    p.add_argument(
        "--enable-module",
        action="append",
        default=[],
        help="include a disabled-by-default module in region requirements",
    )
    p.set_defaults(func=cmd_regions)

    p = sub.add_parser("costs", help="price the v1 estimate and enforce the budget ceiling")
    p.add_argument("--estimate", default=str(DEFAULT_ESTIMATE))
    p.add_argument("--region")
    p.add_argument("--budget", type=float)
    p.add_argument(
        "--enable-module",
        action="append",
        default=[],
        help="include a disabled-by-default module in pricing and the cost gate",
    )
    p.add_argument("--report", default=str(DEFAULT_REPORTS / "cost-report.md"))
    p.set_defaults(func=cmd_costs)

    p = sub.add_parser("boundary", help="validate that everything mutable stays in one resource group")
    p.add_argument("--no-live", action="store_true", help="static checks only")
    p.add_argument(
        "--enable-module",
        action="append",
        default=[],
        help="include a disabled-by-default module in the exact live inventory",
    )
    p.add_argument("--create-resource-group", action="store_true")
    p.add_argument("--confirm", action="store_true", help="actually create it (default is a dry run)")
    p.add_argument("--region")
    p.set_defaults(func=cmd_boundary)

    p = sub.add_parser("scan", help="fail if publishable content contains identifiers or secrets")
    p.add_argument("path", nargs="*", default=[str(REPO_ROOT / "site")])
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("data", help="build and verify the synthetic Contoso data spine")
    p.add_argument(
        "action",
        choices=("build", "verify", "manifest"),
        help="build regenerates and rewrites the lock; verify rebuilds and compares; "
             "manifest re-renders data/MANIFEST.md from data/manifest.yaml",
    )
    p.add_argument("--out", default=str(DEFAULT_DATA / "build"), help="where generated artifacts are written")
    p.add_argument("--no-presidio", action="store_true", help="skip the optional Presidio pass")
    p.set_defaults(func=cmd_data)

    p = sub.add_parser("platform-inventory", help="verify SRE Agent and Logic Apps Control Plane coverage")
    p.add_argument("--platform-config", default=str(DEFAULT_CONTROL_PLANE))
    p.add_argument("--no-live", action="store_true", help="validate declarations without querying Azure")
    p.add_argument(
        "--include-optional",
        action="store_true",
        help="verify disabled-by-default optional platform modules against Azure",
    )
    p.set_defaults(func=cmd_platform_inventory)

    p = sub.add_parser("gateway", help="render and verify AI gateway governance")
    p.add_argument("--gateway-config", default=str(DEFAULT_GATEWAY_CONFIG))
    gateway_sub = p.add_subparsers(dest="gateway_command", required=True)

    gateway_verify = gateway_sub.add_parser("verify", help="read back the live gateway controls")
    gateway_verify.add_argument("--resource-group", default="rg-contoso-agents")
    gateway_verify.add_argument("--resource-prefix", default="contoso-agents")
    gateway_verify.add_argument("--location")
    gateway_verify.set_defaults(func=cmd_gateway)

    gateway_render = gateway_sub.add_parser("render-policies", help="render project token policy fragments")
    gateway_render.add_argument("--output", default=str(DEFAULT_REPORTS / "gateway-policies"))
    gateway_render.set_defaults(func=cmd_gateway)

    gateway_attest = gateway_sub.add_parser(
        "attest-model-deployments",
        help="require deployment input to match the expected and live Foundry catalogs",
    )
    gateway_attest.add_argument("--resource-group", default="rg-contoso-agents")
    gateway_attest.add_argument("--resource-prefix", default="contoso-agents")
    gateway_attest.add_argument("--model-deployments-json", required=True)
    gateway_attest.set_defaults(func=cmd_gateway)

    p = sub.add_parser("toolbox", help="validate the tool contracts and smoke-test them against the data spine")
    p.add_argument(
        "action",
        choices=("validate", "smoke"),
        help="validate checks the contract invariants; smoke rebuilds the spine and calls tools as three personas",
    )
    p.add_argument("--out", default=str(DEFAULT_DATA / "build"), help="where the smoke run builds its dataset")
    p.set_defaults(func=cmd_toolbox)

    p = sub.add_parser("support", help="evaluate and verify the Contoso Support agent")
    support_sub = p.add_subparsers(dest="action", required=True)

    support_evaluate = support_sub.add_parser(
        "evaluate",
        help="run deterministic, model-free security scenarios",
    )
    support_evaluate.add_argument("--database", default=str(DEFAULT_DATA / "build" / "contoso.db"))
    support_evaluate.add_argument(
        "--evaluation-config",
        default=str(DEFAULT_CONFIG / "support-agent" / "evaluations.yaml"),
    )
    support_evaluate.add_argument("--contracts", default=str(DEFAULT_TOOLBOX))
    support_evaluate.set_defaults(func=cmd_support)

    support_verify = support_sub.add_parser(
        "verify-deployment",
        help="verify exact active version and endpoint routing",
    )
    support_verify.add_argument("--project-endpoint", required=True)
    support_verify.add_argument("--agent-name", default="contoso-support")
    support_verify.add_argument("--version", required=True)
    support_verify.set_defaults(func=cmd_support)

    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cache == "":
        args.cache = None
    try:
        return int(args.func(args))
    except (
        costs_mod.CostModelError,
        azure_cli.AzureCliError,
        gateway_mod.GatewayConfigError,
        PermissionError,
        KeyError,
        SupportEvaluationError,
        DeploymentVerificationError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - see comment
        # A refused tool call is an outcome, not a crash. Scope violations,
        # unknown principals, suppressed cohorts and contract errors all reach
        # here, and a stack trace would bury the one line that says why. The
        # exception type is named in the message so the reason stays legible.
        from .toolbox.contracts import ContractError
        from .toolbox.identity import UnknownPrincipalError
        from .toolbox.repository import CohortTooSmallError, ScopeViolationError
        from .toolbox.tools import ToolError

        if isinstance(exc, (ContractError, UnknownPrincipalError, CohortTooSmallError, ScopeViolationError, ToolError)):
            print(f"error ({type(exc).__name__}): {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
