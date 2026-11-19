targetScope = 'resourceGroup'

param location string = resourceGroup().location
@allowed(['Standard_D2s_v4', 'Standard_D2ds_v4', 'Standard_D2s_v5', 'Standard_D4s_v4'])
param vmSize string = 'Standard_D2s_v4'
param adminUsername string = 'labadmin'
@secure()
param adminPassword string
param imageVersion string

var tags = {
  repo: 'freezer'
  project: 'freeze-lab'
  deployment: 'freezer-lab'
  environment: 'disposable-lab'
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'freezer-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [{
      name: 'DenyAllInbound'
      properties: {
        priority: 100
        protocol: '*'
        access: 'Deny'
        direction: 'Inbound'
        sourceAddressPrefix: '*'
        sourcePortRange: '*'
        destinationAddressPrefix: '*'
        destinationPortRange: '*'
      }
    }]
  }
}
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'freezer-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.89.0.0/24'] }
    subnets: [{
      name: 'lab'
      properties: {
        addressPrefix: '10.89.0.0/27'
        defaultOutboundAccess: false
        networkSecurityGroup: { id: nsg.id }
      }
    }]
  }
}
resource pip 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'freezer-egress'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  properties: { publicIPAllocationMethod: 'Static' }
}
resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: 'freezer-nic'
  location: location
  tags: tags
  properties: {
    enableAcceleratedNetworking: true
    ipConfigurations: [{
      name: 'primary'
      properties: {
        privateIPAllocationMethod: 'Dynamic'
        subnet: { id: vnet.properties.subnets[0].id }
        publicIPAddress: { id: pip.id }
      }
    }]
  }
}
resource vm 'Microsoft.Compute/virtualMachines@2025-11-01' = {
  name: 'freezer-host'
  location: location
  tags: tags
  properties: {
    hardwareProfile: { vmSize: vmSize }
    securityProfile: { securityType: 'Standard' }
    storageProfile: {
      imageReference: {
        publisher: 'MicrosoftWindowsServer'
        offer: 'WindowsServer'
        sku: '2022-datacenter-smalldisk-g2'
        version: imageVersion
      }
      osDisk: {
        name: 'freezer-os'
        createOption: 'FromImage'
        deleteOption: 'Delete'
        diskSizeGB: 32
        managedDisk: { storageAccountType: 'Standard_LRS' }
      }
    }
    osProfile: {
      computerName: 'freezer-host'
      adminUsername: adminUsername
      adminPassword: adminPassword
      windowsConfiguration: {
        provisionVMAgent: true
        enableAutomaticUpdates: false
        patchSettings: { patchMode: 'Manual' }
      }
    }
    networkProfile: { networkInterfaces: [{ id: nic.id }] }
    diagnosticsProfile: { bootDiagnostics: { enabled: false } }
  }
}
output vmName string = vm.name
