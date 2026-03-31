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
