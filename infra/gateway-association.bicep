targetScope = 'resourceGroup'

@description('Stable prefix for resources owned by this resource group.')
param resourcePrefix string = 'contoso-agents'

@description('Four existing Foundry projects to enroll.')
param projectNames array = [
  'travel'
  'support'
  'research'
  'platform'
]

@description('Existing model deployments supplied explicitly by the model-owning dependency stack.')
param existingModelDeployments array = []

var gatewayConfig = loadYamlContent('../config/gateway.yaml')

module association 'modules/gateway-association.bicep' = {
  name: 'gateway-association'
  params: {
    gatewayName: '${resourcePrefix}-gateway'
    foundryAccountName: '${resourcePrefix}-foundry'
    projectNames: projectNames
    projectTokenLimits: gatewayConfig.projects
    defaultProjectTokenLimits: gatewayConfig.default_project
    modelDeployments: existingModelDeployments
  }
}

output enrolledProjects array = association.outputs.enrolledProjects
output sharedDefaultConnectionName string = association.outputs.sharedDefaultConnectionName
