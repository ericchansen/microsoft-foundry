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

var gatewayConfig = loadYamlContent('../config/gateway.yaml')

module association 'modules/gateway-association.bicep' = {
  name: 'gateway-association'
  params: {
    gatewayName: '${resourcePrefix}-gateway'
    foundryAccountName: '${resourcePrefix}-foundry'
    projectNames: projectNames
    projectTokenLimits: gatewayConfig.projects
  }
}

output enrolledProjects array = association.outputs.enrolledProjects
