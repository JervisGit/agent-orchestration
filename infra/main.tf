terraform {
  required_version = ">= 1.5"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }

  backend "azurerm" {
    # Configure via -backend-config flags (see docs/guides/azure-deployment.md)
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}

# ── Foundation: Security, Registry, Database, Messaging, AI, Observability ──

module "security" {
  source              = "./modules/security"
  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.main.name
  location            = var.location
  tags                = var.tags
}

module "registry" {
  source              = "./modules/registry"
  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.main.name
  location            = var.location
  tags                = var.tags
}

module "database" {
  source              = "./modules/database"
  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.main.name
  location            = var.location
  admin_password      = var.postgres_admin_password
  key_vault_id        = module.security.key_vault_id
  tags                = var.tags
}

module "messaging" {
  source              = "./modules/messaging"
  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.main.name
  location            = var.location
  tags                = var.tags
}

module "ai" {
  source              = "./modules/ai"
  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.main.name
  location            = var.location
  openai_location     = "eastus"
  tags                = var.tags
}

module "observability" {
  source              = "./modules/observability"
  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.main.name
  location            = var.location
  tags                = var.tags
}

# ── Compute: ACA or AKS (toggle via var.compute_platform) ─────────

module "aca" {
  source = "./modules/aca"
  count  = var.compute_platform == "aca" ? 1 : 0

  environment                     = var.environment
  resource_group_name             = data.azurerm_resource_group.main.name
  location                        = var.location
  log_analytics_workspace_id      = module.observability.log_analytics_workspace_id
  acr_login_server                = module.registry.acr_login_server
  acr_id                          = module.registry.acr_id
  ao_api_identity_id              = module.security.ao_api_identity_id
  ao_worker_identity_id           = module.security.ao_worker_identity_id
  ao_api_identity_principal_id    = module.security.ao_api_identity_principal_id
  ao_worker_identity_principal_id = module.security.ao_worker_identity_principal_id
  tags                            = var.tags
}

module "aks" {
  source = "./modules/aks"
  count  = var.compute_platform == "aks" ? 1 : 0

  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.main.name
  aks_cluster_name    = var.aks_cluster_name
  acr_id              = module.registry.acr_id
  tags                = var.tags
}

# ── Outputs ────────────────────────────────────────────────────────

output "compute_platform" {
  value = var.compute_platform
}

output "api_url" {
  value = var.compute_platform == "aca" ? module.aca[0].api_url : "kubectl port-forward or ingress — see AKS deployment docs"
}

output "postgresql_fqdn" {
  value = module.database.postgresql_fqdn
}

output "redis_hostname" {
  value = module.database.redis_hostname
}

output "openai_endpoint" {
  value = module.ai.openai_endpoint
}

output "key_vault_uri" {
  value = module.security.key_vault_uri
}
