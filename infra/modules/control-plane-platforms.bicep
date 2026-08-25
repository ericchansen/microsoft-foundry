@description('Primary Azure region selected by the repository region evaluation.')
param location string

@description('Supported Azure SRE Agent region.')
param sreLocation string = 'eastus2'

@description('Stable prefix for resources owned by this resource group.')
param resourcePrefix string

@description('Tags applied to every resource that supports tags.')
param tags object

@description('Name of the shared workspace-based Application Insights component.')
param applicationInsightsName string

@description('Resource ID of the Log Analytics workspace backing Application Insights.')
param logAnalyticsWorkspaceResourceId string

@description('Microsoft Entra group object ID granted least-privilege data-plane access to the SRE Agent.')
@secure()
@minLength(36)
@maxLength(36)
param sreOperatorGroupObjectId string

var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
var logAnalyticsReaderRoleId = '73c42c96-874c-492b-b04d-ab87d138a893'
var sreAgentStandardUserRoleId = '2d84a65a-63b2-4343-bbb6-31105d857bc1'

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource sreIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: '${resourcePrefix}-sre'
  location: sreLocation
  tags: union(tags, {
    component: 'contoso-sre'
    platform: 'azure-sre-agent'
  })
}

resource approvalsIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: '${resourcePrefix}-approvals'
  location: location
  tags: union(tags, {
    component: 'contoso-approvals'
    platform: 'logic-apps-agent-loop'
  })
}

resource sreAgent 'Microsoft.App/agents@2026-01-01' = {
  name: '${resourcePrefix}-sre-control-plane'
  location: sreLocation
  tags: union(tags, {
    component: 'contoso-sre'
    'service-name': 'contoso-sre-control-plane'
    platform: 'azure-sre-agent'
  })
  identity: {
    type: 'SystemAssigned,UserAssigned'
    userAssignedIdentities: {
      '${sreIdentity.id}': {}
    }
  }
  properties: {
    actionConfiguration: {
      accessLevel: 'Low'
      identity: sreIdentity.id
      mode: 'Review'
    }
    defaultModel: {
      name: 'Automatic'
      provider: 'MicrosoftFoundry'
    }
    knowledgeGraphConfiguration: {
      identity: sreIdentity.id
      managedResources: [
        resourceGroup().id
      ]
    }
    logConfiguration: {
      applicationInsightsConfiguration: {
        appId: applicationInsights.properties.AppId
        connectionString: applicationInsights.properties.ConnectionString
      }
    }
    upgradeChannel: 'Stable'
  }
}

