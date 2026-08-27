"""Runtime policy values shared by generated data and Toolbox callers."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml


class RuntimePolicyError(ValueError):
    """Raised when the data-spine runtime policy is incomplete or invalid."""


@dataclass(frozen=True)
class RuntimePolicy:
    as_of: dt.date
    minimum_cohort: int


def load_runtime_policy(path: Path) -> RuntimePolicy:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RuntimePolicyError(f"{path} must contain a mapping")

    privacy = document.get("privacy")
    if not isinstance(privacy, dict):
        raise RuntimePolicyError(f"{path} must declare privacy policy")

    minimum_cohort = privacy.get("minimum_cohort")
    if isinstance(minimum_cohort, bool) or not isinstance(minimum_cohort, int) or minimum_cohort < 1:
        raise RuntimePolicyError(f"{path} privacy.minimum_cohort must be a positive integer")

    try:
        as_of = dt.date.fromisoformat(str(document["as_of"]))
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimePolicyError(f"{path} as_of must be a valid ISO date") from error

    return RuntimePolicy(as_of=as_of, minimum_cohort=minimum_cohort)


__all__ = ["RuntimePolicy", "RuntimePolicyError", "load_runtime_policy"]
