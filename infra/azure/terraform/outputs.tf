output "resource_group" {
  value = azurerm_resource_group.main.name
}

output "registry" {
  value       = azurerm_container_registry.main.login_server
  description = "Push aidocumines-app, aidocumines-frontend and aidocumines-backend images here."
}

output "key_vault" {
  value = azurerm_key_vault.main.name
}

output "front_door_endpoint" {
  value       = azurerm_cdn_frontdoor_endpoint.main.host_name
  description = "CNAME each public hostname to this."
}

output "domain_validation" {
  value       = { for k, d in azurerm_cdn_frontdoor_custom_domain.sites : d.host_name => d.validation_token }
  description = "Create TXT record _dnsauth.<hostname> with this value to prove domain ownership."
}

output "migrate_job" {
  value = azurerm_container_app_job.migrate.name
}

output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.main.fqdn
}

output "search_vm_private_ip" {
  value = azurerm_network_interface.search.private_ip_address
}
