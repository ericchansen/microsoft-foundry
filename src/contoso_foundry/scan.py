"""Public-content scanner.

Runs over the generated site (and, separately, over tracked source files) and
fails when anything that identifies the tenant, the subscription, a live
endpoint, a secret, or a real person made it into something publishable.

The scanner is deliberately blunt. A false positive costs a minute of triage; a
false negative publishes a tenant identifier to the internet permanently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import patterns

#: Extensions worth scanning. Binary assets and vendored bundles are skipped.
TEXT_SUFFIXES = frozenset(
    {".html", ".htm", ".md", ".markdown", ".json", ".yml", ".yaml", ".txt", ".xml", ".py", ".toml", ".cfg", ".ini"}
)

#: Paths never scanned: vendored theme bundles emitted by MkDocs Material, VCS
#: internals, and the quarantine directory itself.
EXCLUDED_DIR_PARTS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".ruff_cache", ".pytest_cache", "internal"}
)
EXCLUDED_PATH_FRAGMENTS = ("assets/javascripts/", "assets/stylesheets/", "assets/images/")


@dataclass
class Finding:
    path: Path
    line_number: int
    rule: str
    why: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line_number}: [{self.rule}] {self.why} -> {self.excerpt}"


@dataclass
class ScanResult:
    scanned_files: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def _redact_excerpt(text: str, start: int, end: int, *, window: int = 24) -> str:
    """Show enough context to locate the hit without reprinting the secret."""
    lead = text[max(0, start - window) : start]
    hit = text[start:end]
    tail = text[end : end + window]
    if len(hit) > 12:
        hit = f"{hit[:4]}\u2026{hit[-4:]}"
    return f"{lead}[{hit}]{tail}".strip()


def _should_scan(path: Path, root: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    parts = set(path.parts)
    if parts & EXCLUDED_DIR_PARTS:
        return False
    relative = path.relative_to(root).as_posix()
    return not any(fragment in relative for fragment in EXCLUDED_PATH_FRAGMENTS)


def _check_line(path: Path, number: int, line: str, allowed_hosts: frozenset[str]) -> list[Finding]:
    found: list[Finding] = []

    for match in patterns.GUID.finditer(line):
        if match.group(0).lower() in patterns.PLACEHOLDER_GUIDS:
            continue
        found.append(
            Finding(path, number, "guid", "a GUID, which may identify a tenant, subscription or principal",
                    _redact_excerpt(line, *match.span()))
        )

    for rule in (*patterns.IDENTIFIER_RULES, *patterns.SECRET_RULES):
        for match in rule.pattern.finditer(line):
            found.append(Finding(path, number, rule.name, rule.why, _redact_excerpt(line, *match.span())))

    for rule in patterns.LIVE_ENDPOINT_RULES:
        for match in rule.pattern.finditer(line):
            if match.group(0).lower() in allowed_hosts:
                continue
            found.append(Finding(path, number, rule.name, rule.why, _redact_excerpt(line, *match.span())))

    for match in patterns.EMAIL.finditer(line):
        if match.group(1).lower() in patterns.SYNTHETIC_EMAIL_DOMAINS:
            continue
        found.append(
            Finding(path, number, "email", "an email address on a non-synthetic domain",
                    _redact_excerpt(line, *match.span()))
        )

    for match in patterns.PHONE.finditer(line):
        if match.group(2).startswith("555"):
            continue
        found.append(
            Finding(path, number, "phone", "something shaped like a real phone number",
                    _redact_excerpt(line, *match.span()))
        )

    for match in patterns.US_SSN.finditer(line):
        found.append(Finding(path, number, "ssn", "something shaped like a national ID number",
                             _redact_excerpt(line, *match.span())))

    return found


def scan_text(
    text: str, *, path: Path = Path("<memory>"), allowed_hosts: frozenset[str] = frozenset()
) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        findings.extend(_check_line(path, number, line, allowed_hosts))
    return findings


def scan_path(root: Path, *, allowed_hosts: frozenset[str] = frozenset()) -> ScanResult:
    result = ScanResult()
    targets = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
    base = root.parent if root.is_file() else root

    for path in targets:
        if not _should_scan(path, base):
            continue
        result.scanned_files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - unreadable file
            result.findings.append(Finding(path, 0, "unreadable", str(exc), ""))
            continue
        result.findings.extend(scan_text(text, path=path, allowed_hosts=allowed_hosts))
    return result


def check_internal_is_excluded(repo_root: Path) -> list[str]:
    """Belt-and-braces: prove `internal/` cannot reach git or the built site."""
    problems: list[str] = []

    gitignore = repo_root / ".gitignore"
    if not gitignore.exists() or "internal/" not in gitignore.read_text(encoding="utf-8"):
        problems.append(".gitignore does not exclude internal/")

    docs_dir = repo_root / "docs"
    if docs_dir.exists() and (docs_dir / "internal").exists():
        problems.append("docs/internal exists, so internal evidence would be published by MkDocs")

    mkdocs = repo_root / "mkdocs.yml"
    if mkdocs.exists():
        text = mkdocs.read_text(encoding="utf-8")
        docs_dir_match = re.search(r"^docs_dir:\s*(\S+)", text, re.M)
        configured = docs_dir_match.group(1).strip("'\"") if docs_dir_match else "docs"
        if configured != "docs":
            problems.append(f"mkdocs docs_dir is {configured!r}; expected 'docs' so internal/ stays out of the build")

    return problems
