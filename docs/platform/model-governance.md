# Model governance and guardrails

> **Takeaway:** Azure Policy blocks unapproved model deployments at the resource
> group boundary, while a separate responsible AI policy defines the content
> guardrails that each approved deployment must reference.

## Deployment allow-list

Two generally available Microsoft built-in policies are assigned only to the
project resource group:

1. **Foundry model deployments should only use approved models** allows exact
   model-family asset IDs from `config/gateway.yaml`.
2. **Foundry model deployments should meet eligibility requirements** permits
   only models distributed directly by Azure and denies preview lifecycle
   models.

The default list permits the Travel `gpt-5.4-mini` family, the low-cost
`gpt-4.1-mini` evaluation family, and three explicit later experiment families.
The publisher-wide list is intentionally empty: allowing the `OpenAI` publisher
would also allow arbitrary OpenAI families and defeat the narrow allow-list.
Every family asset ID ends in `/` so prefix matching cannot accidentally approve
similarly named models.

These GA built-ins govern deployment through the Foundry portal. Microsoft
documents that scope as the portal **Deploy** experience. A live, valid
unapproved-model probe through the ARM `accounts/deployments` API was not denied,
so this repository does not claim the built-ins protect direct ARM callers. The
probe deployment was removed immediately.

Closing that API-channel gap would require a custom policy definition at
subscription scope. The project boundary forbids creating or changing mutable
subscription-level policy definitions, so comprehensive ARM enforcement remains
an explicit limitation rather than a hidden manual claim.

## Content guardrail baseline

The custom `contoso-agents-guardrails` RAI policy uses the stable
`Microsoft.CognitiveServices/accounts/raiPolicies@2026-05-01` API and inherits
`Microsoft.DefaultV2`. It blocks medium-or-higher hate, sexual, violence, and
self-harm content in prompts and completions, plus jailbreak prompts and
protected text completions.

!!! warning "A policy definition is not enforcement"
    Creating an RAI policy does **not** attach it to a model deployment. Each
    approved deployment must set `properties.raiPolicyName` to
    `contoso-agents-guardrails`. The Travel deployment owns that attachment.
    Until live readback shows the deployment references this name, the guardrail
    is **defined but not claimed as enforced**.

Microsoft's abuse monitoring remains enabled by default for models sold by
Azure. It is a Microsoft-operated service control, not a property this template
can switch on. The repository does not request modified abuse monitoring and
does not claim to configure it.

Prompt Shields are represented by the `Jailbreak` prompt filter inherited from
and made explicit against `Microsoft.DefaultV2`. The APIM custom-agent policy can
also call Azure AI Content Safety in a future deployment, but doing so requires
a separately owned Content Safety backend; this branch does not create one.

## Limitations

- Azure Policy assignment propagation can take about 15 minutes.
- Compliance state can take up to 24 hours to appear even though deny
  enforcement is already active.
- Model cards remain visible; policy blocks deployment rather than hiding catalog
  entries.
- The built-ins do not provide a verified deny boundary for direct ARM model
  deployment in this subscription.
- The experimental `gpt-5.6-*` entries remain blocked if their lifecycle is
  preview, even though their families are explicitly listed.

## Sources

- [Built-in policies for Foundry model deployment](https://learn.microsoft.com/azure/foundry/how-to/model-deployment-policy)
- [Responsible AI policy Bicep resource](https://learn.microsoft.com/azure/templates/microsoft.cognitiveservices/2026-05-01/accounts/raipolicies)
- [Content filtering](https://learn.microsoft.com/azure/foundry/openai/concepts/content-filter)
- [Abuse monitoring](https://learn.microsoft.com/azure/foundry/openai/concepts/abuse-monitoring)
