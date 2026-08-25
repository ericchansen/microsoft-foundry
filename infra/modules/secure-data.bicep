@description('Azure region for data services.')
param location string

@description('Stable prefix for resources owned by this resource group.')
param resourcePrefix string

@description('Tags applied to data services.')
param tags object

var compactPrefix = replace(resourcePrefix, '-', '')
var keyVaultPrefixCandidate = take(resourcePrefix, 14)
var keyVaultPrefix = endsWith(keyVaultPrefixCandidate, '-')
  ? take(keyVaultPrefixCandidate, 13)
  : keyVaultPrefixCandidate
var storagePrefix = take(compactPrefix, 16)
var uniqueness = take(uniqueString(resourceGroup().id), 8)
var keyVaultUniqueness = take(uniqueness, 6)

resource keyVault 'Microsoft.KeyVault/vaults@2025-05-01' = {
  name: '${keyVaultPrefix}-kv-${keyVaultUniqueness}'
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
      defaultAction: 'Allow'
    }
    publicNetworkAccess: 'Enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    softDeleteRetentionInDays: 90
    tenantId: subscription().tenantId
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2025-08-01' = {
  name: '${storagePrefix}${uniqueness}'
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
    publicNetworkAccess: 'Enabled'
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

output keyVaultName string = keyVault.name
output storageAccountName string = storage.name
output containerRegistryName string = registry.name