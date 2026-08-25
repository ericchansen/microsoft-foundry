@description('Azure region for the telemetry resources.')
param location string

@description('Stable prefix for resources owned by this resource group.')
param resourcePrefix string

@description('Tags applied to telemetry resources.')
param tags object

@description('Log Analytics data retention period.')
@minValue(90)
param retentionInDays int = 90

@description('Required email recipients for operational and cost alerts.')
@minLength(1)
param alertEmails array

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: '${resourcePrefix}-logs'
  location: location
  tags: tags
  properties: {
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    retentionInDays: retentionInDays
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${resourcePrefix}-alerts'
  location: 'global'
  tags: tags
  properties: {
    emailReceivers: [for (email, index) in alertEmails: {
      emailAddress: email
      name: 'budget-recipient-${index + 1}'
      useCommonAlertSchema: true
    }]
    enabled: true
    groupShortName: 'ContosoOps'
  }
}
output logAnalyticsWorkspaceName string = logAnalytics.name
output logAnalyticsWorkspaceResourceId string = logAnalytics.id
output actionGroupResourceId string = actionGroup.id