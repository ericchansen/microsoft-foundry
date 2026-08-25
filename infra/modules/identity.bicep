@description('Azure region for managed identities.')
param location string

@description('Stable prefix for resources owned by this resource group.')
param resourcePrefix string

@description('Tags applied to managed identities.')
param tags object

@description('GitHub owner/repository allowed to request the deployment identity.')
param githubRepository string

@description('Protected GitHub environment allowed to request the deployment identity.')
param githubEnvironment string

@description('Name of the Foundry account receiving the runtime role assignment.')
param foundryAccountName string

var contributorRoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'
var rbacAdministratorRoleId = 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource deployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: '${resourcePrefix}-github-deploy'
  location: location
  tags: tags
}

resource githubFederatedCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  parent: deployIdentity
  name: 'github-${githubEnvironment}'
  properties: {
    audiences: [
      'api://AzureADTokenExchange'
    ]
    issuer: 'https://token.actions.githubusercontent.com'
    subject: 'repo:${githubRepository}:environment:${githubEnvironment}'
  }
}

resource runtimeIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: '${resourcePrefix}-runtime'
  location: location
  tags: tags
}

resource deployContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, deployIdentity.id, contributorRoleId)
  properties: {
    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributorRoleId)
  }
}

resource deployRbacAdministrator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, deployIdentity.id, rbacAdministratorRoleId)
  properties: {
    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', rbacAdministratorRoleId)
  }
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: foundryAccountName
}

resource runtimeFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, runtimeIdentity.id, foundryUserRoleId)
  scope: foundryAccount
  properties: {
    principalId: runtimeIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
  }
}

output deployIdentityName string = deployIdentity.name
output deployIdentityClientId string = deployIdentity.properties.clientId
output runtimeIdentityName string = runtimeIdentity.name