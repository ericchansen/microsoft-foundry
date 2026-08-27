@description('Existing Foundry account that owns the deployment.')
param accountName string

@description('Stable deployment name referenced by the prompt agent.')
param deploymentName string = 'travel-gpt-5-4-mini'

@description('Exact model name.')
param modelName string = 'gpt-5.4-mini'

@description('Exact model version. Latest aliases are forbidden.')
param modelVersion string = '2026-03-17'

@description('Global deployment SKU required by the selected region policy.')
param skuName string = 'GlobalStandard'

@description('Existing account-level responsible AI policy enforced by this deployment.')
param raiPolicyName string = 'contoso-agents-guardrails'

@minValue(1)
param capacity int = 10

resource account 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: accountName
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: deploymentName
  sku: {
    name: skuName
    capacity: capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    raiPolicyName: raiPolicyName
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

output deploymentName string = deployment.name
output modelName string = modelName
output modelVersion string = modelVersion
