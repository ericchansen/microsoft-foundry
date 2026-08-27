targetScope = 'resourceGroup'

param location string
param resourcePrefix string = 'contoso-agents'

@description('Image repository in the project-owned registry, without a tag.')
param imageRepository string

@description('Exact sha256 image digest without the sha256 prefix.')
@minLength(64)
@maxLength(64)
param imageDigest string

@description('Exact immutable agent version selected after evaluation.')
@minLength(1)
param agentVersion string

param tags object = {
  project: 'contoso-agents'
  'managed-by': 'microsoft-foundry-demo'
  boundary: 'rg-contoso-agents'
  'cost-centre': 'demo'
}

var compactPrefix = replace(resourcePrefix, '-', '')
var uniqueness = take(uniqueString(resourceGroup().id), 8)
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: '${resourcePrefix}-foundry'
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: take('${compactPrefix}${uniqueness}', 50)
}

resource storage 'Microsoft.Storage/storageAccounts@2025-08-01' existing = {
  name: take('${compactPrefix}${uniqueness}', 24)
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-08-01' existing = {
  parent: storage
  name: 'default'
}

resource trafficLedger 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-08-01' = {
  parent: blobService
  name: 'synthetic-traffic-ledger'
  properties: {
    publicAccess: 'None'
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: '${resourcePrefix}-insights'
}

resource trafficIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: '${resourcePrefix}-travel-traffic'
  location: location
  tags: tags
}

resource foundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, trafficIdentity.id, foundryUserRoleId)
  scope: foundryAccount
  properties: {
    principalId: trafficIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
  }
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, trafficIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: trafficIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource ledgerWriter 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, trafficIdentity.id, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    principalId: trafficIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      storageBlobDataContributorRoleId
    )
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: '${resourcePrefix}-traffic-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
  }
}

resource trafficJob 'Microsoft.App/jobs@2025-07-01' = {
  name: '${resourcePrefix}-travel-traffic'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${trafficIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 600
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: '0,30 * * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: trafficIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'travel-traffic'
          image: '${imageRepository}@sha256:${imageDigest}'
          env: [
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: applicationInsights.properties.ConnectionString
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: trafficIdentity.properties.clientId
            }
            {
              name: 'FOUNDRY_PROJECT_ENDPOINT'
              value: 'https://${foundryAccount.name}.services.ai.azure.com/api/projects/travel'
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'contoso-travel'
            }
            {
              name: 'TRAFFIC_ENABLED'
              value: 'false'
            }
            {
              name: 'TRAFFIC_LEDGER_URL'
              value: '${storage.properties.primaryEndpoints.blob}${trafficLedger.name}'
            }
            {
              name: 'TRAFFIC_FORCE_RUN'
              value: 'false'
            }
            {
              name: 'TRAVEL_AGENT_VERSION'
              value: agentVersion
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    registryPull
    ledgerWriter
    foundryUser
  ]
}

output jobName string = trafficJob.name
output environmentName string = environment.name
