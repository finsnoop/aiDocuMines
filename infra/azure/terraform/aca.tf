# ── Container Apps environment: private (internal load balancer), reached through Front Door ─
resource "azurerm_container_app_environment" "main" {
  name                               = "cae-${local.name}"
  location                           = azurerm_resource_group.main.location
  resource_group_name                = azurerm_resource_group.main.name
  log_analytics_workspace_id         = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id           = azurerm_subnet.aca.id
  internal_load_balancer_enabled     = true
  zone_redundancy_enabled            = true
  infrastructure_resource_group_name = "rg-${local.name}-aca-infra"

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
  workload_profile {
    name                  = "heavy"
    workload_profile_type = var.dedicated_profile.type
    minimum_count         = var.dedicated_profile.min
    maximum_count         = var.dedicated_profile.max
  }
  tags = local.tags
}

resource "azurerm_container_app_environment_storage" "shares" {
  for_each                     = { documents = "documents", onlyoffice = "onlyoffice-data", litellm = "litellm-config", ollama = "ollama-models", playbooks = "playbooks" }
  name                         = each.key
  container_app_environment_id = azurerm_container_app_environment.main.id
  account_name                 = azurerm_storage_account.main.name
  share_name                   = each.key == "playbooks" ? azurerm_storage_share.playbooks.name : azurerm_storage_share.shares[each.value].name
  access_key                   = azurerm_storage_account.main.primary_access_key
  access_mode                  = "ReadWrite"
}

resource "azurerm_storage_share" "playbooks" {
  name               = "playbooks"
  storage_account_id = azurerm_storage_account.main.id
  quota              = 10
}

resource "azurerm_postgresql_flexible_server_database" "nodeauth" {
  name      = "nodeauth"
  server_id = azurerm_postgresql_flexible_server.main.id
  charset   = "UTF8"
  collation = "en_US.utf8"
  lifecycle {
    prevent_destroy = true
  }
}

