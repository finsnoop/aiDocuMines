# Front Door Premium + WAF is the only way in. Origins are reached over Private Link into the internal
# Container Apps environment, so the apps have no public address of their own.

resource "azurerm_cdn_frontdoor_profile" "main" {
  name                     = "afd-${local.name}"
  resource_group_name      = azurerm_resource_group.main.name
  sku_name                 = "Premium_AzureFrontDoor"
  response_timeout_seconds = 120 # long AI answers and uploads
  tags                     = local.tags
}

resource "azurerm_cdn_frontdoor_firewall_policy" "main" {
  name                              = "waf${replace(local.name, "-", "")}"
  resource_group_name               = azurerm_resource_group.main.name
  sku_name                          = azurerm_cdn_frontdoor_profile.main.sku_name
  enabled                           = true
  mode                              = "Prevention"
  request_body_check_enabled        = true
  custom_block_response_status_code = 403

  managed_rule {
    type    = "Microsoft_DefaultRuleSet"
    version = "2.1"
    action  = "Block"
  }
  managed_rule {
    type    = "Microsoft_BotManagerRuleSet"
    version = "1.1"
    action  = "Block"
  }

  # Generous per-address ceiling in front of the application's own per-account limits:
  # an office of hundreds behind one address must still be able to work.
  custom_rule {
    name                           = "RateLimitPerAddress"
    enabled                        = true
    priority                       = 100
    type                           = "RateLimitRule"
    rate_limit_duration_in_minutes = 1
    rate_limit_threshold           = 3000
    action                         = "Block"
    match_condition {
      match_variable = "RequestUri"
      operator       = "RegEx"
      match_values   = [".*"]
    }
  }
  tags = local.tags
}

resource "azurerm_cdn_frontdoor_endpoint" "main" {
  name                     = "fde-${local.flat}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.main.id
  tags                     = local.tags
}

locals {
  sites = {
    app = {
      hostname = var.hostnames.app, origin = azurerm_container_app.frontend.ingress[0].fqdn, probe = "/"
    }
    api = {
      hostname = var.hostnames.api, origin = azurerm_container_app.api_roles["api"].ingress[0].fqdn, probe = "/api/v1/health/"
    }
    backend = {
      hostname = var.hostnames.backend, origin = azurerm_container_app.backend.ingress[0].fqdn, probe = "/"
    }
    onlyoffice = {
      hostname = var.hostnames.onlyoffice, origin = azurerm_container_app.onlyoffice.ingress[0].fqdn, probe = "/healthcheck"
    }
  }
}

resource "azurerm_cdn_frontdoor_custom_domain" "sites" {
  for_each                 = local.sites
  name                     = "cd-${each.key}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.main.id
  host_name                = each.value.hostname
  tls {
    certificate_type = "ManagedCertificate"
  }
}

resource "azurerm_cdn_frontdoor_origin_group" "sites" {
  for_each                 = local.sites
  name                     = "og-${each.key}"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.main.id
  session_affinity_enabled = false
  load_balancing {
    sample_size                 = 4
    successful_samples_required = 3
  }
  health_probe {
    path                = each.value.probe
    protocol            = "Https"
    interval_in_seconds = 60
    request_type        = "GET"
  }
}

resource "azurerm_cdn_frontdoor_origin" "sites" {
  for_each                       = local.sites
  name                           = "origin-${each.key}"
  cdn_frontdoor_origin_group_id  = azurerm_cdn_frontdoor_origin_group.sites[each.key].id
  enabled                        = true
  host_name                      = each.value.origin
  origin_host_header             = each.value.origin
  certificate_name_check_enabled = true
  private_link {
    request_message        = "Front Door to aiDocuMines Container Apps"
    target_type            = "managedEnvironments"
    location               = azurerm_resource_group.main.location
    private_link_target_id = azurerm_container_app_environment.main.id
  }
}

resource "azurerm_cdn_frontdoor_route" "sites" {
  for_each                        = local.sites
  name                            = "route-${each.key}"
  cdn_frontdoor_endpoint_id       = azurerm_cdn_frontdoor_endpoint.main.id
  cdn_frontdoor_origin_group_id   = azurerm_cdn_frontdoor_origin_group.sites[each.key].id
  cdn_frontdoor_origin_ids        = [azurerm_cdn_frontdoor_origin.sites[each.key].id]
  cdn_frontdoor_custom_domain_ids = [azurerm_cdn_frontdoor_custom_domain.sites[each.key].id]
  supported_protocols             = ["Http", "Https"]
  https_redirect_enabled          = true
  forwarding_protocol             = "HttpsOnly"
  patterns_to_match               = ["/*"]
  link_to_default_domain          = false
}

resource "azurerm_cdn_frontdoor_security_policy" "waf" {
  name                     = "waf-all-sites"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.main.id
  security_policies {
    firewall {
      cdn_frontdoor_firewall_policy_id = azurerm_cdn_frontdoor_firewall_policy.main.id
      association {
        dynamic "domain" {
          for_each = azurerm_cdn_frontdoor_custom_domain.sites
          content {
            cdn_frontdoor_domain_id = domain.value.id
          }
        }
        patterns_to_match = ["/*"]
      }
    }
  }
}
