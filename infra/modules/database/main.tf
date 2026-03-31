# PostgreSQL Flexible Server + Azure Cache for Redis

variable "environment" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "admin_password" {
  type      = string
  sensitive = true
}
variable "key_vault_id" { type = string }
variable "tags" { type = map(string) }

# ── PostgreSQL Flexible Server ─────────────────────────────────────

resource "azurerm_postgresql_flexible_server" "ao" {
  name                         = "psql-ao-${var.environment}"
  resource_group_name          = var.resource_group_name
  location                     = var.location
  version                      = "16"
  administrator_login          = "aoadmin"
  administrator_password       = var.admin_password
  storage_mb                   = 32768
  sku_name                     = "B_Standard_B1ms"
  backup_retention_days        = 7
  geo_redundant_backup_enabled = false

  tags = var.tags
}

resource "azurerm_postgresql_flexible_server_database" "ao" {
  name      = "ao"
  server_id = azurerm_postgresql_flexible_server.ao.id
  collation = "en_US.utf8"
  charset   = "UTF8"
}

# Enable pgvector extension
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.ao.id
  value     = "VECTOR,UUID-OSSP"
}

# Store connection string in Key Vault
resource "azurerm_key_vault_secret" "pg_connection" {
  name         = "ao-postgres-connection"
  value        = "postgresql://aoadmin:${var.admin_password}@${azurerm_postgresql_flexible_server.ao.fqdn}:5432/ao?sslmode=require"
  key_vault_id = var.key_vault_id
}

# ── Azure Cache for Redis ──────────────────────────────────────────

resource "azurerm_redis_cache" "ao" {
  name                 = "redis-ao-${var.environment}"
  location             = var.location
  resource_group_name  = var.resource_group_name
  capacity             = 0
  family               = "C"
  sku_name             = "Basic"
  minimum_tls_version  = "1.2"
  non_ssl_port_enabled = false

  redis_configuration {}

  tags = var.tags
}

resource "azurerm_key_vault_secret" "redis_connection" {
  name         = "ao-redis-connection"
  value        = azurerm_redis_cache.ao.primary_connection_string
  key_vault_id = var.key_vault_id
}

# ── Outputs ────────────────────────────────────────────────────────

output "postgresql_fqdn" {
  value = azurerm_postgresql_flexible_server.ao.fqdn
}

output "redis_hostname" {
  value = azurerm_redis_cache.ao.hostname
}
