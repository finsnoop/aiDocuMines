resource "azurerm_monitor_action_group" "ops" {
  name                = "ag-${local.name}-ops"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "aidocops"
  email_receiver {
    name          = "operations"
    email_address = var.alert_email
  }
  tags = local.tags
}

locals {
  metric_alerts = {
    api-5xx = {
      scope       = azurerm_container_app.api_roles["api"].id, namespace = "Microsoft.App/containerApps"
      metric      = "Requests", aggregation = "Total", operator = "GreaterThan", threshold = 25, severity = 1
      dimension   = { name = "statusCodeCategory", values = ["5xx"] }
      description = "More than 25 server errors in 5 minutes on the API"
    }
    api-restarts = {
      scope     = azurerm_container_app.api_roles["api"].id, namespace = "Microsoft.App/containerApps"
      metric    = "RestartCount", aggregation = "Maximum", operator = "GreaterThan", threshold = 2, severity = 2
      dimension = null, description = "API containers restarting"
    }
    worker-restarts = {
      scope     = azurerm_container_app.api_roles["worker"].id, namespace = "Microsoft.App/containerApps"
      metric    = "RestartCount", aggregation = "Maximum", operator = "GreaterThan", threshold = 2, severity = 2
      dimension = null, description = "AI workers restarting (memory?)"
    }
    db-cpu = {
      scope     = azurerm_postgresql_flexible_server.main.id, namespace = "Microsoft.DBforPostgreSQL/flexibleServers"
      metric    = "cpu_percent", aggregation = "Average", operator = "GreaterThan", threshold = 80, severity = 2
      dimension = null, description = "Database CPU above 80%"
    }
    db-storage = {
      scope     = azurerm_postgresql_flexible_server.main.id, namespace = "Microsoft.DBforPostgreSQL/flexibleServers"
      metric    = "storage_percent", aggregation = "Average", operator = "GreaterThan", threshold = 80, severity = 1
      dimension = null, description = "Database storage above 80%"
    }
    db-connections = {
      scope     = azurerm_postgresql_flexible_server.main.id, namespace = "Microsoft.DBforPostgreSQL/flexibleServers"
      metric    = "active_connections", aggregation = "Maximum", operator = "GreaterThan", threshold = 700, severity = 2
      dimension = null, description = "Database connections near the limit"
    }
  }
}

resource "azurerm_monitor_metric_alert" "alerts" {
  for_each            = local.metric_alerts
  name                = "alert-${local.name}-${each.key}"
  resource_group_name = azurerm_resource_group.main.name
  scopes              = [each.value.scope]
  description         = each.value.description
  severity            = each.value.severity
  frequency           = "PT1M"
  window_size         = "PT5M"
  criteria {
    metric_namespace = each.value.namespace
    metric_name      = each.value.metric
    aggregation      = each.value.aggregation
    operator         = each.value.operator
    threshold        = each.value.threshold
    dynamic "dimension" {
      for_each = each.value.dimension == null ? [] : [each.value.dimension]
      content {
        name     = dimension.value.name
        operator = "Include"
        values   = dimension.value.values
      }
    }
  }
  action {
    action_group_id = azurerm_monitor_action_group.ops.id
  }
  tags = local.tags
}
