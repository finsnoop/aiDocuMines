resource "azurerm_virtual_network" "main" {
  name                = "vnet-${local.name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = ["10.40.0.0/16"]
  tags                = local.tags
}

# Container Apps environment (workload profiles): delegated /23
resource "azurerm_subnet" "aca" {
  name                 = "snet-aca"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.40.0.0/23"]
  delegation {
    name = "aca"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

# PostgreSQL Flexible Server, private access (VNet integration)
resource "azurerm_subnet" "postgres" {
  name                 = "snet-postgres"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.40.2.0/24"]
  delegation {
    name = "postgres"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

# Private endpoints: Redis, Storage (Files), Key Vault
resource "azurerm_subnet" "endpoints" {
  name                 = "snet-endpoints"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.40.3.0/24"]
}

# Search VM (Elasticsearch + Milvus), no public IP
resource "azurerm_subnet" "search" {
  name                 = "snet-search"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.40.4.0/24"]
}

resource "azurerm_network_security_group" "search" {
  name                = "nsg-${local.name}-search"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  security_rule {
    name                       = "search-from-container-apps"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_address_prefix      = "10.40.0.0/23"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_ranges    = ["9200", "19530"]
  }
  security_rule {
    name                       = "deny-everything-else-from-vnet"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_address_prefix      = "VirtualNetwork"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "*"
  }
  tags = local.tags
}

resource "azurerm_subnet_network_security_group_association" "search" {
  subnet_id                 = azurerm_subnet.search.id
  network_security_group_id = azurerm_network_security_group.search.id
}

# ── Private DNS ──────────────────────────────────────────────────────────────
locals {
  private_zones = {
    postgres = "${local.name}.private.postgres.database.azure.com"
    redis    = "privatelink.redis.azure.net"
    file     = "privatelink.file.core.windows.net"
    vault    = "privatelink.vaultcore.azure.net"
  }
}

resource "azurerm_private_dns_zone" "zones" {
  for_each            = local.private_zones
  name                = each.value
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "zones" {
  for_each            = local.private_zones
  name                = "link-${each.key}"
  private_dns_zone_id = azurerm_private_dns_zone.zones[each.key].id
  virtual_network_id  = azurerm_virtual_network.main.id
}
