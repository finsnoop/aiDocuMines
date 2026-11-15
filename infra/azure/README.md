# aiDocuMines on Azure (Container Apps, South Africa North)

Terraform for production: `terraform/`. Cost model with Microsoft's published prices: `pricing/`.
Sizing and the load test behind it: `../../docs/AZURE.md`.

## What gets built

| Layer | Azure service | Why |
|---|---|---|
| Edge | Front Door Premium + WAF (DRS 2.1, bot rules, 3,000 req/min per address) | Only way in; reaches the apps over Private Link |
| Apps | Container Apps (internal environment, workload profiles) | API, frontend, Node sign-in, LiteLLM on Consumption; AI worker, OnlyOffice, Ollama on a D4 dedicated profile |
| Jobs | Container Apps Job `migrate` | Database migrations once per deploy |
| Database | PostgreSQL Flexible Server 16, GP D4ds_v5, private, PgBouncer (6432) | One database for the platform, one for the Node service, one for OnlyOffice |
| Queue/cache | Azure Managed Redis Balanced B1, private, TLS | Celery broker + Django cache (database 0; cache keys prefixed) |
| Documents | Azure Files (Hot ZRS) mounted at `/app/media` | Every API/worker replica sees the same files, no code change |
| Search | Private VM D4s_v5 + Premium SSD (Elasticsearch 8.19.25, Milvus 2.6.17) | Both need local disks; Container Apps has none |
| Secrets | Key Vault (RBAC) + user-assigned identity | Containers read secrets directly from Key Vault |
| Images | Container Registry Standard | Pulled with the managed identity (no admin user) |
| Ops | Log Analytics, Application Insights, metric alerts, Recovery Services (VM + Files backups) | |

## First deployment

1. **State storage (once).**
   ```bash
   az group create -n rg-aidoc-tfstate -l southafricanorth
   az storage account create -n staidoctfstate$RANDOM -g rg-aidoc-tfstate --sku Standard_ZRS --min-tls-version TLS1_2 --allow-blob-public-access false
   az storage container create -n tfstate --account-name <that account> --auth-mode login
   ```
   Copy `terraform/environments/prod.backend.hcl.example` to `prod.backend.hcl` with that account name.

2. **Variables.** Copy `prod.tfvars.example` to `prod.tfvars` (not committed): subscription, hostnames, alert email.

3. **Apply.**
   ```bash
   cd terraform
   terraform init -backend-config=environments/prod.backend.hcl
   terraform plan -var-file=environments/prod.tfvars -out plan.out
   terraform apply plan.out
   ```

4. **Secrets you supply** (Terraform created placeholders and never overwrites them):
   ```bash
   KV=$(terraform output -raw key_vault)
   # MUST be the values from the current server's .env, or encrypted data cannot be read after migration:
   az keyvault secret set --vault-name $KV -n field-encryption-key  --value '<FIELD_ENCRYPTION_KEY>'
   az keyvault secret set --vault-name $KV -n encryption-secret-key --value '<ENCRYPTION_SECRET_KEY>'
   az keyvault secret set --vault-name $KV -n oidc-private-key      --file oidc-private-key.pem
   az keyvault secret set --vault-name $KV -n openai-api-key        --value '<key>'
   az keyvault secret set --vault-name $KV -n email-host-password   --value '<new app password>'
   # optional: anthropic-api-key, gemini-api-key, openrouter-api-key
   ```

5. **Images.** Build and push `aidocumines-app` (API repo), `aidocumines-frontend` (frontend repo, with
   `--build-arg API_URL=https://<api host> BACKEND_URL=https://<backend host> ONLYOFFICE_URL=https://<docs host>`)
   and `aidocumines-backend` (Node repo) to `$(terraform output -raw registry)` with the tag in `image_tag`.

6. **LiteLLM config.** Upload `litellm/config.yaml` to the `litellm-config` file share, with the Ollama
   `api_base` set to `http://ca-<prefix>-<env>-ollama` (Container Apps name, not the Compose name).

7. **Private Link approval.** Front Door's private endpoint to the Container Apps environment arrives as a
   pending connection: approve it once (portal: Container Apps environment > Networking > Private endpoint
   connections, or `az network private-endpoint-connection approve`).

8. **DNS.** For each hostname: a TXT record `_dnsauth.<host>` with the value from `terraform output domain_validation`,
   and a CNAME to `terraform output front_door_endpoint`. Front Door issues the TLS certificates.

9. **Migrate**, then start the migrations job: `az containerapp job start -n $(terraform output -raw migrate_job) -g <rg>`.

## Moving the data off the VPS

1. Announce a short maintenance window; stop writes (scale the VPS web/workers to 0).
2. **Database**: `pg_dump -Fc` on the VPS, `pg_restore --no-owner` into `aidocumines` on the Flexible Server
   (from a machine in the VNet or via `az postgres flexible-server execute`/a jump container). Same for the
   Node service's database into `nodeauth`.
3. **Documents**: `azcopy copy /home/.../media/* 'https://<storage>.file.core.windows.net/documents?<SAS>' --recursive`.
4. **Search**: Elasticsearch and Milvus are rebuilt from the database (re-index / re-embed task) rather than copied.
5. Run the `migrate` job, switch DNS, watch the alerts and `queue_doctor`.
6. Keep the VPS read-only for a week as a fallback.

## Day 2

- Deploy: push images with a new tag, `terraform apply -var image_tag=<tag>`, start the `migrate` job.
- Scale: `api.max_replicas`, `workers.*_max`, `dedicated_profile.max` in tfvars.
- Database high availability (zone-redundant standby): `postgres.high_availability = true` (doubles database compute/storage).
- Defender for Cloud: `enable_defender = true` (subscription-wide).
