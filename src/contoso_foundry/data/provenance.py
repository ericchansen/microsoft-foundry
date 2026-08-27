"""Provenance and licence checks for everything under ``data/``.

The rule this enforces is narrow and absolute: nothing in the data layer exists
without a record saying where it came from, what licence governs it, which
version was used, and what was done to it. A dataset whose origin is unknown
cannot be published, and "I think it was synthetic" is not an origin.

The manifest is deliberately machine-readable YAML with a rendered Markdown
companion rather than prose alone, because a completeness check over prose is
guesswork. The checks below are the reason the format is worth the friction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Licences whose terms permit inclusion in a public repository under this
#: project's own licence. Anything share-alike, non-commercial or gated is
#: absent on purpose: those may be *fetched* by a pinned script at evaluation
#: time, never vendored.
ALLOWED_LICENCES = frozenset({
    "MIT",
    "Apache-2.0",
    "BSD-3-Clause",
    "CC0-1.0",
    "CC-BY-4.0",
    "IETF-Trust-RFC",
    "Microsoft-Documentation",
    "Generated-Synthetic",
})

#: Licences that may appear only on an *external, fetched* entry. Recording them
#: is how the repository states what it deliberately does not carry.
FETCH_ONLY_LICENCES = frozenset({
    "CC-BY-SA-4.0",
    "CC-BY-NC-4.0",
    "MSMARCO-Non-Commercial",
    "Gated-Registration-Required",
    "Other-Restricted",
})

REQUIRED_FIELDS = ("id", "title", "source_url", "licence", "version", "transformation", "destination")

#: Every source must be a resolvable https URL. A bare citation is not a source.
URL = re.compile(r"^https://[^\s)]+$")


@dataclass(frozen=True)
class ProvenanceFinding:
    entry: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.entry}: {self.rule}: {self.detail}"


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"no data manifest at {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return list(manifest.get("artifacts", [])) + list(manifest.get("external", []))


def check_fields(manifest: dict[str, Any]) -> list[ProvenanceFinding]:
    findings: list[ProvenanceFinding] = []
    for entry in _entries(manifest):
        name = str(entry.get("id", "<unnamed>"))
        for field in REQUIRED_FIELDS:
            value = entry.get(field)
            if value is None or str(value).strip() == "":
                findings.append(ProvenanceFinding(name, "missing-field", field))
        url = str(entry.get("source_url", ""))
        if url and not URL.fullmatch(url):
            findings.append(ProvenanceFinding(name, "source-url-not-https", url))
    return findings


def check_licences(manifest: dict[str, Any]) -> list[ProvenanceFinding]:
    findings: list[ProvenanceFinding] = []
    for entry in manifest.get("artifacts", []):
        name = str(entry.get("id", "<unnamed>"))
        licence = str(entry.get("licence", ""))
        if licence in FETCH_ONLY_LICENCES:
            findings.append(
                ProvenanceFinding(name, "restricted-licence-vendored",
                                  f"{licence} may be fetched at run time but never committed")
            )
        elif licence not in ALLOWED_LICENCES:
            findings.append(ProvenanceFinding(name, "licence-not-allowed", licence))

    for entry in manifest.get("external", []):
        name = str(entry.get("id", "<unnamed>"))
        if entry.get("vendored") is not False:
            findings.append(
                ProvenanceFinding(
                    name,
                    "external-must-not-be-vendored",
                    "vendored must be present and exactly false",
                )
            )
        if entry.get("licence") in ALLOWED_LICENCES | FETCH_ONLY_LICENCES:
            continue
        findings.append(ProvenanceFinding(name, "licence-unrecognised", str(entry.get("licence"))))
    return findings


def check_coverage(manifest: dict[str, Any], data_root: Path) -> list[ProvenanceFinding]:
    """Every committed file under ``data/`` must be claimed by an entry.

    Claiming is by prefix, so one entry can cover a directory of seed files, but
    an unclaimed file is a failure. That is what stops a dataset from arriving
    quietly through a merge.
    """
    findings: list[ProvenanceFinding] = []
    claimed: list[str] = []
    repository_root = data_root.parent.resolve()
    for entry in manifest.get("artifacts", []):
        destination = entry.get("destination")
        if destination:
            normalized = str(destination).replace("\\", "/").strip("/")
            claimed.append(normalized)
            target = (repository_root / normalized).resolve()
            try:
                target.relative_to(repository_root)
            except ValueError:
                findings.append(
                    ProvenanceFinding(str(entry.get("id", "<unnamed>")), "destination-outside-repository", normalized)
                )
            else:
                if not target.exists():
                    findings.append(
                        ProvenanceFinding(
                            str(entry.get("id", "<unnamed>")),
                            "claimed-destination-missing",
                            normalized,
                        )
                    )

    ignored_parts = {"build", "cache", "__pycache__"}
    for path in sorted(data_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(data_root.parent).as_posix()
        if any(part in ignored_parts for part in path.parts):
            continue
        if not any(relative == c or relative.startswith(f"{c.rstrip('/')}/") for c in claimed):
            findings.append(ProvenanceFinding(relative, "unclaimed-file",
                                              "no manifest entry covers this path"))
    return findings


def check_all(manifest: dict[str, Any], data_root: Path | None = None) -> list[ProvenanceFinding]:
    findings = check_fields(manifest) + check_licences(manifest)
    if data_root is not None and data_root.is_dir():
        findings.extend(check_coverage(manifest, data_root))
    return findings


def render_markdown(manifest: dict[str, Any]) -> str:
    """Render the manifest as the committed ``data/MANIFEST.md``.

    Generated from the YAML so the two cannot disagree. A hand-maintained
    Markdown table beside a machine-readable manifest drifts within a month.
    """
    lines: list[str] = [
        "<!-- Generated by 'foundry data manifest'. Edit data/manifest.yaml instead. -->",
        "",
        "# Data provenance",
        "",
        str(manifest.get("preamble", "")).strip(),
        "",
        "## Committed artifacts",
        "",
        "| Artifact | Source | Licence | Version | Transformation | Destination |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in manifest.get("artifacts", []):
        lines.append(
            "| {title} | [{host}]({url}) | {licence} | {version} | {transformation} | `{destination}` |".format(
                title=entry.get("title", ""),
                host=str(entry.get("source_label") or entry.get("source_url", "")),
                url=entry.get("source_url", ""),
                licence=entry.get("licence", ""),
                version=entry.get("version", ""),
                transformation=entry.get("transformation", ""),
                destination=entry.get("destination", ""),
            )
        )

    lines += [
        "",
        "## External datasets, fetched but never committed",
        "",
        str(manifest.get("external_preamble", "")).strip(),
        "",
        "| Dataset | Source | Licence | Pinned version | Why it is not committed |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in manifest.get("external", []):
        lines.append(
            "| {title} | [{host}]({url}) | {licence} | `{version}` | {reason} |".format(
                title=entry.get("title", ""),
                host=str(entry.get("source_label") or entry.get("source_url", "")),
                url=entry.get("source_url", ""),
                licence=entry.get("licence", ""),
                version=entry.get("version", ""),
                reason=entry.get("exclusion_reason", ""),
            )
        )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "ALLOWED_LICENCES",
    "FETCH_ONLY_LICENCES",
    "ProvenanceFinding",
    "check_all",
    "load_manifest",
    "render_markdown",
]