locals {
  registry  = azurerm_container_registry.main.login_server
  app_image = "${local.registry}/aidocumines-app:${var.image_tag}"
  kv        = azurerm_key_vault.main.vault_uri

  # Key Vault secrets every API container reads (Container Apps resolves them with the managed identity)
  api_secrets = {
    "django-secret-key"      = "django-secret-key"
    "postgres-password"      = "postgres-admin-password"
    "redis-url"              = "redis-url"
    "field-encryption-key"   = "field-encryption-key"
    "encryption-secret-key"  = "encryption-secret-key"
    "openai-api-key"         = "openai-api-key"
    "litellm-master-key"     = "litellm-master-key"
    "onlyoffice-jwt-secret"  = "onlyoffice-jwt-secret"
    "elasticsearch-password" = "elasticsearch-password"
    "email-host-password"    = "email-host-password"
    "oidc-private-key"       = "oidc-private-key"
  }

  api_env_plain = {
    DEBUG                                 = "False"
    ALLOWED_HOSTS                         = "${var.hostnames.api},.${azurerm_container_app_environment.main.default_domain},localhost"
    FRONTEND_URL                          = "https://${var.hostnames.app}"
    SPA_REDIRECT_URIS                     = "https://${var.hostnames.app}/sign_in"
    CORS_EXTRA_ORIGINS                    = "https://${var.hostnames.app}"
    API_BASE_URL                          = "https://${var.hostnames.api}"
    NUM_PROXIES                           = "2" # Front Door + Container Apps ingress both append X-Forwarded-For
    POSTGRES_HOST                         = azurerm_postgresql_flexible_server.main.fqdn
    POSTGRES_PORT                         = "6432" # built-in PgBouncer
    POSTGRES_DB                           = azurerm_postgresql_flexible_server_database.app.name
    POSTGRES_USER                         = azurerm_postgresql_flexible_server.main.administrator_login
    POSTGRES_SSLMODE                      = "require"
    POSTGRES_CONN_MAX_AGE                 = "0" # PgBouncer pools the connections
    ELASTICSEARCH_URL                     = "http://${azurerm_network_interface.search.private_ip_address}:9200"
    ELASTICSEARCH_USER                    = "elastic"
    MILVUS_HOST                           = azurerm_network_interface.search.private_ip_address
    MILVUS_PORT                           = "19530"
    LLM_DEFAULT_PROVIDER                  = "litellm"
    LITELLM_URL                           = "http://ca-${local.name}-litellm/v1"
    OLLAMA_URL                            = "http://ca-${local.name}-ollama"
    ONLYOFFICE_URL                        = "http://ca-${local.name}-onlyoffice"
    ONLYOFFICE_PUBLIC_URL                 = "https://${var.hostnames.onlyoffice}"
    OIDC_RSA_PRIVATE_KEY_PATH             = "/mnt/secrets/oidc-private-key"
    APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.main.connection_string
  }
  api_env_secret = {
    SECRET_KEY             = "django-secret-key"
    POSTGRES_PASSWORD      = "postgres-password"
    REDIS_URL              = "redis-url"
    CELERY_BROKER_URL      = "redis-url"
    CELERY_RESULT_BACKEND  = "redis-url"
    FIELD_ENCRYPTION_KEY   = "field-encryption-key"
    ENCRYPTION_SECRET_KEY  = "encryption-secret-key"
    OPENAI_API_KEY         = "openai-api-key"
    LITELLM_API_KEY        = "litellm-master-key"
    ONLYOFFICE_JWT_SECRET  = "onlyoffice-jwt-secret"
    ELASTICSEARCH_PASSWORD = "elasticsearch-password"
    EMAIL_HOST_PASSWORD    = "email-host-password"
  }

  # One definition per API-image role; sizes from var.api / var.workers
  api_roles = {
    api = {
      service = "web", profile = "Consumption", cpu = var.api.cpu, memory = var.api.memory
      min     = var.api.min_replicas, max = var.api.max_replicas, ingress = true, queue = null, extra = {}
    }
    worker = {
      service = "celery", profile = "heavy", cpu = var.workers.heavy_cpu, memory = var.workers.heavy_memory
      min     = var.workers.heavy_min, max = var.workers.heavy_max, ingress = false, queue = "ai_documines"
      extra   = { CELERY_QUEUES = "ai_documines", CELERY_CONCURRENCY = "2", CELERY_WORKER_NAME = "heavy" }
    }
    worker-fast = {
      service = "celery", profile = "Consumption", cpu = var.workers.fast_cpu, memory = var.workers.fast_memory
      min     = var.workers.fast_min, max = var.workers.fast_max, ingress = false, queue = "ai_documines_fast"
      extra   = { CELERY_QUEUES = "ai_documines_fast", CELERY_POOL = "threads", CELERY_CONCURRENCY = "8", CELERY_WORKER_NAME = "fast" }
    }
    beat = {
      service = "celery_beat", profile = "Consumption", cpu = 0.5, memory = "1Gi"
      min     = 1, max = 1, ingress = false, queue = null, extra = {} # exactly one scheduler
    }
    file-monitor = {
      service = "file_monitor", profile = "Consumption", cpu = 0.5, memory = "1Gi"
      min     = 1, max = 1, ingress = false, queue = null, extra = {}
    }
  }
}

