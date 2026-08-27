"""Materialise the generated dataset as CSV, JSONL, SQLite and a lock file.

Determinism is asserted over the **canonical exports**, not the SQLite file. A
``.db`` embeds page layout, free-list state and a library version, so two runs of
the same generator on different SQLite builds produce different bytes while
holding identical rows. Comparing the file would therefore fail for reasons that
have nothing to do with the data. Instead the lock file records:

* a SHA-256 per CSV and per JSONL artifact — real byte equality where bytes are
  meaningful, and
* a SHA-256 of the database's SQL dump — logical equality where they are not.

``data/build.lock.json`` is committed. That looks like it contradicts the rule
that generated output is never committed, so the distinction is worth stating:
this is a *lock file*, an assertion about what the generator must produce, in
the same category as a dependency lock. It is small, it is reviewed, and CI
rebuilds from scratch and compares against it. A drifting dataset then fails the
build instead of quietly changing what every agent believes about the company.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .generate import Dataset, SeedInputs, generate, load_seed_inputs
from .model import SCHEMA, Table

#: Bumped whenever the *layout* of the build output changes in a way that makes
#: an older lock file incomparable, as opposed to the data simply changing.
BUILD_FORMAT_VERSION = 1

LOCK_FILENAME = "build.lock.json"


class BuildError(RuntimeError):
    """Raised when the build cannot produce the expected artifacts."""


@dataclass(frozen=True)
class BuildResult:
    root: Path
    lock: dict[str, Any]
    dataset: Dataset

    @property
    def row_counts(self) -> dict[str, int]:
        return {name: len(rows) for name, rows in self.dataset.items()}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _csv_bytes(table: Table, rows: list[dict[str, Any]]) -> bytes:
    """Render one table as RFC 4180 CSV with LF endings.

    ``newline=""`` plus an explicit ``lineterminator`` is the only combination
    that produces the same bytes on Windows and Linux; the default would emit
    CRLF on one and LF on the other and the hashes would never agree.
    """
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=[column.name for column in table.columns],
        lineterminator="\n",
        extrasaction="raise",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({column.name: "" if row.get(column.name) is None else row[column.name]
                         for column in table.columns})
    return buffer.getvalue().encode("utf-8")


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    out = io.StringIO()
    for row in rows:
        out.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        out.write("\n")
    return out.getvalue().encode("utf-8")


def _sorted_rows(table: Table, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order rows by primary key so the export cannot depend on insertion order."""
    keys = [column.name for column in table.columns if column.primary_key]
    if not keys:
        return list(rows)
    return sorted(rows, key=lambda row: tuple(str(row.get(key, "")) for key in keys))


def _write_sqlite(path: Path, dataset: Dataset) -> str:
    if path.exists():
        path.unlink()

    connection = sqlite3.connect(path)
    try:
        # Enforced rather than assumed: if the generator ever emits a dangling
        # reference the insert fails here, before anything downstream trusts it.
        connection.execute("PRAGMA foreign_keys = ON")
        for table in SCHEMA:
            connection.execute(_ddl(table))

        for table in SCHEMA:
            rows = _sorted_rows(table, dataset[table.name])
            if not rows:
                continue
            columns = [column.name for column in table.columns]
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(
                f"INSERT INTO {table.name} ({', '.join(columns)}) VALUES ({placeholders})",
                [tuple(row.get(column) for column in columns) for row in rows],
            )
        connection.commit()

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise BuildError(f"SQLite reported {len(violations)} foreign key violation(s)")

        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()

    return _sha256(dump.encode("utf-8"))


def _ddl(table: Table) -> str:
    from .model import ddl

    return ddl(table)


def build(
    *,
    config_path: Path,
    seed_dir: Path,
    out_dir: Path,
    fixtures_dir: Path | None = None,
) -> BuildResult:
    """Generate and write everything. Returns the lock document."""
    seed = load_seed_inputs(config_path, seed_dir)
    dataset = generate(seed)

    missing = {table.name for table in SCHEMA} - set(dataset)
    if missing:
        raise BuildError(f"generator produced no rows for: {', '.join(sorted(missing))}")

    csv_dir = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    artifacts: dict[str, dict[str, Any]] = {}

    for table in SCHEMA:
        rows = _sorted_rows(table, dataset[table.name])
        payload = _csv_bytes(table, rows)
        target = csv_dir / f"{table.name}.csv"
        target.write_bytes(payload)
        artifacts[f"csv/{table.name}.csv"] = {
            "sha256": _sha256(payload),
            "rows": len(rows),
            "bytes": len(payload),
        }

    if fixtures_dir is not None and fixtures_dir.is_dir():
        for fixture in sorted(fixtures_dir.glob("*.jsonl")):
            payload = fixture.read_bytes()
            artifacts[f"fixtures/{fixture.name}"] = {
                "sha256": _sha256(payload),
                "rows": payload.decode("utf-8").count("\n"),
                "bytes": len(payload),
            }

    database_digest = _write_sqlite(out_dir / "contoso.db", dataset)

    lock = {
        "build_format_version": BUILD_FORMAT_VERSION,
        "seed": int(seed.config["seed"]),
        "as_of": str(seed.config["as_of"]),
        "spine_version": int(seed.config["version"]),
        "database_dump_sha256": database_digest,
        "artifacts": dict(sorted(artifacts.items())),
        "row_counts": {name: len(dataset[name]) for name in sorted(dataset)},
        "total_rows": sum(len(rows) for rows in dataset.values()),
    }

    return BuildResult(root=out_dir, lock=lock, dataset=dataset)


def write_lock(lock: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def read_lock(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise BuildError(f"no lock file at {path}; run 'foundry data build --write-lock'")
    return json.loads(path.read_text(encoding="utf-8"))


def compare_lock(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Human-readable differences, most significant first."""
    def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
        if isinstance(value, dict):
            flattened: dict[str, Any] = {}
            for key in sorted(value):
                child = f"{prefix}.{key}" if prefix else str(key)
                flattened.update(flatten(value[key], child))
            return flattened
        return {prefix: value}

    locked = flatten(expected)
    rebuilt = flatten(actual)
    problems: list[str] = []
    for field in sorted(set(locked) | set(rebuilt)):
        if field not in locked:
            problems.append(f"{field}: rebuilt but absent from lock file")
        elif field not in rebuilt:
            problems.append(f"{field}: in lock file but not rebuilt")
        elif locked[field] != rebuilt[field]:
            if field.endswith("sha256"):
                problems.append(f"{field}: digest differs")
            else:
                problems.append(f"{field}: locked {locked[field]!r}, rebuilt {rebuilt[field]!r}")
    return problems


__all__ = [
    "BUILD_FORMAT_VERSION",
    "LOCK_FILENAME",
    "BuildError",
    "BuildResult",
    "SeedInputs",
    "build",
    "compare_lock",
    "read_lock",
    "write_lock",
]
