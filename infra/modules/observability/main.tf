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
  retention_in_days   = 30

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
# Langfuse v2 is self-hosted as a Container App in the same ACA environment
# as the rest of the platform (ACA mode). Data never leaves the Azure tenant.
#
# The Langfuse container app is defined in modules/aca/main.tf.
# It connects to the dedicated `langfuse` database on the shared PostgreSQL
# Flexible Server and is seeded on first boot via LANGFUSE_INIT_* env vars.
#
# AKS mode → Self-hosted via Helm chart on AKS namespace.
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
