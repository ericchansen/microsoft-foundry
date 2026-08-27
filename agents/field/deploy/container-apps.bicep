targetScope = 'resourceGroup'

@description('Azure region selected by the repository region evaluation.')
param location string = resourceGroup().location

@description('Name of the existing project-owned container registry.')
param containerRegistryName string

@description('Repository path inside the existing project-owned container registry. Tags and registries are rejected.')
@allowed([
  'contoso-field'
])
param imageRepository string = 'contoso-field'

@description('Lowercase 64-character sha256 image digest without the sha256 prefix.')
@minLength(64)
@maxLength(64)
param imageDigest string

@description('Name of the existing project-owned Foundry account.')
param foundryAccountName string = 'contoso-agents-foundry'

@description('Name of the existing project-owned Application Insights component.')
param applicationInsightsName string = 'contoso-agents-insights'

@description('Name of the existing project-owned Log Analytics workspace.')
param logAnalyticsWorkspaceName string = 'contoso-agents-logs'

@description('Stable external-agent name.')
param agentName string = 'contoso-field'

@description('Stable OpenTelemetry ID matched by Foundry external-agent registration.')
param otelAgentId string = 'contoso-field-v1'

@description('Azure OpenAI deployment used by Pydantic AI.')
param modelDeploymentName string = 'contoso-field-model'

@description('Model name served by the deployment.')
param modelName string = 'gpt-4.1-mini'

@description('Pinned model version verified against the live account model catalogue.')
param modelVersion string = '2025-04-14'

@description('Install project glue that adds gen_ai.agent.id only when Pydantic AI omits it.')
param enrichMissingAgentId bool = false

@description('Tags applied to every mutable field-agent resource.')
param tags object = {
  project: 'contoso-agents'
  'managed-by': 'microsoft-foundry-demo'
  boundary: 'rg-contoso-agents'
  'cost-centre': 'demo'
  component: 'contoso-field'
}

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'
var normalizedImageDigest = toLower(imageDigest)

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: containerRegistryName
}

var imageReference = '${registry.properties.loginServer}/${imageRepository}@sha256:${normalizedImageDigest}'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: foundryAccountName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource fieldIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: '${agentName}-runtime'
  location: location
  tags: tags
}

resource fieldModel 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: foundryAccount
  name: modelDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: 10
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: modelVersion
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource fieldEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: '${agentName}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    publicNetworkAccess: 'Enabled'
  }
}

resource fieldApp 'Microsoft.App/containerApps@2025-07-01' = {
  name: agentName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${fieldIdentity.id}': {}
    }
  }
  properties: {
    environmentId: fieldEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: false
        targetPort: 8000
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        transport: 'http'
      }
      registries: [
        {
          identity: fieldIdentity.id
          server: registry.properties.loginServer
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'field'
          image: imageReference
          env: [
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: applicationInsights.properties.ConnectionString
            }
            {
              name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING'
              value: 'Authorization=AAD'
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: fieldIdentity.properties.clientId
            }
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: foundryAccount.properties.endpoints['OpenAI Language Model Instance API']
            }
            {
              name: 'FIELD_AGENT_NAME'
              value: agentName
            }
            {
              name: 'FIELD_ENRICH_AGENT_ID'
              value: string(enrichMissingAgentId)
            }
            {
              name: 'FIELD_MODEL_DEPLOYMENT'
              value: fieldModel.name
            }
            {
              name: 'FIELD_OTEL_AGENT_ID'
              value: otelAgentId
            }
            {
              name: 'FIELD_PRINCIPAL_OID'
              value: 'OID-APAC-FIELDENG-01'
            }
            {
              name: 'FIELD_PRINCIPAL_TID'
              value: 'TID-CONTOSO-01'
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'contoso-field'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 20
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        maxReplicas: 1
        minReplicas: 0
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    fieldAcrPull
    fieldFoundryUser
    fieldMonitoringPublisher
  ]
}

resource fieldAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, fieldIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: fieldIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource fieldFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, fieldIdentity.id, foundryUserRoleId)
  scope: foundryAccount
  properties: {
    principalId: fieldIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
  }
}

resource fieldMonitoringPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(applicationInsights.id, fieldIdentity.id, monitoringMetricsPublisherRoleId)
  scope: applicationInsights
  properties: {
    principalId: fieldIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      monitoringMetricsPublisherRoleId
    )
  }
}

output containerAppName string = fieldApp.name
output containerAppFqdn string = fieldApp.properties.configuration.ingress.fqdn
output environmentName string = fieldEnvironment.name
output fieldIdentityName string = fieldIdentity.name
output modelDeploymentName string = fieldModel.name
