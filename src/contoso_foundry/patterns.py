"""Regex patterns for identifiers that must never appear in public artifacts.

Both the sanitizer (which redacts before writing) and the scanner (which fails a
build when redaction was missed) import from here, so the two can never drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

#: GUIDs that are documentation placeholders rather than real identifiers.
PLACEHOLDER_GUIDS = frozenset(
    {
        "00000000-0000-0000-0000-000000000000",
        "11111111-1111-1111-1111-111111111111",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    }
)


@dataclass(frozen=True)
class Rule:
    """A named detection rule.

    ``why`` is surfaced verbatim in scanner output so a failing build explains
    itself without anyone needing to read this file.
    """

    name: str
    pattern: re.Pattern[str]
    why: str


#: Tenant-specific service endpoints. These are deliberately *not* the public
#: Microsoft portals (portal.azure.com, make.powerapps.com, ...), which are
#: allowed because the published site is expected to deep-link to them.
LIVE_ENDPOINT_RULES: tuple[Rule, ...] = (
    Rule(
        "azure-openai-endpoint",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.openai\.azure\.com\b", re.I),
        "a tenant-specific Azure OpenAI endpoint",
    ),
    Rule(
        "cognitive-services-endpoint",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.cognitiveservices\.azure\.com\b", re.I),
        "a tenant-specific Cognitive Services endpoint",
    ),
    Rule(
        "foundry-endpoint",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.services\.ai\.azure\.com\b", re.I),
        "a tenant-specific Azure AI Foundry endpoint",
    ),
    Rule(
        "apim-gateway",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.azure-api\.net\b", re.I),
        "a live API Management gateway hostname",
    ),
    Rule(
        "container-app",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.azurecontainerapps\.io\b", re.I),
        "a live Container Apps ingress hostname",
    ),
    Rule(
        "app-service",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.azurewebsites\.net\b", re.I),
        "a live App Service hostname",
    ),
    Rule(
        "storage-endpoint",
        re.compile(r"\b[a-z0-9]{3,24}\.(blob|queue|table|file|dfs)\.core\.windows\.net\b", re.I),
        "a live Azure Storage endpoint",
    ),
    Rule(
        "key-vault",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.vault\.azure\.net\b", re.I),
        "a live Key Vault endpoint",
    ),
    Rule(
        "dataverse",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.crm\d*\.dynamics\.com\b", re.I),
        "a tenant-specific Dataverse / Dynamics environment URL",
    ),
    Rule(
        "sharepoint",
        re.compile(r"\b[a-z0-9][a-z0-9-]{1,62}\.sharepoint\.com\b", re.I),
        "a tenant-specific SharePoint hostname",
    ),
)

SECRET_RULES: tuple[Rule, ...] = (
    Rule(
        "private-key",
        re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
        "a PEM private key block",
    ),
    Rule(
        "storage-connection-string",
        re.compile(r"DefaultEndpointsProtocol=|AccountKey=|SharedAccessSignature=", re.I),
        "an Azure Storage connection string",
    ),
    Rule(
        "servicebus-connection-string",
        re.compile(r"SharedAccessKeyName\s*=|SharedAccessKey\s*=", re.I),
        "a Service Bus / Event Hubs connection string",
    ),
    Rule(
        "sql-connection-string",
        re.compile(r"Server\s*=\s*tcp:|Password\s*=\s*[^\s;\"']{6,}", re.I),
        "a SQL connection string",
    ),
    Rule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "a JSON Web Token",
    ),
    Rule(
        "openai-api-key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "an OpenAI-style API key",
    ),
    Rule(
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
        "a GitHub token",
    ),
    Rule(
        "assigned-secret",
        # KEY= / SECRET= / PASSWORD= / TOKEN= followed by something that is not an
        # obvious placeholder. Placeholders are all-caps, angle-bracketed, or dotted.
        # Case-insensitive: `client_secret=` is at least as common as `CLIENT_SECRET=`.
        re.compile(
            r"\b(?:[A-Za-z0-9_]*(?:KEY|SECRET|PASSWORD|TOKEN))\s*[=:]\s*"
            r"(?!\s|$|['\"]?(?:<|\{|\$|xxx|\.\.\.|your|redacted|changeme|placeholder))"
            r"['\"]?[A-Za-z0-9+/_-]{16,}",
            re.I,
        ),
        "a secret assigned to a KEY/SECRET/PASSWORD/TOKEN variable",
    ),
)

IDENTIFIER_RULES: tuple[Rule, ...] = (
    Rule(
        "entra-default-domain",
        re.compile(r"\b[A-Za-z0-9-]+\.onmicrosoft\.com\b", re.I),
        "an Entra ID default tenant domain",
    ),
    Rule(
        "azure-resource-id",
        # Match the whole path, not just the subscription segment: redacting only
        # `/subscriptions/<guid>` would leave the resource group and resource name
        # in the clear, which is most of what an attacker wanted anyway.
        re.compile(r"/subscriptions/[^\s\"'<>)\]]+", re.I),
        "an Azure resource ID or subscription-scoped path",
    ),
    Rule(
        "mcap-subscription-name",
        re.compile(r"\bME-MngEnvMCAP\w*\b", re.I),
        "an MCAP subscription name, which encodes the tenant",
    ),
)

#: Domains considered synthetic by RFC 2606 / Microsoft sample convention.
SYNTHETIC_EMAIL_DOMAINS = frozenset(
    {
        "contoso.com",
        "fabrikam.com",
        "adventure-works.com",
        "example.com",
        "example.org",
        "example.net",
        "users.noreply.github.com",
    }
)

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

#: North-American style phone numbers. Numbers in the 555-01xx range are reserved
#: for fiction and are therefore allowed.
PHONE = re.compile(r"\b(?:\+1[ .-]?)?\(?(\d{3})\)?[ .-]?(\d{3})[ .-]?(\d{4})\b")
FICTIONAL_PHONE_PREFIX = "5550"

US_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
