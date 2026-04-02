terraform {
  required_version = ">= 1.5"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
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

# ── Langfuse self-hosted secrets (generated once, stored in tfstate) ───

resource "random_password" "langfuse_nextauth_secret" {
  length  = 48
  special = false
}

resource "random_password" "langfuse_salt" {
  length  = 32
  special = false
}

resource "random_uuid" "langfuse_public_key" {}
resource "random_uuid" "langfuse_secret_key" {}

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
  database_url                    = module.database.postgresql_connection_string
  openai_api_key                  = var.openai_api_key
  langfuse_database_url           = module.database.langfuse_connection_string
  langfuse_nextauth_secret        = random_password.langfuse_nextauth_secret.result
  langfuse_salt                   = random_password.langfuse_salt.result
  langfuse_admin_password         = var.langfuse_admin_password
  langfuse_init_public_key        = "pk-lf-${random_uuid.langfuse_public_key.result}"
  langfuse_init_secret_key        = "sk-lf-${random_uuid.langfuse_secret_key.result}"
  langfuse_azure_ad_client_id          = var.langfuse_azure_ad_client_id
  langfuse_azure_ad_client_secret      = var.langfuse_azure_ad_client_secret
  langfuse_azure_ad_tenant_id          = var.langfuse_azure_ad_tenant_id
  email_assistant_langfuse_public_key  = var.email_assistant_langfuse_public_key
  email_assistant_langfuse_secret_key  = var.email_assistant_langfuse_secret_key
  servicebus_connection_string    = module.messaging.servicebus_connection_string
  redis_url                       = module.database.redis_connection_string
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

output "email_assistant_url" {
  value = var.compute_platform == "aca" ? module.aca[0].email_assistant_url : "kubectl port-forward or ingress — see AKS deployment docs"
}

output "langfuse_url" {
  value = var.compute_platform == "aca" ? module.aca[0].langfuse_url : "kubectl port-forward or ingress — see AKS deployment docs"
}

output "langfuse_public_key" {
  value     = "pk-lf-${random_uuid.langfuse_public_key.result}"
  sensitive = true
}

output "langfuse_secret_key" {
  value     = "sk-lf-${random_uuid.langfuse_secret_key.result}"
  sensitive = true
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
