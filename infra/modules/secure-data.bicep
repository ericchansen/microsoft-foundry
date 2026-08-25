@description('Azure region for data services.')
param location string

@description('Stable prefix for resources owned by this resource group.')
param resourcePrefix string

@description('Tags applied to data services.')
param tags object

var compactPrefix = replace(resourcePrefix, '-', '')
var uniqueness = take(uniqueString(resourceGroup().id), 8)

resource registryVnet 'Microsoft.Network/virtualNetworks@2025-07-01' = {
  name: '${resourcePrefix}-registry-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.42.0.0/16'
      ]
    }
    subnets: [
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.42.1.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'build-runners'
        properties: {
          addressPrefix: '10.42.2.0/24'
        }
      }
    ]
  }
}

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
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    dataEndpointEnabled: false
    networkRuleBypassOptions: 'None'
    policies: {
      exportPolicy: {
        status: 'disabled'
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
    publicNetworkAccess: 'Disabled'
    zoneRedundancy: 'Disabled'
  }
}

resource registryPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.azurecr.io'
  location: 'global'
  tags: tags
}

resource registryPrivateDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: registryPrivateDnsZone
  name: '${resourcePrefix}-registry-link'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: registryVnet.id
    }
  }
}

resource registryPrivateEndpoint 'Microsoft.Network/privateEndpoints@2025-07-01' = {
  name: '${resourcePrefix}-registry-endpoint'
  location: location
  tags: tags
  properties: {
    customNetworkInterfaceName: '${resourcePrefix}-registry-endpoint-nic'
    privateLinkServiceConnections: [
      {
        name: 'registry'
        properties: {
          groupIds: [
            'registry'
          ]
          privateLinkServiceId: registry.id
        }
      }
    ]
    subnet: {
      id: registryVnet.properties.subnets[0].id
    }
  }
}

resource registryPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2025-07-01' = {
  parent: registryPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'registry'
        properties: {
          privateDnsZoneId: registryPrivateDnsZone.id
        }
      }
    ]
  }
}

output keyVaultName string = keyVault.name
output storageAccountName string = storage.name
output containerRegistryName string = registry.name
output containerRegistryEndpoint string = registry.properties.loginServer
output containerRegistryResourceId string = registry.id
output registryVnetName string = registryVnet.name
output registryBuildSubnetResourceId string = registryVnet.properties.subnets[1].id