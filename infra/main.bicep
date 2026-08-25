targetScope = 'resourceGroup'

@description('Azure region selected by the repository region evaluation.')
param location string = 'northcentralus'

@description('Stable prefix for resources owned by this resource group.')
@minLength(3)
@maxLength(24)
param resourcePrefix string = 'contoso-agents'

@description('Four isolated Foundry project names.')
param projectNames array = [
  'travel'
  'support'
  'research'
  'platform'
]

@description('GitHub owner/repository allowed to request the deployment identity.')
param githubRepository string = 'ericchansen/microsoft-foundry'

@description('Protected GitHub environment allowed to request the deployment identity.')
param githubEnvironment string = 'contoso-agents'

@description('Email recipients for the action group and mandatory budget alerts.')
@minLength(1)
param budgetContactEmails array

@description('Monthly Azure budget ceiling in USD.')
@minValue(1)
param monthlyBudgetUsd int = 500

@description('Budget period start. Kept dynamic so deployments do not expire.')
param budgetStartDate string = utcNow('yyyy-MM-01')

var logAnalyticsReaderRoleId = '73c42c96-874c-492b-b04d-ab87d138a893'
var privilegedMonitoringDataReaderRoleId = 'dbc9c667-e97f-4491-aee6-90b9cf960190'
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var acrPushRoleId = '8311e382-0749-4cb8-b61a-304f252e45ec'
var compactPrefix = replace(resourcePrefix, '-', '')
var uniqueness = take(uniqueString(resourceGroup().id), 8)
var containerRegistryName = take('${compactPrefix}${uniqueness}', 50)

@description('Tags applied to every resource that supports tags.')
param tags object = {
  project: 'contoso-agents'
  'managed-by': 'microsoft-foundry-demo'
  boundary: 'rg-contoso-agents'
  'cost-centre': 'demo'
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    resourcePrefix: resourcePrefix
    tags: tags
    retentionInDays: 90
    alertEmails: budgetContactEmails
  }
}

module secureData 'modules/secure-data.bicep' = {
  name: 'secure-data'
  params: {
    location: location
    resourcePrefix: resourcePrefix
    tags: tags
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${resourcePrefix}-insights'
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    DisableIpMasking: false
    DisableLocalAuth: false
    IngestionMode: 'LogAnalytics'
    RetentionInDays: 90
    WorkspaceResourceId: monitoring.outputs.logAnalyticsWorkspaceResourceId
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-07-01' = {
  name: '${resourcePrefix}-foundry'
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: '${resourcePrefix}-foundry'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource foundryProjects 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' = [for projectName in projectNames: {
  parent: foundryAccount
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'Contoso ${projectName} agent project'
    displayName: 'Contoso ${projectName}'
  }
}]

resource projectFoundryUsers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (projectName, index) in projectNames: {
  name: guid(foundryAccount.id, foundryProjects[index].id, foundryUserRoleId)
  scope: foundryAccount
  properties: {
    principalId: foundryProjects[index].identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
  }
}]

resource supportModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundryAccount
  name: 'support-gpt-5-4-mini'
  sku: {
    name: 'GlobalStandard'
    capacity: 10
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.4-mini'
      version: '2026-03-17'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource projectAppInsightsConnections 'Microsoft.CognitiveServices/accounts/projects/connections@2026-05-01' = [for (projectName, index) in projectNames: {
  parent: foundryProjects[index]
  name: 'appinsights-${projectName}'
  properties: {
    authType: 'ApiKey'
    category: 'AppInsights'
    credentials: {
      key: applicationInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: applicationInsights.id
    }
    target: applicationInsights.properties.ConnectionString
  }
}]

resource projectLogAnalyticsReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (projectName, index) in projectNames: {
  name: guid(applicationInsights.id, foundryProjects[index].id, logAnalyticsReaderRoleId)
  scope: applicationInsights
  properties: {
    principalId: foundryProjects[index].identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', logAnalyticsReaderRoleId)
  }
}]

resource projectPrivilegedMonitoringReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (projectName, index) in projectNames: {
  name: guid(applicationInsights.id, foundryProjects[index].id, privilegedMonitoringDataReaderRoleId)
  scope: applicationInsights
  properties: {
    principalId: foundryProjects[index].identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      privilegedMonitoringDataReaderRoleId
    )
  }
}]

module identities 'modules/identity.bicep' = {
  name: 'identities'
  params: {
    location: location
    resourcePrefix: resourcePrefix
    tags: tags
    githubRepository: githubRepository
    githubEnvironment: githubEnvironment
    foundryAccountName: foundryAccount.name
    foundryProjectName: 'support'
  }
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: containerRegistryName
}

resource deployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' existing = {
  name: '${resourcePrefix}-github-deploy'
}

resource deployAcrPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, deployIdentity.id, acrPushRoleId)
  scope: containerRegistry
  properties: {
    principalId: deployIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPushRoleId)
  }
  dependsOn: [
    identities
  ]
}

resource monthlyBudget 'Microsoft.Consumption/budgets@2024-08-01' = {
  name: '${resourcePrefix}-monthly'
  properties: {
    amount: monthlyBudgetUsd
    category: 'Cost'
    notifications: {
      actual50: {
        contactEmails: budgetContactEmails
        contactGroups: [
          monitoring.outputs.actionGroupResourceId
        ]
        contactRoles: []
        enabled: true
        locale: 'en-us'
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
      }
      actual80: {
        contactEmails: budgetContactEmails
        contactGroups: [
          monitoring.outputs.actionGroupResourceId
        ]
        contactRoles: []
        enabled: true
        locale: 'en-us'
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
      }
      forecast100: {
        contactEmails: budgetContactEmails
        contactGroups: [
          monitoring.outputs.actionGroupResourceId
        ]
        contactRoles: []
        enabled: true
        locale: 'en-us'
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
      }
    }
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
  }
}

output foundryAccountName string = foundryAccount.name
output projectNames array = projectNames
output logAnalyticsWorkspaceName string = monitoring.outputs.logAnalyticsWorkspaceName
output applicationInsightsName string = applicationInsights.name
output applicationInsightsResourceId string = applicationInsights.id
output keyVaultName string = secureData.outputs.keyVaultName
output storageAccountName string = secureData.outputs.storageAccountName
output containerRegistryName string = secureData.outputs.containerRegistryName
output containerRegistryEndpoint string = secureData.outputs.containerRegistryEndpoint
output containerRegistryResourceId string = secureData.outputs.containerRegistryResourceId
output deployIdentityName string = identities.outputs.deployIdentityName
output deployIdentityClientId string = identities.outputs.deployIdentityClientId
output runtimeIdentityName string = identities.outputs.runtimeIdentityName
output AZURE_AI_PROJECT_ENDPOINT string = 'https://${foundryAccount.name}.services.ai.azure.com/api/projects/support'
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = secureData.outputs.containerRegistryEndpoint
output AZURE_CONTAINER_REGISTRY_RESOURCE_ID string = secureData.outputs.containerRegistryResourceId
output MICROSOFT_FOUNDRY_MODEL_DEPLOYMENT_NAME string = supportModelDeployment.name