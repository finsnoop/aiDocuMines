locals {
  name = "${var.prefix}-${var.environment}"
  # Globally unique, letters/digits only (storage, registry, key vault)
  flat = substr(replace(lower("${var.prefix}${var.environment}${random_string.suffix.result}"), "/[^a-z0-9]/", ""), 0, 22)
  tags = merge({ application = "aiDocuMines", environment = var.environment, managed_by = "terraform" }, var.tags)
}

resource "random_string" "suffix" {
  length  = 4
  upper   = false
  special = false
}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.name}"
  location = var.location
  tags     = local.tags
}

# ── Monitoring ───────────────────────────────────────────────────────────────
resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${local.name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 5 # cost guard; raise if logs are being dropped
  tags                = local.tags
}

resource "azurerm_application_insights" "main" {
  name                = "appi-${local.name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  tags                = local.tags
}

# ── Identity used by every container app (registry pull, Key Vault secrets) ──
resource "azurerm_user_assigned_identity" "apps" {
  name                = "id-${local.name}-apps"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

# ── Key Vault: every secret the platform uses ────────────────────────────────
resource "azurerm_key_vault" "main" {
  name                          = "kv-${local.flat}"
  location                      = azurerm_resource_group.main.location
  resource_group_name           = azurerm_resource_group.main.name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = true # reachable for Terraform/CI; apps use the private endpoint
  network_acls {
    default_action = length(var.allowed_admin_cidrs) > 0 ? "Deny" : "Allow"
    bypass         = "AzureServices"
    ip_rules       = var.allowed_admin_cidrs
  }
  tags = local.tags
}

resource "azurerm_role_assignment" "kv_admin_deployer" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "kv_reader_apps" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.apps.principal_id
}

resource "random_password" "generated" {
  for_each = toset(["django-secret-key", "postgres-admin-password", "litellm-master-key",
  "onlyoffice-jwt-secret", "onlyoffice-db-password", "elasticsearch-password"])
  length  = 64
  special = false # safe in URLs, .env files and shell
}

resource "azurerm_key_vault_secret" "generated" {
  for_each     = random_password.generated
  name         = each.key
  value        = each.value.result
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_admin_deployer]
}

# Supplied by people, never generated. The two encryption keys MUST be copied from the current
# server (FIELD_ENCRYPTION_KEY, ENCRYPTION_SECRET_KEY): new keys would make encrypted data unreadable.
# Terraform creates a placeholder once and never overwrites the value you set:
#   az keyvault secret set --vault-name <kv> --name field-encryption-key --value '<from the server .env>'
locals {
  supplied_secrets = ["field-encryption-key", "encryption-secret-key", "openai-api-key", "anthropic-api-key",
  "gemini-api-key", "openrouter-api-key", "email-host-password", "oidc-private-key"]
}

resource "azurerm_key_vault_secret" "supplied" {
  for_each     = toset(local.supplied_secrets)
  name         = each.key
  value        = "set-me"
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_admin_deployer]
  lifecycle {
    ignore_changes = [value, tags]
  }
}

# ── Container registry ───────────────────────────────────────────────────────
resource "azurerm_container_registry" "main" {
  name                = "acr${local.flat}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Standard"
  admin_enabled       = false # pulls use the managed identity
  tags                = local.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.apps.principal_id
}

# ── Microsoft Defender for Cloud (opt-in: subscription-wide) ─────────────────
resource "azurerm_security_center_subscription_pricing" "plans" {
  for_each      = var.enable_defender ? toset(["Containers", "OpenSourceRelationalDatabases", "StorageAccounts", "KeyVaults"]) : toset([])
  tier          = "Standard"
  resource_type = each.key
}
