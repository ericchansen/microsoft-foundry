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

@description('Deploy the disabled synthetic traffic job after an exact agent candidate passes.')
param deployTrafficJob bool = true

@description('Enable scheduled synthetic traffic after live acceptance succeeds.')
param trafficEnabled bool = false

@description('Run one acceptance conversation outside the normal schedule window.')
param trafficForceRun bool = false

@description('Container Apps UTC cron expression. Override only for bounded acceptance tests.')
param trafficCronExpression string = '0,30 * * * *'

@description('Deploy one immutable Travel tool backend and project connection.')
param deployToolService bool = true

@description('Authentication key shared only by the Travel tool service and its Foundry project connection.')
@secure()
@minLength(32)
param toolApiKey string = newGuid()

@description('Immutable backend release matching the prompt-agent definition major version.')
@minLength(2)
param toolRelease string = 'v2'

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
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-07-01' existing = {
  name: '${resourcePrefix}-foundry'
}

resource travelProject 'Microsoft.CognitiveServices/accounts/projects@2026-07-01' existing = {
  parent: foundryAccount
  name: 'travel'
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: take('${compactPrefix}${uniqueness}', 50)
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: '${resourcePrefix}-insights'
}

resource trafficIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: '${resourcePrefix}-travel-traffic'
  location: location
  tags: tags
}

resource toolIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = if (deployToolService) {
  name: '${resourcePrefix}-travel-tool-${toolRelease}'
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

resource toolRegistryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployToolService) {
  name: guid(registry.id, toolIdentity.id, acrPullRoleId)
  scope: registry
  properties: {
    principalId: toolIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource toolMonitoringPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployToolService) {
  name: guid(applicationInsights.id, toolIdentity.id, monitoringMetricsPublisherRoleId)
  scope: applicationInsights
  properties: {
    principalId: toolIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      monitoringMetricsPublisherRoleId
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

resource toolService 'Microsoft.App/containerApps@2025-07-01' = if (deployToolService) {
  name: '${resourcePrefix}-travel-tool-${toolRelease}'
  location: location
  tags: union(tags, {
    component: 'travel-openapi-tool'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${toolIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: true
        targetPort: 8080
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        transport: 'http'
      }
      maxInactiveRevisions: 1
      registries: [
        {
          server: registry.properties.loginServer
          identity: toolIdentity.id
        }
      ]
      secrets: [
        {
          name: 'travel-tool-api-key'
          value: toolApiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'travel-tool'
          image: '${imageRepository}@sha256:${imageDigest}'
          command: [
            'python'
            '-m'
            'contoso_travel_agent.service'
          ]
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
              value: toolIdentity.properties.clientId
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'contoso-travel-tool'
            }
            {
              name: 'AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING'
              value: 'true'
            }
            {
              name: 'TRAVEL_TOOL_API_KEY'
              secretRef: 'travel-tool-api-key'
            }
            {
              name: 'TRAVEL_TOOL_CREDENTIAL_REVISION'
              value: uniqueString(toolApiKey)
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 20
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '1'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    toolRegistryPull
    toolMonitoringPublisher
  ]
}

resource toolConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2026-05-01' = if (deployToolService) {
  parent: travelProject
  name: 'travel-openapi-${toolRelease}'
  properties: {
    authType: 'CustomKeys'
    category: 'CustomKeys'
    credentials: {
      keys: {
        'x-travel-tool-key': toolApiKey
      }
    }
    isSharedToAll: false
    metadata: {
      ApiType: 'OpenApi'
      description: 'Authenticated synthetic Travel tool service'
    }
    peRequirement: 'NotRequired'
    target: 'https://${toolService.properties.configuration.ingress.fqdn}'
    useWorkspaceManagedIdentity: false
  }
}

resource trafficJob 'Microsoft.App/jobs@2025-07-01' = if (deployTrafficJob) {
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
        cronExpression: trafficCronExpression
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
              name: 'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT'
              value: 'false'
            }
            {
              name: 'AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED'
              value: 'false'
            }
            {
              name: 'TRAFFIC_ENABLED'
              value: string(trafficEnabled)
            }
            {
              name: 'TRAFFIC_FORCE_RUN'
              value: string(trafficForceRun)
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
    foundryUser
  ]
}

output jobName string = deployTrafficJob ? trafficJob.name : ''
output environmentName string = environment.name
output toolConnectionName string = deployToolService ? toolConnection.name : ''
output toolServiceName string = deployToolService ? toolService.name : ''
output toolServiceUrl string = deployToolService ? 'https://${toolService.properties.configuration.ingress.fqdn}' : ''
