# ── Placement ────────────────────────────────────────────────────────────────
variable "subscription_id" {
  type        = string
  description = "Azure subscription that hosts aiDocuMines."
}

variable "location" {
  type        = string
  default     = "southafricanorth"
  description = "Azure region (Johannesburg)."
}

variable "prefix" {
  type        = string
  default     = "aidoc"
  description = "Short name used in every resource name (letters and digits)."
}

variable "environment" {
  type        = string
  default     = "prod"
  description = "prod | staging"
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ── Public hostnames (Front Door custom domains, managed TLS certificates) ────
variable "hostnames" {
  type = object({
    app        = string # Angular frontend, e.g. app.aidocumines.com
    api        = string # Django API, e.g. api.aidocumines.com
    backend    = string # Node sign-in service, e.g. auth.aidocumines.com
    onlyoffice = string # OnlyOffice document server, e.g. docs.aidocumines.com
  })
}

variable "alert_email" {
  type        = string
  description = "Where platform alerts go."
}

# ── Images (pushed by the deploy workflow) ───────────────────────────────────
variable "image_tag" {
  type        = string
  default     = "latest"
  description = "Tag of the aidocumines-app, -frontend and -backend images in the registry."
}

# ── Sizing (defaults sized from the 2,500-user load test, see docs/AZURE.md) ───
variable "api" {
  type = object({
    cpu = number, memory = string, min_replicas = number, max_replicas = number, concurrent_requests = number
  })
  default = { cpu = 1, memory = "2Gi", min_replicas = 2, max_replicas = 10, concurrent_requests = 40 }
}

variable "workers" {
  description = "Celery: heavy document work (dedicated profile) and the fast lane (consumption)."
  type = object({
    heavy_cpu = number, heavy_memory = string, heavy_min = number, heavy_max = number
    fast_cpu  = number, fast_memory = string, fast_min = number, fast_max = number
  })
  default = {
    heavy_cpu = 2, heavy_memory = "8Gi", heavy_min = 1, heavy_max = 3
    fast_cpu  = 1, fast_memory = "2Gi", fast_min = 1, fast_max = 4
  }
}

variable "dedicated_profile" {
  description = "Workload profile for memory-heavy containers (AI workers, OnlyOffice, Ollama)."
  type        = object({ type = string, min = number, max = number })
  default     = { type = "D4", min = 1, max = 3 } # D4 = 4 vCPU / 16 GiB per node
}

variable "enable_ollama" {
  type        = bool
  default     = true
  description = "Run Ollama (local models, CPU) in the environment and route ollama/* through LiteLLM."
}

variable "postgres" {
  type = object({
    sku_name              = string
    storage_mb            = number
    version               = string
    high_availability     = bool # zone-redundant standby: doubles compute and storage cost
    backup_retention_days = number
    geo_redundant_backup  = bool
  })
  default = {
    sku_name              = "GP_Standard_D4ds_v5" # 4 vCores, 16 GiB: ~2% of a core per request/s, 100 req/s peak
    storage_mb            = 131072                # 128 GiB
    version               = "16"
    high_availability     = false
    backup_retention_days = 14
    geo_redundant_backup  = true
  }
}

variable "redis_sku" {
  type        = string
  default     = "Balanced_B1"
  description = "Azure Managed Redis SKU (task queue + cache)."
}

variable "search_vm_size" {
  type        = string
  default     = "Standard_D4s_v5" # 4 vCPU / 16 GiB: Elasticsearch + Milvus
  description = "Private VM for Elasticsearch and Milvus (they need local SSD; Container Apps has none)."
}

variable "search_disk_gb" {
  type    = number
  default = 256
}

variable "documents_share_quota_gb" {
  type        = number
  default     = 1024
  description = "Azure Files share for documents (mounted at /app/media in the API and workers)."
}

variable "enable_defender" {
  type        = bool
  default     = false
  description = "Turn on Microsoft Defender for Cloud plans. Subscription-wide: affects every resource in it."
}

variable "allowed_admin_cidrs" {
  type        = list(string)
  default     = []
  description = "Public addresses allowed to reach Key Vault / Storage management planes (CI runners use OIDC)."
}