resource "azurerm_container_app" "api_roles" {
  for_each                     = local.api_roles
  name                         = "ca-${local.name}-${each.key}"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = each.value.profile

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apps.id]
  }
  registry {
    server   = local.registry
    identity = azurerm_user_assigned_identity.apps.id
  }
  dynamic "secret" {
    for_each = local.api_secrets
    content {
      name                = secret.key
      key_vault_secret_id = "${local.kv}secrets/${secret.value}"
      identity            = azurerm_user_assigned_identity.apps.id
    }
  }

  dynamic "ingress" {
    for_each = each.value.ingress ? [1] : []
    content {
      external_enabled = true # "external" to the environment = reachable via its internal load balancer
      target_port      = 8020
      transport        = "http"
      traffic_weight {
        latest_revision = true
        percentage      = 100
      }
    }
  }

  template {
    min_replicas = each.value.min
    max_replicas = each.value.max

    container {
      name   = each.key
      image  = local.app_image
      cpu    = each.value.cpu
      memory = each.value.memory

      env {
        name  = "SERVICE_NAME"
        value = each.value.service
      }
      dynamic "env" {
        for_each = merge(local.api_env_plain, each.value.extra)
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.api_env_secret
        content {
          name        = env.key
          secret_name = env.value
        }
      }
      volume_mounts {
        name = "documents"
        path = "/app/media"
      }
      volume_mounts {
        name = "secrets"
        path = "/mnt/secrets"
      }
      dynamic "liveness_probe" {
        for_each = each.value.ingress ? [1] : []
        content {
          transport = "HTTP"
          port      = 8020
          path      = "/api/v1/health/"
        }
      }
      dynamic "readiness_probe" {
        for_each = each.value.ingress ? [1] : []
        content {
          transport = "HTTP"
          port      = 8020
          path      = "/api/v1/health/"
        }
      }
    }

    volume {
      name         = "documents"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.shares["documents"].name
    }
    volume {
      name         = "secrets"
      storage_type = "Secret" # mounts each secret as a file (the OIDC signing key)
    }

    dynamic "http_scale_rule" {
      for_each = each.value.ingress ? [1] : []
      content {
        name                = "concurrent-requests"
        concurrent_requests = var.api.concurrent_requests
      }
    }
    dynamic "custom_scale_rule" {
      for_each = each.value.queue != null ? [1] : []
      content {
        name             = "queue-length"
        custom_rule_type = "redis"
        metadata = {
          address    = "${azurerm_managed_redis.main.hostname}:${azurerm_managed_redis.main.default_database[0].port}"
          listName   = each.value.queue
          listLength = "5"
          enableTLS  = "true"
        }
        authentication {
          secret_name       = "redis-password"
          trigger_parameter = "password"
        }
      }
    }
  }

  # The queue scaler needs the Redis key on its own
  dynamic "secret" {
    for_each = each.value.queue != null ? [1] : []
    content {
      name                = "redis-password"
      key_vault_secret_id = azurerm_key_vault_secret.redis_password.versionless_id
      identity            = azurerm_user_assigned_identity.apps.id
    }
  }

  tags = local.tags
  depends_on = [azurerm_role_assignment.kv_reader_apps, azurerm_role_assignment.acr_pull,
  azurerm_key_vault_secret.generated, azurerm_key_vault_secret.supplied, azurerm_key_vault_secret.redis_url]
}

resource "azurerm_key_vault_secret" "redis_password" {
  name         = "redis-password"
  value        = azurerm_managed_redis.main.default_database[0].primary_access_key
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [azurerm_role_assignment.kv_admin_deployer]
}

# Migrations run once per deploy, before new revisions take traffic (deploy workflow starts it).
resource "azurerm_container_app_job" "migrate" {
  name                         = "caj-${local.name}-migrate"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 1800
  replica_retry_limit          = 0
  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apps.id]
  }
  registry {
    server   = local.registry
    identity = azurerm_user_assigned_identity.apps.id
  }
  dynamic "secret" {
    for_each = local.api_secrets
    content {
      name                = secret.key
      key_vault_secret_id = "${local.kv}secrets/${secret.value}"
      identity            = azurerm_user_assigned_identity.apps.id
    }
  }
  template {
    container {
      name   = "migrate"
      image  = local.app_image
      cpu    = 1
      memory = "2Gi"
      env {
        name  = "SERVICE_NAME"
        value = "db_prepare"
      }
      dynamic "env" {
        for_each = local.api_env_plain
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.api_env_secret
        content {
          name        = env.key
          secret_name = env.value
        }
      }
      volume_mounts {
        name = "documents"
        path = "/app/media"
      }
      volume_mounts {
        name = "secrets"
        path = "/mnt/secrets"
      }
    }
    volume {
      name         = "documents"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.shares["documents"].name
    }
    volume {
      name         = "secrets"
      storage_type = "Secret"
    }
  }
  tags = local.tags
  depends_on = [azurerm_role_assignment.kv_reader_apps, azurerm_role_assignment.acr_pull,
  azurerm_key_vault_secret.generated, azurerm_key_vault_secret.supplied, azurerm_key_vault_secret.redis_url]
}

