"""Static checks for Container Apps and deliberately undeployed AKS artifacts."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
AKS_DIR = REPO_ROOT / "agents" / "field" / "deploy" / "aks"
CONTAINER_APPS = REPO_ROOT / "agents" / "field" / "deploy" / "container-apps.bicep"
FIELD_DOCKERFILE = REPO_ROOT / "agents" / "field" / "Dockerfile"


def _documents() -> list[dict]:
    documents: list[dict] = []
    for path in sorted(AKS_DIR.glob("*.yaml")):
        documents.extend(document for document in yaml.safe_load_all(path.read_text(encoding="utf-8")) if document)
    return documents


def test_aks_artifacts_are_complete_and_internal() -> None:
    documents = _documents()
    kinds = {document["kind"] for document in documents}
    assert {"Deployment", "Service", "ServiceAccount", "SecretProviderClass"} <= kinds

    service = next(document for document in documents if document["kind"] == "Service")
    assert service["spec"]["type"] == "ClusterIP"

    deployment = next(document for document in documents if document["kind"] == "Deployment")
    pod = deployment["spec"]["template"]
    assert pod["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    assert pod["spec"]["serviceAccountName"] == "contoso-field"
    assert pod["spec"]["automountServiceAccountToken"] is False
    security = pod["spec"]["containers"][0]["securityContext"]
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"]["drop"] == ["ALL"]
    container = pod["spec"]["containers"][0]
    environment = {entry["name"]: entry["value"] for entry in container["env"]}
    assert environment["AZURE_OPENAI_ENDPOINT"] == "${AZURE_OPENAI_ENDPOINT}"
    assert environment["FIELD_RUNTIME_CONFIG_FILE"] == "/mnt/secrets-store/field-runtime-config"
    assert environment["FIELD_DATA_DIR"] == "/var/lib/contoso-field"
    mounts = {entry["name"]: entry["mountPath"] for entry in container["volumeMounts"]}
    assert mounts["field-data"] == "/var/lib/contoso-field"
    volumes = {entry["name"]: entry for entry in pod["spec"]["volumes"]}
    assert volumes["field-data"]["emptyDir"] == {}


def test_aks_readme_prohibits_deployment() -> None:
    readme = (AKS_DIR / "README.md").read_text(encoding="utf-8")
    assert "**They are not deployed by this project.**" in readme
    assert "no command in this repository" in readme


def test_container_app_has_no_public_ingress_or_static_credentials() -> None:
    source = CONTAINER_APPS.read_text(encoding="utf-8")
    assert "external: false" in source
    assert "minReplicas: 0" in source
    assert "type: 'UserAssigned'" in source
    assert "APPLICATIONINSIGHTS_AUTHENTICATION_STRING" in source
    assert "Authorization=AAD" in source
    assert "param image string" not in source
    assert "@allowed([\n  'contoso-field'\n])" in source
    assert "param imageRepository string" in source
    assert "param imageDigest string" in source
    assert "@sha256:${normalizedImageDigest}" in source
    assert "image: imageReference" in source
    assert "apiKey" not in source
    assert "password" not in source.lower()


def test_field_container_base_image_is_digest_pinned() -> None:
    assert "FROM python:3.13.15-slim-bookworm@sha256:" in FIELD_DOCKERFILE.read_text(
        encoding="utf-8"
    )
