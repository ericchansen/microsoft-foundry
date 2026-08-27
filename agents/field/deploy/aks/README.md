# AKS manifests: validation only

These manifests are production-shaped portability artifacts for an AKS cluster
with the Azure Key Vault Secrets Store CSI Driver, OIDC issuer and Microsoft
Entra Workload ID enabled. **They are not deployed by this project.** The live
Contoso Field runtime is Azure Container Apps, and no command in this repository
applies this directory to a cluster.

Before a future deployment, replace the deliberately invalid image and the
`${AZURE_OPENAI_ENDPOINT}`, `${FIELD_CLIENT_ID}`, `${KEY_VAULT_NAME}` and
`${TENANT_ID}` placeholders in a private overlay. Do not commit the resolved
values. The `field-runtime-config` Key Vault object must be a JSON object
containing only `APPLICATIONINSIGHTS_CONNECTION_STRING` and, optionally,
`AZURE_OPENAI_ENDPOINT`; environment values take precedence. The file is mounted
read-only and consumed through `FIELD_RUNTIME_CONFIG_FILE`. The `ClusterIP`
service has no public ingress, and the pod uses a dedicated service account with
Workload Identity rather than a Kubernetes credential.

The container root filesystem remains read-only. An `emptyDir` mounted at
`/var/lib/contoso-field` is the only writable location and is selected through
`FIELD_DATA_DIR`; it contains deterministic generated data and no credentials.
