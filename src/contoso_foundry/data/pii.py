"""Privacy gate for the generated dataset.

Pattern matching alone is the wrong primitive here. A regex can tell you that
``Priya Raman`` looks like a person's name, but not whether it is a *real* one,
and the answer this project needs is the second. So the strongest check runs the
other way round: every value in a person-name column must come from the closed
vocabulary in ``data/seed/reference.yaml``. Anything else — a name pasted in by
hand, a name pulled from an external corpus, a name that arrived through a
merge — fails, regardless of whether a detector recognises it.

The pattern checks are still here, as a second line for the columns that are not
drawn from a vocabulary: free text is scanned with the same engine the public
site scanner uses, so the data layer cannot become a hole in a gate the docs
already pass.

Microsoft Presidio is supported but optional. It is a large dependency for a
repository that deliberately runs on ``PyYAML`` and ``requests``, so it is an
extra rather than a requirement, and its absence downgrades to a skip rather
than a failure. When it is installed it runs over free text only, because that
is the sole place an unexpected entity could appear.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..patterns import SYNTHETIC_EMAIL_DOMAINS, US_SSN
from ..scan import scan_text
from .model import EMAIL, FREE_TEXT, ORG_NAME, PERSON_NAME, PHONE, SCHEMA

Dataset = dict[str, list[dict[str, Any]]]

#: The North American fiction block reserved by the ITU and the NANPA for
#: dramatic use: 555-0100 through 555-0199. Nothing else is acceptable.
FICTIONAL_PHONE = re.compile(r"^\+1 \d{3}-555-01\d{2}$")

#: Payment-card shapes, checked independently of the secret scanner because a
#: synthetic dataset is exactly where a "realistic" test card tends to appear.
CARD_LIKE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

#: Entities Presidio may report without it being a defect. Everything in this
#: dataset is a fictional organisation in a fictional place on a made-up date.
PRESIDIO_ALLOWED = frozenset({
    "DATE_TIME", "ORGANIZATION", "LOCATION", "NRP", "URL", "IP_ADDRESS",
    "US_DRIVER_LICENSE",
})


@dataclass(frozen=True)
class PrivacyFinding:
    table: str
    column: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.table}.{self.column}: {self.rule}: {self.detail}"


def allowed_person_names(reference: dict[str, Any]) -> set[str]:
    """The full cross product of the closed name vocabulary.

    Generated with a numeric suffix too, because the generator appends one when
    the cross product is exhausted.
    """
    given = list(reference["given_names"])
    family = list(reference["family_names"])
    return {f"{g} {f}" for g in given for f in family}


def check_person_names(dataset: Dataset, reference: dict[str, Any]) -> list[PrivacyFinding]:
    allowed = allowed_person_names(reference)
    findings: list[PrivacyFinding] = []
    for table in SCHEMA:
        for column in table.columns:
            if column.classification != PERSON_NAME:
                continue
            for row in dataset.get(table.name, []):
                value = row.get(column.name)
                if value is None:
                    continue
                # Strip the disambiguating suffix the generator may append.
                base = re.sub(r" \d+$", "", str(value))
                if base not in allowed:
                    findings.append(
                        PrivacyFinding(table.name, column.name, "name-outside-vocabulary", repr(value))
                    )
    return findings


def check_org_names(dataset: Dataset, reference: dict[str, Any]) -> list[PrivacyFinding]:
    """Organisation names must be assembled from the seeded word lists.

    Suppliers are Microsoft's own reserved fictitious company names; customers
    are built from a prefix and suffix vocabulary. Either way the value has to be
    traceable to the seed rather than to the world.
    """
    prefixes = set(reference["customer_prefixes"])
    suffixes = set(reference["customer_suffixes"])
    seeded = {str(entry["name"]) for entry in reference["suppliers"]}
    seeded |= {str(entry["name"]) for entry in reference["locations"]}

    findings: list[PrivacyFinding] = []
    for table in SCHEMA:
        for column in table.columns:
            if column.classification != ORG_NAME:
                continue
            for row in dataset.get(table.name, []):
                value = str(row.get(column.name) or "")
                if value in seeded:
                    continue
                # Prefixes and suffixes are both multi-word, so the value cannot
                # be split on whitespace at a fixed position; match the ends
                # against the vocabulary instead.
                base = re.sub(r" \d+$", "", value)
                if any(
                    base == f"{prefix} {suffix}"
                    for prefix in prefixes
                    for suffix in suffixes
                ):
                    continue
                findings.append(
                    PrivacyFinding(table.name, column.name, "organisation-outside-vocabulary", repr(value))
                )
    return findings


def check_emails(dataset: Dataset) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []
    for table in SCHEMA:
        for column in table.columns:
            if column.classification != EMAIL:
                continue
            for row in dataset.get(table.name, []):
                value = str(row.get(column.name) or "")
                domain = value.rpartition("@")[2].lower()
                if domain not in SYNTHETIC_EMAIL_DOMAINS:
                    findings.append(
                        PrivacyFinding(table.name, column.name, "email-domain-not-reserved", repr(value))
                    )
    return findings


def check_phones(dataset: Dataset) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []
    for table in SCHEMA:
        for column in table.columns:
            if column.classification != PHONE:
                continue
            for row in dataset.get(table.name, []):
                value = str(row.get(column.name) or "")
                if not FICTIONAL_PHONE.fullmatch(value):
                    findings.append(
                        PrivacyFinding(table.name, column.name, "phone-outside-fiction-block", repr(value))
                    )
    return findings


def check_free_text(dataset: Dataset) -> list[PrivacyFinding]:
    """Reuse the public-site scanner on every free-text value.

    Sharing the engine is the point. If the docs gate would reject a string,
    the data gate must reject it too, otherwise a sanitised page could still
    quote a record that carries something it should not.
    """
    findings: list[PrivacyFinding] = []
    for table in SCHEMA:
        columns = [c.name for c in table.columns if c.classification == FREE_TEXT]
        if not columns:
            continue
        for row in dataset.get(table.name, []):
            for name in columns:
                value = row.get(name)
                if not value:
                    continue
                text = str(value)
                for finding in scan_text(text, path=Path(f"{table.name}.{name}")):
                    findings.append(
                        PrivacyFinding(table.name, name, f"scanner-{finding.rule}", finding.why)
                    )
                if US_SSN.search(text):
                    findings.append(PrivacyFinding(table.name, name, "ssn-shape", text[:60]))
                match = CARD_LIKE.search(text)
                if match and len(re.sub(r"\D", "", match.group(0))) >= 13:
                    findings.append(PrivacyFinding(table.name, name, "card-shape", match.group(0)[:40]))
    return findings


def presidio_available() -> bool:
    try:
        import presidio_analyzer  # noqa: F401
    except ImportError:
        return False
    return True


def check_presidio(dataset: Dataset) -> list[PrivacyFinding]:
    """Optional deep scan. Returns nothing at all when Presidio is absent.

    Callers distinguish "clean" from "not run" with :func:`presidio_available`;
    conflating them would let a missing dependency look like a passing gate.
    """
    if not presidio_available():
        return []

    from presidio_analyzer import AnalyzerEngine

    analyzer = AnalyzerEngine()
    findings: list[PrivacyFinding] = []
    for table in SCHEMA:
        columns = [c.name for c in table.columns if c.classification == FREE_TEXT]
        for row in dataset.get(table.name, []):
            for name in columns:
                value = row.get(name)
                if not value:
                    continue
                for result in analyzer.analyze(text=str(value), language="en"):
                    if result.entity_type in PRESIDIO_ALLOWED or result.score < 0.6:
                        continue
                    findings.append(
                        PrivacyFinding(table.name, name, f"presidio-{result.entity_type.lower()}",
                                       str(value)[result.start:result.end][:40])
                    )
    return findings


def check_all(dataset: Dataset, reference: dict[str, Any], *, use_presidio: bool = True) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []
    findings.extend(check_person_names(dataset, reference))
    findings.extend(check_org_names(dataset, reference))
    findings.extend(check_emails(dataset))
    findings.extend(check_phones(dataset))
    findings.extend(check_free_text(dataset))
    if use_presidio:
        findings.extend(check_presidio(dataset))
    return findings


__all__ = [
    "FICTIONAL_PHONE",
    "PrivacyFinding",
    "allowed_person_names",
    "check_all",
    "presidio_available",
]
