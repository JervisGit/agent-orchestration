# Azure Container Registry

variable "environment" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }

resource "azurerm_container_registry" "ao" {
  name                = "crao${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = var.environment == "prod" ? "Premium" : "Basic"
  admin_enabled       = false

  tags = var.tags
}

output "acr_id" {
  value = azurerm_container_registry.ao.id
}

output "acr_login_server" {
  value = azurerm_container_registry.ao.login_server
}