resource approvalsWorkflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: '${resourcePrefix}-approvals-loop'
  location: location
  tags: union(tags, {
    component: 'contoso-approvals'
    'service-name': 'contoso-approvals-loop'
    platform: 'logic-apps-agent-loop'
    status: 'public-preview'
  })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${approvalsIdentity.id}': {}
    }
  }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      metadata: {
        agentType: 'autonomous'
      }
      triggers: {
        Receive_synthetic_approval_scenario: {
          type: 'Request'
          kind: 'Http'
          inputs: {
            schema: {
              type: 'object'
              additionalProperties: false
              properties: {
                scenario: {
                  type: 'string'
                }
                synthetic: {
                  type: 'boolean'
                  enum: [
                    true
                  ]
                }
              }
              required: [
                'scenario'
                'synthetic'
              ]
            }
          }
          conditions: [
            {
              expression: '@equals(triggerBody()?[\'synthetic\'], true)'
            }
          ]
        }
      }
      actions: {
        Approval_triage_agent: {
          type: 'Agent'
          inputs: {
            parameters: {
              agentModelType: 'AzureOpenAI'
              modelId: 'gpt-4o-mini'
              messages: [
                {
                  role: 'System'
                  content: 'Classify only synthetic Contoso approval scenarios. Never execute a change. Use only the recommendation tool when a tool is needed, and always require a human decision.'
                }
                {
                  role: 'User'
                  content: '@triggerBody()?[\'scenario\']'
                }
              ]
              agentModelSettings: {
                maxTokens: 800
                agentHistoryReductionSettings: {
                  agentHistoryReductionType: 'maximumTokenCountReduction'
                  maximumTokenCount: 4096
                }
              }
            }
          }
          tools: {
            Create_approval_recommendation: {
              description: 'Return a synthetic recommendation for human review. This tool cannot mutate Azure or external systems.'
              agentParameterSchema: {
                type: 'object'
                properties: {
                  recommendation: {
                    type: 'string'
                    description: 'Recommend approve, reject, or investigate.'
                  }
                  rationale: {
                    type: 'string'
                    description: 'A short evidence-based explanation.'
                  }
                }
                required: [
                  'recommendation'
                  'rationale'
                ]
              }
              actions: {
                Build_recommendation: {
                  type: 'Compose'
                  inputs: {
                    recommendation: '@agentParameters(\'recommendation\')'
                    rationale: '@agentParameters(\'rationale\')'
                    requiresHumanApproval: true
                    synthetic: '@triggerBody()?[\'synthetic\']'
                  }
                }
              }
            }
          }
          runAfter: {}
          limit: {
            count: 5
            timeout: 'PT5M'
          }
        }
        Create_synthetic_review_envelope: {
          type: 'Compose'
          inputs: {
            scenario: '@triggerBody()?[\'scenario\']'
            disposition: 'Human review required'
            requiresHumanApproval: true
            synthetic: '@triggerBody()?[\'synthetic\']'
            agentLoopCompleted: true
          }
          runAfter: {
            Approval_triage_agent: [
              'Succeeded'
            ]
          }
        }
        Return_recommendation: {
          type: 'Response'
          kind: 'Http'
          inputs: {
            statusCode: 202
            body: {
              message: 'Synthetic agent loop completed; no change was executed.'
              result: '@outputs(\'Create_synthetic_review_envelope\')'
            }
          }
          runAfter: {
            Create_synthetic_review_envelope: [
              'Succeeded'
            ]
          }
        }
      }
      outputs: {}
    }
    parameters: {}
  }
}

#disable-next-line use-recent-api-versions
resource approvalsDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${resourcePrefix}-approvals-platform-logs'
  scope: approvalsWorkflow
  properties: {
    workspaceId: logAnalyticsWorkspaceResourceId
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource sreUserReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, sreIdentity.id, readerRoleId)
  properties: {
    principalId: sreIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
  }
}

resource sreUserMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, sreIdentity.id, monitoringReaderRoleId)
  properties: {
    principalId: sreIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
  }
}

resource sreUserLogReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(applicationInsights.id, sreIdentity.id, logAnalyticsReaderRoleId)
  scope: applicationInsights
  properties: {
    principalId: sreIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', logAnalyticsReaderRoleId)
  }
}

resource sreSystemReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, sreAgent.id, readerRoleId)
  properties: {
    principalId: sreAgent.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
  }
}

resource sreSystemMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, sreAgent.id, monitoringReaderRoleId)
  properties: {
    principalId: sreAgent.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
  }
}

resource sreSystemLogReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(applicationInsights.id, sreAgent.id, logAnalyticsReaderRoleId)
  scope: applicationInsights
  properties: {
    principalId: sreAgent.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', logAnalyticsReaderRoleId)
  }
}

resource sreOperatorStandardUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sreAgent.id, sreOperatorGroupObjectId, sreAgentStandardUserRoleId)
  scope: sreAgent
  properties: {
    principalId: sreOperatorGroupObjectId
    principalType: 'Group'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      sreAgentStandardUserRoleId
    )
  }
}

output sreAgentName string = sreAgent.name
output sreIdentityName string = sreIdentity.name
output approvalsWorkflowName string = approvalsWorkflow.name
output approvalsIdentityName string = approvalsIdentity.name
