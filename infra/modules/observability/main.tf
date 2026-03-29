# Langfuse deployment + Azure Monitor / App Insights

variable "environment" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }

# ── Application Insights ──────────────────────────────────────────

resource "azurerm_log_analytics_workspace" "ao" {
  name                = "law-ao-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = var.environment == "prod" ? 90 : 30

  tags = var.tags
}

resource "azurerm_application_insights" "ao" {
  name                = "ai-ao-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  workspace_id        = azurerm_log_analytics_workspace.ao.id
  application_type    = "web"

  tags = var.tags
}

# ── Langfuse (deployed as a container on AKS) ─────────────────────
#
# Langfuse is self-hosted via Helm chart on AKS, not a native Azure
# resource. Configuration here captures the App Insights connection
# for correlation. The actual Langfuse Helm deployment is managed
# via the CI/CD pipeline (see .github/workflows/deploy.yml).
#
# Langfuse environment variables reference:
#   DATABASE_URL  → PostgreSQL from database module
#   NEXTAUTH_URL  → Langfuse ingress URL
#   NEXTAUTH_SECRET → Key Vault secret

output "app_insights_connection_string" {
  value     = azurerm_application_insights.ao.connection_string
  sensitive = true
}

output "app_insights_instrumentation_key" {
  value     = azurerm_application_insights.ao.instrumentation_key
  sensitive = true
}

output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.ao.id
}
