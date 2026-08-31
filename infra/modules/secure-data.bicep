@description('Azure region for data services.')
param location string

@description('Stable prefix for resources owned by this resource group.')
param resourcePrefix string

@description('Tags applied to data services.')
param tags object

@description('Project-owned deployment principal granted push access to the shared registry.')
param deployPrincipalId string

var compactPrefix = replace(resourcePrefix, '-', '')
var uniqueness = take(uniqueString(resourceGroup().id), 8)
var acrPushRoleId = '8311e382-0749-4cb8-b61a-304f252e45ec'

resource keyVault 'Microsoft.KeyVault/vaults@2025-05-01' = {
  name: take('${resourcePrefix}-kv-${uniqueness}', 24)
  location: location
  tags: tags
  properties: {
    enablePurgeProtection: true
    enableRbacAuthorization: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
    publicNetworkAccess: 'Disabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    softDeleteRetentionInDays: 90
    tenantId: subscription().tenantId
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2025-08-01' = {
  name: take('${compactPrefix}${uniqueness}', 24)
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    dnsEndpointType: 'Standard'
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-08-01' = {
  parent: storage
  name: 'default'
  properties: {
    containerDeleteRetentionPolicy: {
      days: 14
      enabled: true
    }
    deleteRetentionPolicy: {
      allowPermanentDelete: false
      days: 14
      enabled: true
    }
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: take('${compactPrefix}${uniqueness}', 50)
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    dataEndpointEnabled: false
    networkRuleBypassOptions: 'AzureServices'
    policies: {
      exportPolicy: {
        status: 'enabled'
      }
      quarantinePolicy: {
        status: 'disabled'
      }
      retentionPolicy: {
        days: 7
        status: 'disabled'
      }
      trustPolicy: {
        status: 'disabled'
        type: 'Notary'
      }
    }
    publicNetworkAccess: 'Enabled'
    zoneRedundancy: 'Disabled'
  }
}

resource deployAcrPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, 'github-deploy', acrPushRoleId)
  scope: registry
  properties: {
    principalId: deployPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPushRoleId)
  }
}

output keyVaultName string = keyVault.name
output storageAccountName string = storage.name
output containerRegistryName string = registry.name
output containerRegistryEndpoint string = registry.properties.loginServer
output containerRegistryResourceId string = registry.id