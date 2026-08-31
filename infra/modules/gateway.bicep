@description('Azure region for the AI gateway.')
param location string

@description('Stable prefix for resources owned by this resource group.')
param resourcePrefix string

@description('Publisher email required by API Management.')
param publisherEmail string

@description('Resource ID of the project-owned Log Analytics workspace.')
param logAnalyticsWorkspaceResourceId string

@description('Tags applied to the gateway.')
param tags object

resource gateway 'Microsoft.ApiManagement/service@2024-05-01' = {
  name: '${resourcePrefix}-gateway'
  location: location
  tags: tags
  sku: {
    name: 'BasicV2'
    capacity: 1
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: 'Contoso Agents'
    notificationSenderEmail: 'apimgmt-noreply@mail.windowsazure.com'
    publicNetworkAccess: 'Enabled'
    virtualNetworkType: 'None'
    natGatewayState: 'Enabled'
    developerPortalStatus: 'Disabled'
    legacyPortalStatus: 'Disabled'
    customProperties: {
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Protocols.Server.Http2': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Ssl30': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls10': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls11': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls10': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls11': 'False'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Ciphers.TripleDes168': 'False'
    }
  }
}

// The latest diagnostic-settings API is still preview; it is required for resource-specific tables.
#disable-next-line use-recent-api-versions
resource gatewayDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: '${resourcePrefix}-gateway-logs'
  scope: gateway
  properties: {
    workspaceId: logAnalyticsWorkspaceResourceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'GatewayLogs'
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

output gatewayName string = gateway.name
output gatewayResourceId string = gateway.id
output gatewayPrincipalId string = gateway.identity.principalId
