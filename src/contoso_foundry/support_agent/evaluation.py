"""Deterministic, model-free security evaluation for Contoso Support."""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from contoso_foundry.support_agent.identity import PrincipalAllowlist, RequestIdentityBinding
from contoso_foundry.support_agent.tools import CanonicalDataStore, ScopedToolSessionFactory
from contoso_foundry.toolbox.identity import UnknownPrincipalError


class SupportEvaluationError(RuntimeError):
    """Raised when a scenario cannot prove its expected security outcome."""


@dataclass(frozen=True)
class _EvaluationContext:
    user_id: str | None
    call_id: str | None


@dataclass(frozen=True)
class ScenarioResult:
    id: str
    outcome: str


def _load_config(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != 1:
        raise SupportEvaluationError(f"{path} must contain support evaluation version 1")
    if not isinstance(document.get("principal_map"), dict) or not isinstance(document.get("scenarios"), list):
        raise SupportEvaluationError(f"{path} is missing principal_map or scenarios")
    return document


def _assert_expectation(scenario: dict[str, Any], result: Any) -> None:
    expected = scenario.get("expect")
    if not isinstance(expected, dict):
        raise SupportEvaluationError(f"scenario {scenario.get('id')!r} has no expectation")

    kind = expected.get("kind")
    if kind == "null":
        if result is not None:
            raise SupportEvaluationError(f"scenario {scenario['id']!r} disclosed a scoped row")
        return
    if kind == "field_equals":
        field = expected.get("field")
        if not isinstance(result, dict) or result.get(field) != expected.get("value"):
            raise SupportEvaluationError(f"scenario {scenario['id']!r} returned unexpected evidence")
        return
    if kind == "rows_field_equals":
        field = expected.get("field")
        value = expected.get("value")
        if not isinstance(result, list) or not result or any(
            not isinstance(row, dict) or row.get(field) != value for row in result
        ):
            raise SupportEvaluationError(f"scenario {scenario['id']!r} crossed its row-level scope")
        return
    raise SupportEvaluationError(f"scenario {scenario['id']!r} uses unsupported expectation {kind!r}")


def evaluate(
    *,
    database_path: Path,
    config_path: Path,
    contracts_dir: Path,
) -> list[ScenarioResult]:
    """Run every declared scenario against the real scoped Toolbox contracts.

    Expected authorization failures are explicit scenario outcomes. Missing data,
    malformed configuration, dependency failures, and assertion mismatches
    propagate as errors so CI cannot report a success-shaped evaluation.
    """

    document = _load_config(config_path)
    current_user: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "support_evaluation_user",
        default=None,
    )
    allowlist = PrincipalAllowlist.from_json(
        json.dumps(document["principal_map"]),
        tenant_key=str(document.get("tenant_key", "")),
    )
    binding = RequestIdentityBinding.from_allowlist(
        allowlist,
        lambda: _EvaluationContext(current_user.get(), call_id=f"eval-{current_user.get()}"),
    )
    sessions = ScopedToolSessionFactory(
        CanonicalDataStore(database_path=database_path),
        binding,
        contracts_dir=contracts_dir,
    )

    results: list[ScenarioResult] = []
    seen_ids: set[str] = set()
    for raw in document["scenarios"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise SupportEvaluationError("every support evaluation scenario needs a string id")
        scenario_id = raw["id"]
        if scenario_id in seen_ids:
            raise SupportEvaluationError(f"duplicate support evaluation scenario {scenario_id!r}")
        seen_ids.add(scenario_id)

        token = current_user.set(raw.get("principal"))
        try:
            expected_error = raw.get("expect_error")
            if expected_error:
                if expected_error != "UnknownPrincipalError":
                    raise SupportEvaluationError(
                        f"scenario {scenario_id!r} uses unsupported expected error {expected_error!r}"
                    )
                try:
                    sessions.call(str(raw.get("tool")), raw.get("arguments"))
                except UnknownPrincipalError:
                    results.append(ScenarioResult(id=scenario_id, outcome="expected authorization denial"))
                    continue
                raise SupportEvaluationError(f"scenario {scenario_id!r} unexpectedly resolved its principal")

            result = sessions.call(str(raw.get("tool")), raw.get("arguments"))
            _assert_expectation(raw, result)
            results.append(ScenarioResult(id=scenario_id, outcome="expected scoped result"))
        finally:
            current_user.reset(token)

    if not results:
        raise SupportEvaluationError("support evaluation declared no scenarios")
    return results
