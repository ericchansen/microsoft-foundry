@description('Existing API Management service name.')
param gatewayName string

@description('Existing project-owned Foundry account name.')
param foundryAccountName string

@description('Foundry project names explicitly enrolled in the gateway.')
param projectNames array

@description('Per-project tokens-per-minute and total quota settings.')
param projectTokenLimits object

var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'

resource gateway 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: gatewayName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2026-05-01' existing = {
  name: foundryAccountName
}

resource foundryProjects 'Microsoft.CognitiveServices/accounts/projects@2026-05-01' existing = [
  for projectName in projectNames: {
    name: projectName
    parent: foundryAccount
  }
]

resource gatewayFoundryAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryAccount.id, gateway.id, foundryUserRoleId)
  scope: foundryAccount
  properties: {
    principalId: gateway.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRoleId)
  }
}

resource foundryBackend 'Microsoft.ApiManagement/service/backends@2024-05-01' = {
  name: 'foundry-models'
  parent: gateway
  properties: {
    description: 'Project-owned Foundry model endpoint authenticated with the gateway managed identity.'
    protocol: 'http'
    url: 'https://${foundryAccountName}.services.ai.azure.com/openai/v1'
  }
}

resource projectTokenPolicies 'Microsoft.ApiManagement/service/policyFragments@2024-05-01' = [
  for projectName in projectNames: {
    name: '${projectName}-token-governance'
    parent: gateway
    properties: {
      description: 'Project-level TPM and total token quota for ${projectName}.'
      format: 'rawxml'
      value: '<fragment><llm-token-limit counter-key="project:${projectName}" tokens-per-minute="${projectTokenLimits[projectName].tokens_per_minute}" token-quota="${projectTokenLimits[projectName].total_token_quota}" token-quota-period="${projectTokenLimits[projectName].quota_period}" estimate-prompt-tokens="true" remaining-tokens-header-name="x-contoso-remaining-tpm" remaining-quota-tokens-header-name="x-contoso-remaining-quota" /></fragment>'
    }
  }
]

resource projectApis 'Microsoft.ApiManagement/service/apis@2024-05-01' = [
  for projectName in projectNames: {
    name: 'foundry-${projectName}'
    parent: gateway
    properties: {
      apiType: 'http'
      description: 'Governed Foundry model route for the ${projectName} project.'
      displayName: 'Foundry ${projectName}'
      path: 'models/${projectName}'
      protocols: [
        'https'
      ]
      serviceUrl: foundryBackend.properties.url
      subscriptionRequired: true
    }
  }
]

resource chatCompletionOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [
  for (projectName, index) in projectNames: {
    name: 'chat-completions'
    parent: projectApis[index]
    properties: {
      displayName: 'Chat completions'
      method: 'POST'
      urlTemplate: '/chat/completions'
      request: {
        headers: [
          {
            name: 'Content-Type'
            required: true
            type: 'string'
            values: [
              'application/json'
            ]
          }
        ]
      }
      responses: []
    }
  }
]

resource projectApiPolicies 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = [
  for (projectName, index) in projectNames: {
    name: 'policy'
    parent: projectApis[index]
    properties: {
      format: 'rawxml'
      value: '<policies><inbound><base /><include-fragment fragment-id="${projectName}-token-governance" /><set-backend-service backend-id="${foundryBackend.name}" /><authentication-managed-identity resource="https://cognitiveservices.azure.com" /></inbound><backend><forward-request /></backend><outbound><base /></outbound><on-error><base /></on-error></policies>'
    }
    dependsOn: [
      gatewayFoundryAccess
      projectTokenPolicies[index]
    ]
  }
]

resource projectSubscriptions 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = [
  for (projectName, index) in projectNames: {
    name: '${projectName}-gateway'
    parent: gateway
    properties: {
      allowTracing: false
      displayName: 'Foundry ${projectName} gateway'
      scope: projectApis[index].id
      state: 'active'
    }
  }
]

resource projectGatewayConnections 'Microsoft.CognitiveServices/accounts/projects/connections@2026-05-01' = [
  for (projectName, index) in projectNames: {
    name: 'ai-gateway-${projectName}'
    parent: foundryProjects[index]
    properties: {
      authType: 'ApiKey'
      category: 'ApiManagement'
      credentials: {
        key: projectSubscriptions[index].listSecrets().primaryKey
      }
      isSharedToAll: true
      metadata: {
        authHeaderFormat: '{api_key}'
        authHeaderName: 'Ocp-Apim-Subscription-Key'
        customHeaders: '{}'
        deploymentInPath: 'false'
        models: '[]'
      }
      target: '${gateway.properties.gatewayUrl}/${projectApis[index].properties.path}'
    }
  }
]

output enrolledProjects array = [
  for (projectName, index) in projectNames: {
    connectionName: projectGatewayConnections[index].name
    projectName: projectName
  }
]
