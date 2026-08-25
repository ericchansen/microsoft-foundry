targetScope = 'resourceGroup'

@description('Existing project-owned Foundry account.')
param foundryAccountName string

@description('A deliberately unapproved synthetic model name used only with what-if.')
param modelName string = 'unapproved-policy-probe'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-05-01' existing = {
  name: foundryAccountName
}

resource deniedModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  name: 'policy-deny-probe'
  parent: foundryAccount
  sku: {
    name: 'GlobalStandard'
    capacity: 1
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: '1'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}
