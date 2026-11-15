# ── PostgreSQL Flexible Server (private, built-in PgBouncer) ─────────────────
resource "azurerm_postgresql_flexible_server" "main" {
  name                          = "psql-${local.flat}"
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  version                       = var.postgres.version
  sku_name                      = var.postgres.sku_name
  storage_mb                    = var.postgres.storage_mb
  storage_tier                  = "P10"
  auto_grow_enabled             = true
  backup_retention_days         = var.postgres.backup_retention_days
  geo_redundant_backup_enabled  = var.postgres.geo_redundant_backup
  delegated_subnet_id           = azurerm_subnet.postgres.id
  private_dns_zone_id           = azurerm_private_dns_zone.zones["postgres"].id
  public_network_access_enabled = false
  administrator_login           = "aidocadmin"
  administrator_password        = random_password.generated["postgres-admin-password"].result
  zone                          = "1"

  dynamic "high_availability" {
    for_each = var.postgres.high_availability ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = "2"
    }
  }

  maintenance_window {
    day_of_week  = 0 # Sunday
    start_hour   = 2
    start_minute = 0
  }
  tags       = local.tags
  depends_on = [azurerm_private_dns_zone_virtual_network_link.zones]

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [zone, high_availability[0].standby_availability_zone]
  }
}

resource "azurerm_postgresql_flexible_server_database" "app" {
  name      = "aidocumines"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_postgresql_flexible_server_database" "onlyoffice" {
  name      = "onlyoffice"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# Connection pooling: apps connect to port 6432; one web replica holds ~17 connections
# (2 workers x 8 threads), measured in the scale test.
resource "azurerm_postgresql_flexible_server_configuration" "pgbouncer" {
  for_each = {
    "pgbouncer.enabled"           = "true"
    "pgbouncer.default_pool_size" = "50"
    "pgbouncer.max_client_conn"   = "2000"
    "require_secure_transport"    = "on"
  }
  name      = each.key
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = each.value
}

# ── Azure Managed Redis (task queue + cache, TLS, private) ────────────────────
# One database per instance: queue and cache share database 0 (cache keys are prefixed "ds:").
resource "azurerm_managed_redis" "main" {
  name                      = "redis-${local.flat}"
  location                  = azurerm_resource_group.main.location
  resource_group_name       = azurerm_resource_group.main.name
  sku_name                  = var.redis_sku
  high_availability_enabled = true
  public_network_access     = "Disabled"
  default_database {
    access_keys_authentication_enabled = true # Celery/django-redis authenticate with a key
    client_protocol                    = "Encrypted"
    clustering_policy                  = "EnterpriseCluster" # one endpoint; clients need no cluster support
    eviction_policy                    = "NoEviction"        # never drop queued tasks silently
  }
  tags = local.tags
}

resource "azurerm_private_endpoint" "redis" {
  name                = "pe-${local.name}-redis"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.endpoints.id
  private_service_connection {
    name                           = "redis"
    private_connection_resource_id = azurerm_managed_redis.main.id
    subresource_names              = ["redisEnterprise"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "redis"
    private_dns_zone_ids = [azurerm_private_dns_zone.zones["redis"].id]
  }
  tags = local.tags
}

resource "azurerm_key_vault_secret" "redis_url" {
  name         = "redis-url"
  value        = "rediss://:${azurerm_managed_redis.main.default_database[0].primary_access_key}@${azurerm_managed_redis.main.hostname}:${azurerm_managed_redis.main.default_database[0].port}/0"
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_admin_deployer]
}

# ── Storage: documents (Azure Files, shared by every replica) and backups ────
resource "azurerm_storage_account" "main" {
  name                            = "st${local.flat}"
  location                        = azurerm_resource_group.main.location
  resource_group_name             = azurerm_resource_group.main.name
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = true # Container Apps mounts Azure Files with the account key
  https_traffic_only_enabled      = true
  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }
  share_properties {
    retention_policy {
      days = 30 # soft delete for file shares
    }
  }
  tags = local.tags
  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_storage_share" "shares" {
  for_each = {
    documents       = var.documents_share_quota_gb # /app/media in the API and workers
    onlyoffice-data = 50
    litellm-config  = 1
    ollama-models   = 100
  }
  name               = each.key
  storage_account_id = azurerm_storage_account.main.id
  quota              = each.value
}

resource "azurerm_storage_container" "backups" {
  name                  = "backups"
  storage_account_id    = azurerm_storage_account.main.id
  container_access_type = "private"
}

resource "azurerm_private_endpoint" "files" {
  name                = "pe-${local.name}-files"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.endpoints.id
  private_service_connection {
    name                           = "files"
    private_connection_resource_id = azurerm_storage_account.main.id
    subresource_names              = ["file"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "file"
    private_dns_zone_ids = [azurerm_private_dns_zone.zones["file"].id]
  }
  tags = local.tags
}

resource "azurerm_private_endpoint" "vault" {
  name                = "pe-${local.name}-vault"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.endpoints.id
  private_service_connection {
    name                           = "vault"
    private_connection_resource_id = azurerm_key_vault.main.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "vault"
    private_dns_zone_ids = [azurerm_private_dns_zone.zones["vault"].id]
  }
  tags = local.tags
}

# ── Backups beyond the built-in ones (PostgreSQL PITR, blob versioning) ──────
resource "azurerm_recovery_services_vault" "main" {
  name                = "rsv-${local.name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Standard"
  storage_mode_type   = "ZoneRedundant"
  # soft delete is always on in azurerm 5 (no argument)
  tags = local.tags
}

resource "azurerm_backup_container_storage_account" "main" {
  resource_group_name = azurerm_resource_group.main.name
  recovery_vault_name = azurerm_recovery_services_vault.main.name
  storage_account_id  = azurerm_storage_account.main.id
}

resource "azurerm_backup_policy_file_share" "daily" {
  name                = "daily-35d"
  resource_group_name = azurerm_resource_group.main.name
  recovery_vault_name = azurerm_recovery_services_vault.main.name
  timezone            = "South Africa Standard Time"
  backup {
    frequency = "Daily"
    time      = "01:00"
  }
  retention_daily {
    count = 35
  }
}

resource "azurerm_backup_protected_file_share" "documents" {
  resource_group_name       = azurerm_resource_group.main.name
  recovery_vault_name       = azurerm_recovery_services_vault.main.name
  source_storage_account_id = azurerm_backup_container_storage_account.main.storage_account_id
  source_file_share_name    = azurerm_storage_share.shares["documents"].name
  backup_policy_id          = azurerm_backup_policy_file_share.daily.id
}