# ── Frontend (Angular, nginx) ────────────────────────────────────────────────
resource "azurerm_container_app" "frontend" {
  name                         = "ca-${local.name}-frontend"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apps.id]
  }
  registry {
    server   = local.registry
    identity = azurerm_user_assigned_identity.apps.id
  }
  ingress {
    external_enabled = true
    target_port      = 80
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 2
    max_replicas = 4
    container {
      name   = "frontend"
      image  = "${local.registry}/aidocumines-frontend:${var.image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"
    }
    http_scale_rule {
      name                = "concurrent-requests"
      concurrent_requests = 200
    }
  }
  tags       = local.tags
  depends_on = [azurerm_role_assignment.acr_pull]
}

# ── Node sign-in service ─────────────────────────────────────────────────────
resource "azurerm_container_app" "backend" {
  name                         = "ca-${local.name}-backend"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apps.id]
  }
  registry {
    server   = local.registry
    identity = azurerm_user_assigned_identity.apps.id
  }
  secret {
    name                = "postgres-password"
    key_vault_secret_id = "${local.kv}secrets/postgres-admin-password"
    identity            = azurerm_user_assigned_identity.apps.id
  }
  ingress {
    external_enabled = true
    target_port      = 7000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 1
    max_replicas = 3
    container {
      name   = "backend"
      image  = "${local.registry}/aidocumines-backend:${var.image_tag}"
      cpu    = 0.5
      memory = "1Gi"
      dynamic "env" {
        for_each = {
          PORT          = "7000"
          DB_HOST       = azurerm_postgresql_flexible_server.main.fqdn
          DB_PORT       = "6432"
          DB_NAME       = azurerm_postgresql_flexible_server_database.nodeauth.name
          DB_USER       = azurerm_postgresql_flexible_server.main.administrator_login
          DB_SSL        = "true"
          API_LAYER_URL = "http://ca-${local.name}-api"
          CORS_ORIGINS  = "https://${var.hostnames.app}"
          PLAYBOOKS_DIR = "/app/playbooks"
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name        = "DB_PASSWORD"
        secret_name = "postgres-password"
      }
      volume_mounts {
        name = "playbooks"
        path = "/app/playbooks"
      }
    }
    volume {
      name         = "playbooks"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.shares["playbooks"].name
    }
  }
  tags = local.tags
  depends_on = [azurerm_role_assignment.kv_reader_apps, azurerm_role_assignment.acr_pull,
  azurerm_key_vault_secret.generated, azurerm_key_vault_secret.supplied, azurerm_key_vault_secret.redis_url]
}

