# Azure OpenAI / Foundry endpoints

variable "environment" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "openai_location" {
  type        = string
  default     = "eastus"
  description = "Region for Azure OpenAI (model availability differs by region)"
}
variable "tags" { type = map(string) }

resource "azurerm_cognitive_account" "openai" {
  name                = "aoai-ao-${var.environment}"
  location            = var.openai_location
  resource_group_name = var.resource_group_name
  kind                = "OpenAI"
  sku_name            = "S0"

  tags = var.tags
}

# ── Model deployments ──────────────────────────────────────────────
#
# Azure free trial has 0 TPM quota for GPT chat models.
# Request quota at https://aka.ms/oai/quotaincrease, then deploy via:
#   Azure Portal → Azure OpenAI → aoai-ao-dev → Model deployments
#
# Recommended models once quota is approved:
#   - gpt-4.1       (Standard/GlobalStandard, capacity 10)
#   - gpt-4.1-mini  (Standard/GlobalStandard, capacity 20)
#   - text-embedding-3-large is already deployed below.

# text-embedding-3-large — for pgvector embeddings
resource "azurerm_cognitive_deployment" "embedding" {
  name                 = "text-embedding-3-large"
  cognitive_account_id = azurerm_cognitive_account.openai.id

  model {
    format  = "OpenAI"
    name    = "text-embedding-3-large"
    version = "1"
  }

  sku {
    name     = "Standard"
    capacity = 20
  }
}

output "openai_endpoint" {
  value = azurerm_cognitive_account.openai.endpoint
}

output "openai_id" {
  value = azurerm_cognitive_account.openai.id
}

# ── Azure AI Content Safety (optional) ───────────────────────────
#
# Requires subscription-level quota for 'ContentSafety' kind.
# Set enable_content_safety = false to skip on free-trial subscriptions.
# When disabled, AZURE_CONTENT_SAFETY_ENDPOINT will be empty and
# the Phase 1 regex guardrail in content_safety.py remains active.

variable "enable_content_safety" {
  type        = bool
  default     = true
  description = "Set false to skip Content Safety provisioning entirely"
}

resource "azurerm_cognitive_account" "content_safety" {
  count               = var.enable_content_safety ? 1 : 0
  name                = "cs-ao-${var.environment}"
  location            = "eastus"
  resource_group_name = var.resource_group_name
  kind                = "ContentSafety"
  sku_name            = "F0"

  tags = var.tags
}

output "content_safety_endpoint" {
  value = var.enable_content_safety ? azurerm_cognitive_account.content_safety[0].endpoint : ""
}

output "content_safety_key" {
  value     = var.enable_content_safety ? azurerm_cognitive_account.content_safety[0].primary_access_key : ""
  sensitive = true
}
