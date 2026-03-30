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

# ── Langfuse ───────────────────────────────────────────────────────
#
# Langfuse strategy depends on compute_platform (set in root module):
#
# ACA mode  → Langfuse Cloud (free tier, https://cloud.langfuse.com)
#             No infra to manage. Set LANGFUSE_HOST, PUBLIC_KEY, SECRET_KEY
#             as env vars on the Container Apps.
#
# AKS mode  → Self-hosted via Helm chart on AKS namespace.
#             DATABASE_URL → PostgreSQL from database module
#             NEXTAUTH_URL → Langfuse ingress URL
#             NEXTAUTH_SECRET → Key Vault secret
#
# From the SDK side both are identical: same env vars, same API.

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
