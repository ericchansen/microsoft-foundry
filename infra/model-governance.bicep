targetScope = 'resourceGroup'

@description('Stable prefix for resources owned by this resource group.')
param resourcePrefix string = 'contoso-agents'

var gatewayConfig = loadYamlContent('../config/gateway.yaml')

module governance 'modules/model-governance.bicep' = {
  name: 'model-governance'
  params: {
    foundryAccountName: '${resourcePrefix}-foundry'
    resourcePrefix: resourcePrefix
    allowedModelPublishers: gatewayConfig.model_governance.allowed_publishers
    allowedModelAssetIds: gatewayConfig.model_governance.allowed_asset_ids
    onlyAllowDirectFromAzure: gatewayConfig.model_governance.only_allow_direct_from_azure
    denyPreviewModels: gatewayConfig.model_governance.deny_preview_models
    tags: {
      project: 'contoso-agents'
      'managed-by': 'microsoft-foundry-demo'
      boundary: 'rg-contoso-agents'
      'cost-centre': 'demo'
    }
  }
}

output approvedModelsAssignmentName string = governance.outputs.approvedModelsAssignmentName
output modelEligibilityAssignmentName string = governance.outputs.modelEligibilityAssignmentName
output guardrailPolicyName string = governance.outputs.guardrailPolicyName
