"""Tests for the synthetic Contoso data spine.

The generator is only useful if it is boring: the same seed must produce the
same company every time, on every machine, and the result must survive the
referential, privacy and provenance gates without exception. These tests are
what make that claim checkable rather than aspirational.

Nothing here touches the network, Azure, or the committed ``data/build/``
directory — every build goes to a temporary path.
"""

from __future__ import annotations

import builtins
import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from contoso_foundry.data import build as build_mod
from contoso_foundry.data import integrity, pii, provenance
from contoso_foundry.data.generate import SeedInputs, generate, load_seed_inputs
from contoso_foundry.data.model import PERSONAL_CLASSIFICATIONS, SCHEMA, table_by_name

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = REPO_ROOT / "data"
SPINE_CONFIG = REPO_ROOT / "config" / "data-spine.yaml"
SEED_DIR = DATA_ROOT / "seed"
FIXTURES_DIR = DATA_ROOT / "fixtures"


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> build_mod.BuildResult:
    return build_mod.build(
        config_path=SPINE_CONFIG,
        seed_dir=SEED_DIR,
        out_dir=tmp_path_factory.mktemp("spine"),
        fixtures_dir=FIXTURES_DIR,
    )


@pytest.fixture(scope="module")
def reference() -> dict:
    return yaml.safe_load((SEED_DIR / "reference.yaml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #


def test_two_builds_are_byte_for_byte_identical(tmp_path: Path) -> None:
    """The acceptance criterion: one command, one seed, one dataset.

    Compared through the lock document rather than by walking the directories,
    because that is exactly what CI compares — testing a different comparison
    than the one that gates the build would prove the wrong thing.
    """
    first = build_mod.build(
        config_path=SPINE_CONFIG, seed_dir=SEED_DIR,
        out_dir=tmp_path / "a", fixtures_dir=FIXTURES_DIR,
    )
    second = build_mod.build(
        config_path=SPINE_CONFIG, seed_dir=SEED_DIR,
        out_dir=tmp_path / "b", fixtures_dir=FIXTURES_DIR,
    )
    assert build_mod.compare_lock(first.lock, second.lock) == []


def test_csv_bytes_match_across_builds(tmp_path: Path) -> None:
    """Belt and braces: compare the files themselves, not just their digests."""
    for name in ("a", "b"):
        build_mod.build(
            config_path=SPINE_CONFIG, seed_dir=SEED_DIR,
            out_dir=tmp_path / name, fixtures_dir=FIXTURES_DIR,
        )
    for left in sorted((tmp_path / "a" / "csv").glob("*.csv")):
        right = tmp_path / "b" / "csv" / left.name
        assert left.read_bytes() == right.read_bytes(), f"{left.name} differs between builds"


def test_csv_uses_lf_endings(built: build_mod.BuildResult) -> None:
    """CRLF would make the hashes platform-dependent and the lock file useless."""
    sample = (built.root / "csv" / "customers.csv").read_bytes()
    assert b"\r\n" not in sample


def test_committed_lock_matches_a_fresh_build(built: build_mod.BuildResult) -> None:
    """The committed lock file is the contract; drifting from it is a failure."""
    locked = build_mod.read_lock(DATA_ROOT / build_mod.LOCK_FILENAME)
    assert build_mod.compare_lock(locked, built.lock) == []


def test_lock_file_is_stable_json() -> None:
    text = (DATA_ROOT / build_mod.LOCK_FILENAME).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n" == text


@pytest.mark.parametrize(
    "path,value",
    [
        (("total_rows",), -1),
        (("row_counts", "customers"), -1),
        (("artifacts", "csv/customers.csv", "rows"), -1),
        (("artifacts", "csv/customers.csv", "bytes"), -1),
    ],
)
def test_lock_comparison_rejects_tampered_metadata(
    built: build_mod.BuildResult,
    path: tuple[str, ...],
    value: int,
) -> None:
    tampered = copy.deepcopy(built.lock)
    cursor = tampered
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    assert build_mod.compare_lock(tampered, built.lock)


# --------------------------------------------------------------------------- #
# Shape and volume
# --------------------------------------------------------------------------- #


def test_every_declared_table_has_rows(built: build_mod.BuildResult) -> None:
    for table in SCHEMA:
        assert built.dataset[table.name], f"{table.name} generated no rows"


def test_canonical_entities_are_all_present(built: build_mod.BuildResult) -> None:
    """The brief names ten canonical entities; each must be addressable."""
    required = {
        "customers", "employees", "products", "suppliers", "locations",
        "orders", "invoices", "support_cases", "travel_bookings", "work_orders",
    }
    assert required <= set(built.dataset)


def test_sqlite_database_is_written_and_queryable(built: build_mod.BuildResult) -> None:
    import sqlite3

    connection = sqlite3.connect(built.root / "contoso.db")
    try:
        tables = {
            row[0] for row in
            connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {table.name for table in SCHEMA} <= tables
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# Referential integrity
# --------------------------------------------------------------------------- #


def test_integrity_gate_is_clean(built: build_mod.BuildResult) -> None:
    findings = integrity.check_all(built.dataset)
    assert findings == [], "\n".join(str(f) for f in findings[:20])


def test_integrity_gate_detects_a_dangling_reference(built: build_mod.BuildResult) -> None:
    """A gate that has never failed is a gate nobody has tested."""
    broken = {name: list(rows) for name, rows in built.dataset.items()}
    broken["orders"] = [dict(row) for row in broken["orders"]]
    broken["orders"][0]["customer_id"] = "CUST-99999"

    findings = integrity.check_foreign_keys(broken)
    assert any(f.rule == "foreign-key-dangling" for f in findings)


def test_integrity_gate_detects_a_broken_rollup(built: build_mod.BuildResult) -> None:
    broken = {name: list(rows) for name, rows in built.dataset.items()}
    broken["orders"] = [dict(row) for row in broken["orders"]]
    broken["orders"][0]["order_total"] = 1.0

    findings = integrity.check_rollups(broken)
    assert any(f.rule == "order-total" for f in findings)


def test_employee_hierarchy_has_one_root_and_no_cycles(built: build_mod.BuildResult) -> None:
    roots = [e for e in built.dataset["employees"] if e["manager_id"] is None]
    assert len(roots) == 1
    assert integrity.check_hierarchy(built.dataset) == []


def test_travel_routes_are_unique_by_origin_destination_and_mode(built: build_mod.BuildResult) -> None:
    keys = {
        (row["origin_location_id"], row["destination_location_id"], row["mode"])
        for row in built.dataset["travel_routes"]
    }
    assert len(keys) == len(built.dataset["travel_routes"])


def test_employee_generation_terminates_after_the_name_cross_product(reference: dict) -> None:
    seed = load_seed_inputs(SPINE_CONFIG, SEED_DIR)
    config = copy.deepcopy(seed.config)
    config["volumes"]["employees"] = len(reference["given_names"]) * len(reference["family_names"]) + 1
    dataset = generate(SeedInputs(config=config, reference=seed.reference, identities=seed.identities))
    names = [row["full_name"] for row in dataset["employees"]]
    assert len(names) == len(set(names))
    assert any(name.endswith(" 2") for name in names)


def test_cancelled_orders_are_never_invoiced(built: build_mod.BuildResult) -> None:
    """A deliberate asymmetry, asserted so a future refactor cannot smooth it away."""
    cancelled = {o["order_id"] for o in built.dataset["orders"] if o["status"] == "cancelled"}
    assert cancelled, "the fixture needs at least one cancelled order to be interesting"
    invoiced = {i["order_id"] for i in built.dataset["invoices"]}
    assert cancelled.isdisjoint(invoiced)


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_privacy_gate_is_clean(built: build_mod.BuildResult, reference: dict) -> None:
    findings = pii.check_all(built.dataset, reference, use_presidio=False)
    assert findings == [], "\n".join(str(f) for f in findings[:20])


def test_every_person_name_comes_from_the_closed_vocabulary(
    built: build_mod.BuildResult, reference: dict
) -> None:
    allowed = pii.allowed_person_names(reference)
    names = {row["full_name"] for row in built.dataset["employees"]}
    assert names <= allowed


def test_every_telephone_number_is_in_the_fiction_block(built: build_mod.BuildResult) -> None:
    for table in SCHEMA:
        columns = [c.name for c in table.columns if c.classification == "phone"]
        for row in built.dataset[table.name]:
            for column in columns:
                assert pii.FICTIONAL_PHONE.fullmatch(str(row[column])), row[column]


def test_privacy_gate_detects_a_real_looking_name(
    built: build_mod.BuildResult, reference: dict
) -> None:
    broken = {name: list(rows) for name, rows in built.dataset.items()}
    broken["employees"] = [dict(row) for row in broken["employees"]]
    broken["employees"][0]["full_name"] = "Satya Nadella"

    findings = pii.check_person_names(broken, reference)
    assert any(f.rule == "name-outside-vocabulary" for f in findings)


def test_privacy_gate_detects_a_non_reserved_email_domain(built: build_mod.BuildResult) -> None:
    broken = {name: list(rows) for name, rows in built.dataset.items()}
    broken["employees"] = [dict(row) for row in broken["employees"]]
    broken["employees"][0]["work_email"] = "someone@gmail.com"

    findings = pii.check_emails(broken)
    assert any(f.rule == "email-domain-not-reserved" for f in findings)


def test_privacy_gate_reports_a_shared_scanner_hit_in_free_text(built: build_mod.BuildResult) -> None:
    """The shared scanner path must survive its own positive result.

    This gate exists to report a leak, so it has to produce a finding rather
    than an exception on the one input it is meant to catch. The reported
    detail must also stay quote-free, matching the site scanner's rule that a
    finding locates a hit without echoing it.
    """
    broken = {name: list(rows) for name, rows in built.dataset.items()}
    broken["support_cases"] = [dict(row) for row in broken["support_cases"]]
    broken["support_cases"][0]["subject"] = "escalate via https://contoso.openai.azure.com/ today"

    findings = pii.check_free_text(broken)

    assert any(f.rule == "scanner-azure-openai-endpoint" for f in findings)
    assert all("contoso.openai.azure.com" not in f.detail for f in findings)


def test_broken_presidio_installation_is_not_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "presidio_analyzer":
            raise RuntimeError("broken installation")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    with pytest.raises(RuntimeError, match="broken installation"):
        pii.presidio_available()


def test_example_dot_com_is_not_falsely_blocked() -> None:
    """Fixtures use example.com deliberately; the gate must not fight them.

    RFC 2606 reserves it precisely so documentation can use it, and a gate that
    rejects the reserved domain would push authors towards a domain that is not
    reserved — the opposite of the intended effect.
    """
    dataset = {"employees": [{"employee_id": "EMP-0001", "work_email": "person@example.com"}]}
    assert pii.check_emails(dataset) == []


def test_published_row_counts_match_the_committed_lock() -> None:
    overview = (REPO_ROOT / "docs" / "data" / "overview.md").read_text(encoding="utf-8")
    lock = json.loads((DATA_ROOT / "build.lock.json").read_text(encoding="utf-8"))

    for table in ("travel_routes", "travel_fares"):
        match = re.search(rf"\| `{table}` \| ([\d,]+) \|", overview)
        assert match is not None
        assert int(match.group(1).replace(",", "")) == lock["row_counts"][table]

    assert f"**{lock['total_rows']:,} rows across {len(lock['row_counts'])} tables.**" in overview


def test_scanner_columns_are_classified(built: build_mod.BuildResult) -> None:
    """Every column carrying personal data must be classified as such.

    The privacy gate keys off the classification, so an unclassified column is
    an unchecked column.
    """
    for column in table_by_name("employees").columns:
        if column.name in {"full_name", "work_email", "work_phone"}:
            assert column.classification in PERSONAL_CLASSIFICATIONS


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


def test_manifest_is_complete_and_permissively_licensed() -> None:
    manifest = provenance.load_manifest(DATA_ROOT / "manifest.yaml")
    findings = provenance.check_all(manifest, DATA_ROOT)
    assert findings == [], "\n".join(str(f) for f in findings[:20])


def test_every_committed_data_file_is_claimed_by_the_manifest() -> None:
    manifest = provenance.load_manifest(DATA_ROOT / "manifest.yaml")
    findings = provenance.check_coverage(manifest, DATA_ROOT)
    assert findings == [], "\n".join(str(f) for f in findings)


def test_restricted_datasets_are_declared_but_never_vendored() -> None:
    """The share-alike and gated corpora must appear only as fetch entries."""
    manifest = provenance.load_manifest(DATA_ROOT / "manifest.yaml")
    external_ids = {entry["id"] for entry in manifest["external"]}
    assert {"schema-guided-dialogue", "ms-marco", "gaia"} <= external_ids

    for entry in manifest["external"]:
        assert entry["vendored"] is False
        assert entry["exclusion_reason"].strip()

    committed_licences = {entry["licence"] for entry in manifest["artifacts"]}
    assert committed_licences.isdisjoint(provenance.FETCH_ONLY_LICENCES)


def test_external_entries_must_explicitly_declare_not_vendored() -> None:
    manifest = {
        "external": [
            {
                "id": "missing",
                "licence": "CC-BY-SA-4.0",
            }
        ]
    }
    findings = provenance.check_licences(manifest)
    assert any(f.rule == "external-must-not-be-vendored" for f in findings)


def test_provenance_rejects_a_claimed_artifact_that_does_not_exist(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    manifest = {"artifacts": [{"id": "missing", "destination": "data/fixtures"}]}
    findings = provenance.check_coverage(manifest, data_root)
    assert any(f.rule == "claimed-destination-missing" for f in findings)


def test_external_destinations_do_not_claim_committed_files(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    external = data_root / "external"
    external.mkdir(parents=True)
    (external / "unexpected.jsonl").write_text("{}\n", encoding="utf-8")
    manifest = {
        "external": [
            {
                "id": "external",
                "destination": "data/external",
            }
        ]
    }
    findings = provenance.check_coverage(manifest, data_root)
    assert any(f.rule == "unclaimed-file" for f in findings)


def test_provenance_gate_rejects_a_vendored_share_alike_dataset() -> None:
    manifest = {
        "artifacts": [
            {
                "id": "bad", "title": "t", "source_url": "https://example.com/x",
                "licence": "CC-BY-SA-4.0", "version": "1", "transformation": "copied",
                "destination": "data/bad",
            }
        ]
    }
    findings = provenance.check_licences(manifest)
    assert any(f.rule == "restricted-licence-vendored" for f in findings)


def test_provenance_gate_requires_an_https_source() -> None:
    manifest = {
        "artifacts": [
            {
                "id": "bad", "title": "t", "source_url": "somewhere",
                "licence": "MIT", "version": "1", "transformation": "x",
                "destination": "data/bad",
            }
        ]
    }
    findings = provenance.check_fields(manifest)
    assert any(f.rule == "source-url-not-https" for f in findings)


def test_rendered_manifest_matches_the_yaml() -> None:
    """MANIFEST.md is generated, so it must never be edited by hand."""
    manifest = provenance.load_manifest(DATA_ROOT / "manifest.yaml")
    expected = provenance.render_markdown(manifest)
    actual = (DATA_ROOT / "MANIFEST.md").read_text(encoding="utf-8")
    assert actual == expected, "run 'foundry data manifest' to regenerate data/MANIFEST.md"


def test_external_manifest_pins_a_commit_not_a_branch() -> None:
    external = yaml.safe_load((DATA_ROOT / "external" / "manifest.yaml").read_text(encoding="utf-8"))
    for dataset in external["datasets"]:
        assert dataset["committed"] is False
        ref = str(dataset["ref"])
        assert ref == "local" or len(ref) == 40, f"{dataset['id']} is not pinned to a commit"


def test_external_cache_is_not_tracked() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/external/cache/" in ignored
    assert "data/build/" in ignored