# ── LiteLLM gateway (one worker is enough; ~1 GB with several vendor catalogues) ─
resource "azurerm_container_app" "litellm" {
  name                         = "ca-${local.name}-litellm"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apps.id]
  }
  dynamic "secret" {
    for_each = toset(["litellm-master-key", "openai-api-key", "anthropic-api-key", "gemini-api-key", "openrouter-api-key"])
    content {
      name                = secret.key
      key_vault_secret_id = "${local.kv}secrets/${secret.key}"
      identity            = azurerm_user_assigned_identity.apps.id
    }
  }
  ingress {
    external_enabled = false # internal to the environment only
    target_port      = 4000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 1
    max_replicas = 3
    container {
      name   = "litellm"
      image  = "ghcr.io/berriai/litellm:main-stable" # pin a release tag before go-live
      cpu    = 1
      memory = "2Gi"
      args   = ["--config", "/app/litellm/config.yaml", "--port", "4000", "--num_workers", "1"]
      dynamic "env" {
        for_each = {
          LITELLM_MASTER_KEY = "litellm-master-key", OPENAI_API_KEY = "openai-api-key"
          ANTHROPIC_API_KEY  = "anthropic-api-key", GEMINI_API_KEY = "gemini-api-key", OPENROUTER_API_KEY = "openrouter-api-key"
        }
        content {
          name        = env.key
          secret_name = env.value
        }
      }
      volume_mounts {
        name = "config"
        path = "/app/litellm"
      }
    }
    volume {
      name         = "config"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.shares["litellm"].name
    }
    http_scale_rule {
      name                = "concurrent-requests"
      concurrent_requests = 50
    }
  }
  tags       = local.tags
  depends_on = [azurerm_role_assignment.kv_reader_apps]
}

# ── OnlyOffice document server (dedicated profile, its own database) ─────────
resource "azurerm_container_app" "onlyoffice" {
  name                         = "ca-${local.name}-onlyoffice"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "heavy"
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apps.id]
  }
  secret {
    name                = "jwt-secret"
    key_vault_secret_id = "${local.kv}secrets/onlyoffice-jwt-secret"
    identity            = azurerm_user_assigned_identity.apps.id
  }
  secret {
    name                = "db-password"
    key_vault_secret_id = "${local.kv}secrets/postgres-admin-password"
    identity            = azurerm_user_assigned_identity.apps.id
  }
  ingress {
    external_enabled = true
    target_port      = 80
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 1
    max_replicas = 1 # the community edition keeps editing sessions in one instance
    container {
      name   = "onlyoffice"
      image  = "onlyoffice/documentserver:9.1" # pin; upgrade deliberately
      cpu    = 2
      memory = "4Gi"
      dynamic "env" {
        for_each = {
          JWT_ENABLED = "true", JWT_HEADER = "Authorization", JWT_IN_BODY = "true", DB_TYPE = "postgres"
          DB_HOST     = azurerm_postgresql_flexible_server.main.fqdn, DB_PORT = "5432"
          DB_NAME     = azurerm_postgresql_flexible_server_database.onlyoffice.name
          DB_USER     = azurerm_postgresql_flexible_server.main.administrator_login
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name        = "JWT_SECRET"
        secret_name = "jwt-secret"
      }
      env {
        name        = "DB_PWD"
        secret_name = "db-password"
      }
      volume_mounts {
        name = "data"
        path = "/var/www/onlyoffice/Data"
      }
    }
    volume {
      name         = "data"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.shares["onlyoffice"].name
    }
  }
  tags       = local.tags
  depends_on = [azurerm_role_assignment.kv_reader_apps]
}

# ── Ollama (local models on CPU; LiteLLM routes ollama/*) ────────────────────
resource "azurerm_container_app" "ollama" {
  count                        = var.enable_ollama ? 1 : 0
  name                         = "ca-${local.name}-ollama"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  workload_profile_name        = "heavy"
  ingress {
    external_enabled = false
    target_port      = 11434
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
  template {
    min_replicas = 1
    max_replicas = 1
    container {
      name   = "ollama"
      image  = "ollama/ollama:0.12.6" # pin; models are pulled into the share at first start
      cpu    = 4
      memory = "8Gi"
      env {
        name  = "OLLAMA_KEEP_ALIVE"
        value = "5m"
      }
      volume_mounts {
        name = "models"
        path = "/root/.ollama"
      }
    }
    volume {
      name         = "models"
      storage_type = "AzureFile"
      storage_name = azurerm_container_app_environment_storage.shares["ollama"].name
    }
  }
  tags = local.tags
}
