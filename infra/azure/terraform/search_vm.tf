# Elasticsearch and Milvus need fast local disks (fsync-heavy). Container Apps only offers network
# file shares, which Elasticsearch does not support, so both run on one private VM with a managed
# Premium SSD. No public IP; administration through `az vm run-command`. Backed up daily.

resource "azurerm_network_interface" "search" {
  name                = "nic-${local.name}-search"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.search.id
    private_ip_address_allocation = "Static"
    private_ip_address            = "10.40.4.10"
  }
  tags = local.tags
}

resource "azurerm_managed_disk" "search_data" {
  name                 = "disk-${local.name}-search-data"
  location             = azurerm_resource_group.main.location
  resource_group_name  = azurerm_resource_group.main.name
  storage_account_type = "Premium_ZRS"
  create_option        = "Empty"
  disk_size_gb         = var.search_disk_gb
  tags                 = local.tags
  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_linux_virtual_machine" "search" {
  name                            = "vm-${local.name}-search"
  location                        = azurerm_resource_group.main.location
  resource_group_name             = azurerm_resource_group.main.name
  size                            = var.search_vm_size
  admin_username                  = "aidocops"
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.search.id]
  encryption_at_host_enabled      = false # enable once the subscription feature is registered
  admin_ssh_key {
    username   = "aidocops"
    public_key = tls_private_key.search.public_key_openssh
  }
  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Premium_LRS"
    disk_size_gb         = 64
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }
  identity {
    type = "SystemAssigned"
  }
  custom_data = base64encode(templatefile("${path.module}/cloud-init-search.yaml", {
    elastic_password = random_password.generated["elasticsearch-password"].result
  }))
  tags = local.tags
  lifecycle {
    ignore_changes = [custom_data, source_image_reference[0].version] # cloud-init runs once
  }
}

resource "tls_private_key" "search" {
  algorithm = "ED25519"
}

resource "azurerm_key_vault_secret" "search_ssh" {
  name         = "search-vm-ssh-private-key"
  value        = tls_private_key.search.private_key_openssh
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_admin_deployer]
}

resource "azurerm_virtual_machine_data_disk_attachment" "search" {
  managed_disk_id    = azurerm_managed_disk.search_data.id
  virtual_machine_id = azurerm_linux_virtual_machine.search.id
  lun                = 0
  caching            = "ReadOnly"
}

resource "azurerm_backup_policy_vm" "daily" {
  name                = "vm-daily-30d"
  resource_group_name = azurerm_resource_group.main.name
  recovery_vault_name = azurerm_recovery_services_vault.main.name
  timezone            = "South Africa Standard Time"
  backup {
    frequency = "Daily"
    time      = "01:30"
  }
  retention_daily {
    count = 30
  }
}

resource "azurerm_backup_protected_vm" "search" {
  resource_group_name = azurerm_resource_group.main.name
  recovery_vault_name = azurerm_recovery_services_vault.main.name
  source_vm_id        = azurerm_linux_virtual_machine.search.id
  backup_policy_id    = azurerm_backup_policy_vm.daily.id
  depends_on          = [azurerm_virtual_machine_data_disk_attachment.search]
}
