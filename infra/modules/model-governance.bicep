@description('Name of the project-owned Foundry account.')
param foundryAccountName string

@description('Stable prefix for policy assignment and guardrail names.')
param resourcePrefix string

@description('Publisher-wide exceptions. Empty by default to avoid approving every model from a publisher.')
param allowedModelPublishers array = []

@description('Exact approved Foundry model asset families.')
@minLength(1)
param allowedModelAssetIds array

@description('Allow only models distributed directly by Azure.')
param onlyAllowDirectFromAzure bool = true

@description('Deny preview model deployments.')
param denyPreviewModels bool = true

@description('Tags applied to the custom responsible AI policy.')
param tags object

var approvedModelsPolicyDefinitionName = 'aafe3651-cb78-4f68-9f81-e7e41509110f'
var modelEligibilityPolicyDefinitionName = '8791d062-ba96-4c34-b604-8538f7e30ca0'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-05-01' existing = {
  name: foundryAccountName
}

resource approvedModels 'Microsoft.Authorization/policyAssignments@2025-03-01' = {
  name: '${resourcePrefix}-approved-models'
  properties: {
    displayName: 'Contoso Foundry approved models'
    description: 'Deny model deployments outside the explicit project allow-list.'
    enforcementMode: 'Default'
    policyDefinitionId: tenantResourceId(
      'Microsoft.Authorization/policyDefinitions',
      approvedModelsPolicyDefinitionName
    )
    parameters: {
      effect: {
        value: 'Deny'
      }
      allowedPublishers: {
        value: allowedModelPublishers
      }
      allowedAssetIds: {
        value: allowedModelAssetIds
      }
    }
    metadata: {
      category: 'Microsoft Foundry'
      managedBy: 'microsoft-foundry-demo'
    }
  }
}

resource modelEligibility 'Microsoft.Authorization/policyAssignments@2025-03-01' = {
  name: '${resourcePrefix}-model-eligibility'
  properties: {
    displayName: 'Contoso Foundry model eligibility'
    description: 'Deny preview models and models that are not distributed directly by Azure.'
    enforcementMode: 'Default'
    policyDefinitionId: tenantResourceId(
      'Microsoft.Authorization/policyDefinitions',
      modelEligibilityPolicyDefinitionName
    )
    parameters: {
      effect: {
        value: 'Deny'
      }
      onlyAllowDirectFromAzure: {
        value: onlyAllowDirectFromAzure
      }
      denyPreviewModels: {
        value: denyPreviewModels
      }
    }
    metadata: {
      category: 'Microsoft Foundry'
      managedBy: 'microsoft-foundry-demo'
    }
  }
}

resource guardrailBaseline 'Microsoft.CognitiveServices/accounts/raiPolicies@2026-05-01' = {
  name: '${resourcePrefix}-guardrails'
  parent: foundryAccount
  tags: tags
  properties: {
    basePolicyName: 'Microsoft.DefaultV2'
    mode: 'Blocking'
    contentFilters: [
      {
        name: 'Hate'
        source: 'Prompt'
        enabled: true
        blocking: true
        severityThreshold: 'Medium'
      }
      {
        name: 'Hate'
        source: 'Completion'
        enabled: true
        blocking: true
        severityThreshold: 'Medium'
      }
      {
        name: 'Sexual'
        source: 'Prompt'
        enabled: true
        blocking: true
        severityThreshold: 'Medium'
      }
      {
        name: 'Sexual'
        source: 'Completion'
        enabled: true
        blocking: true
        severityThreshold: 'Medium'
      }
      {
        name: 'Violence'
        source: 'Prompt'
        enabled: true
        blocking: true
        severityThreshold: 'Medium'
      }
      {
        name: 'Violence'
        source: 'Completion'
        enabled: true
        blocking: true
        severityThreshold: 'Medium'
      }
      {
        name: 'Selfharm'
        source: 'Prompt'
        enabled: true
        blocking: true
        severityThreshold: 'Medium'
      }
      {
        name: 'Selfharm'
        source: 'Completion'
        enabled: true
        blocking: true
        severityThreshold: 'Medium'
      }
      {
        name: 'Jailbreak'
        source: 'Prompt'
        enabled: true
        blocking: true
      }
      {
        name: 'Protected Material Text'
        source: 'Completion'
        enabled: true
        blocking: true
      }
    ]
  }
}

output approvedModelsAssignmentName string = approvedModels.name
output modelEligibilityAssignmentName string = modelEligibility.name
output guardrailPolicyName string = guardrailBaseline.name
