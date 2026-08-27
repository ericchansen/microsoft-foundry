"""Validated environment configuration for the Contoso Field runtime."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_RUNTIME_FILE_KEYS = frozenset(
    {
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "AZURE_OPENAI_ENDPOINT",
    }
)


def _as_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no", ""}:
        return False
    raise ValueError(f"expected a boolean value, got {value!r}")


def _runtime_file_values() -> dict[str, str]:
    configured_path = os.getenv("FIELD_RUNTIME_CONFIG_FILE")
    if not configured_path:
        return {}
    path = Path(configured_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read FIELD_RUNTIME_CONFIG_FILE {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("FIELD_RUNTIME_CONFIG_FILE must contain a JSON object")
    unsupported = set(payload) - _RUNTIME_FILE_KEYS
    if unsupported:
        raise ValueError(f"FIELD_RUNTIME_CONFIG_FILE contains unsupported keys: {sorted(unsupported)}")
    if not all(isinstance(value, str) for value in payload.values()):
        raise ValueError("FIELD_RUNTIME_CONFIG_FILE values must be strings")
    return payload


def _configured_value(name: str, default: str, file_values: dict[str, str]) -> str:
    return os.environ.get(name, file_values.get(name, default))


@dataclass(frozen=True)
class FieldSettings:
    """Configuration contains resource-relative names and synthetic fixture keys only."""

    agent_name: str
    otel_agent_id: str
    service_name: str
    model_deployment: str
    azure_openai_endpoint: str
    azure_openai_api_version: str
    azure_client_id: str | None
    application_insights_connection_string: str | None
    enrich_missing_agent_id: bool
    principal_oid: str
    principal_tid: str
    data_config: Path
    seed_dir: Path
    contracts_dir: Path
    data_dir: Path

    @classmethod
    def from_env(cls) -> FieldSettings:
        root = Path(__file__).resolve().parents[3]
        file_values = _runtime_file_values()
        agent_name = os.getenv("FIELD_AGENT_NAME", "contoso-field")
        return cls(
            agent_name=agent_name,
            otel_agent_id=os.getenv("FIELD_OTEL_AGENT_ID", f"{agent_name}-v1"),
            service_name=os.getenv("OTEL_SERVICE_NAME", "contoso-field"),
            model_deployment=os.getenv("FIELD_MODEL_DEPLOYMENT", "gpt-4.1-mini"),
            azure_openai_endpoint=_configured_value("AZURE_OPENAI_ENDPOINT", "", file_values),
            azure_openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
            azure_client_id=os.getenv("AZURE_CLIENT_ID") or None,
            application_insights_connection_string=(
                _configured_value("APPLICATIONINSIGHTS_CONNECTION_STRING", "", file_values) or None
            ),
            enrich_missing_agent_id=_as_bool(os.getenv("FIELD_ENRICH_AGENT_ID", "false")),
            principal_oid=os.getenv("FIELD_PRINCIPAL_OID", "OID-APAC-FIELDENG-01"),
            principal_tid=os.getenv("FIELD_PRINCIPAL_TID", "TID-CONTOSO-01"),
            data_config=Path(os.getenv("FIELD_DATA_CONFIG", root / "config" / "data-spine.yaml")),
            seed_dir=Path(os.getenv("FIELD_SEED_DIR", root / "data" / "seed")),
            contracts_dir=Path(os.getenv("FIELD_CONTRACTS_DIR", root / "config" / "toolbox")),
            data_dir=Path(os.getenv("FIELD_DATA_DIR", "/tmp/contoso-field-data")),
        )

    def validate_runtime(self) -> None:
        if not self.agent_name or not self.otel_agent_id:
            raise ValueError("FIELD_AGENT_NAME and FIELD_OTEL_AGENT_ID must be non-empty")
        if self.service_name != "contoso-field":
            raise ValueError("OTEL_SERVICE_NAME must be contoso-field")
        if not self.azure_openai_endpoint.startswith("https://"):
            raise ValueError("AZURE_OPENAI_ENDPOINT must be an HTTPS endpoint")
        for path in (self.data_config, self.seed_dir, self.contracts_dir):
            if not path.exists():
                raise ValueError(f"required runtime input does not exist: {path}")
