# Key Vault, Entra app registrations, managed identities

variable "environment" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }

data "azurerm_client_config" "current" {}

# ── Key Vault ──────────────────────────────────────────────────────

resource "azurerm_key_vault" "ao" {
  name                = "kv-ao-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  purge_protection_enabled   = false
  soft_delete_retention_days = 30

  tags = var.tags
}

# Grant current deployer full access
resource "azurerm_key_vault_access_policy" "deployer" {
  key_vault_id = azurerm_key_vault.ao.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = data.azurerm_client_config.current.object_id

  secret_permissions = ["Get", "List", "Set", "Delete"]
}

# ── Managed Identity for AO services ──────────────────────────────

resource "azurerm_user_assigned_identity" "ao_api" {
  name                = "id-ao-api-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "ao_worker" {
  name                = "id-ao-worker-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

# Grant managed identities read access to Key Vault secrets
resource "azurerm_key_vault_access_policy" "ao_api" {
  key_vault_id = azurerm_key_vault.ao.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azurerm_user_assigned_identity.ao_api.principal_id

  secret_permissions = ["Get", "List"]
}

resource "azurerm_key_vault_access_policy" "ao_worker" {
  key_vault_id = azurerm_key_vault.ao.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azurerm_user_assigned_identity.ao_worker.principal_id

  secret_permissions = ["Get", "List"]
}

# ── Outputs ────────────────────────────────────────────────────────

output "key_vault_id" {
  value = azurerm_key_vault.ao.id
}

output "key_vault_uri" {
  value = azurerm_key_vault.ao.vault_uri
}

output "ao_api_identity_id" {
  value = azurerm_user_assigned_identity.ao_api.id
}

output "ao_api_identity_principal_id" {
  value = azurerm_user_assigned_identity.ao_api.principal_id
}

output "ao_worker_identity_id" {
  value = azurerm_user_assigned_identity.ao_worker.id
}

output "ao_worker_identity_principal_id" {
  value = azurerm_user_assigned_identity.ao_worker.principal_id
}
