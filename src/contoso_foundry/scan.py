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
    {
        ".html", ".htm", ".md", ".markdown", ".json", ".yml", ".yaml", ".txt",
        ".xml", ".py", ".toml", ".cfg", ".ini",
        # Asset types are scanned too. A published bundle is as readable as a
        # page, and an authored `extra_javascript` file or a plugin-generated
        # search index is exactly the kind of place a stray endpoint or key
        # survives review.
        ".js", ".mjs", ".css", ".svg",
    }
)

#: Paths never scanned: VCS and tooling internals only.
#:
#: `internal` is deliberately NOT in this set. Excluding any directory component
#: with that name would create a blind spot exactly where it hurts most: a stray
#: `site/internal/leak.html` would be silently skipped by the CI scan of the
#: generated site. The quarantine directory is kept out of the build by
#: `check_internal_is_excluded` and out of git by `.gitignore`; the scanner's job
#: is to read whatever it is pointed at, without exceptions.
#:
#: Vendored theme bundles under `assets/` are likewise NOT excluded, for the same
#: reason: "it is only an asset" is an assumption, and the gate exists precisely
#: to stop assumptions from reaching a public site.
EXCLUDED_DIR_PARTS = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", ".ruff_cache", ".pytest_cache"}
)


@dataclass
class Finding:
    path: Path
    line_number: int
    rule: str
    why: str
    column: int

    def __str__(self) -> str:
        # Deliberately no excerpt. Path, line and column locate the hit exactly,
        # and the rule name says what was found -- so echoing the matched text
        # would add nothing except a copy of the secret in a public CI log.
        return f"{self.path}:{self.line_number}:{self.column}: [{self.rule}] {self.why}"


@dataclass
class ScanResult:
    scanned_files: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


#: The ONLY paths skipped inside build output: third-party libraries vendored by
#: the theme, whose bytes are identical for every mkdocs-material install and
#: contain no project content. `lunr/` ships TinySegmenter, which carries its
#: author's email address and would otherwise fail every build.
#:
#: This replaces a far broader `assets/javascripts/`, `assets/stylesheets/`,
#: `assets/images/` exclusion. That version also skipped files we author
#: (`extra_javascript`) and files plugins generate from our content, which is
#: exactly where a stray endpoint or key would survive review.
VENDORED_PATH_PREFIXES = ("assets/javascripts/lunr/",)


def _is_vendored(path: Path) -> bool:
    posix = path.as_posix()
    return any(prefix in posix for prefix in VENDORED_PATH_PREFIXES)


def _should_scan(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    if set(path.parts) & EXCLUDED_DIR_PARTS:
        return False
    return not _is_vendored(path)


def _check_line(path: Path, number: int, line: str, allowed_hosts: frozenset[str]) -> list[Finding]:
    found: list[Finding] = []

    def hit(match: re.Match[str], rule: str, why: str) -> Finding:
        return Finding(path, number, rule, why, match.start() + 1)

    for match in patterns.GUID.finditer(line):
        if match.group(0).lower() in patterns.PLACEHOLDER_GUIDS:
            continue
        found.append(
            hit(match, "guid", "a GUID, which may identify a tenant, subscription or principal")
        )

    for rule in (*patterns.IDENTIFIER_RULES, *patterns.SECRET_RULES):
        for match in rule.pattern.finditer(line):
            found.append(hit(match, rule.name, rule.why))

    for rule in patterns.LIVE_ENDPOINT_RULES:
        for match in rule.pattern.finditer(line):
            if match.group(0).lower() in allowed_hosts:
                continue
            found.append(hit(match, rule.name, rule.why))

    for match in patterns.EMAIL.finditer(line):
        if match.group(1).lower() in patterns.SYNTHETIC_EMAIL_DOMAINS:
            continue
        found.append(hit(match, "email", "an email address on a non-synthetic domain"))

    for match in patterns.PHONE.finditer(line):
        if patterns.is_fictional_phone(match):
            continue
        found.append(hit(match, "phone", "something shaped like a real phone number"))

    for match in patterns.US_SSN.finditer(line):
        found.append(hit(match, "ssn", "something shaped like a national ID number"))

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

    for path in targets:
        if not _should_scan(path):
            continue
        result.scanned_files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - unreadable file
            result.findings.append(Finding(path, 0, "unreadable", str(exc), 0))
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
    if docs_dir.exists():
        # Nested as well as top-level: docs/platform/internal/ would be published
        # just as readily as docs/internal/.
        for nested in docs_dir.rglob("internal"):
            if nested.is_dir():
                rel = nested.relative_to(repo_root).as_posix()
                problems.append(f"{rel} exists, so internal evidence would be published by MkDocs")

    mkdocs = repo_root / "mkdocs.yml"
    if mkdocs.exists():
        text = mkdocs.read_text(encoding="utf-8")
        docs_dir_match = re.search(r"^docs_dir:\s*(\S+)", text, re.M)
        configured = docs_dir_match.group(1).strip("'\"") if docs_dir_match else "docs"
        if configured != "docs":
            problems.append(f"mkdocs docs_dir is {configured!r}; expected 'docs' so internal/ stays out of the build")

    return problems
