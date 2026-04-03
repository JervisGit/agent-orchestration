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

    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 2.0"
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

# azuread uses the same Azure CLI / service principal credentials as azurerm
provider "azuread" {}

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
  enable_content_safety = var.enable_content_safety
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
  content_safety_endpoint         = module.ai.content_safety_endpoint
  content_safety_key              = module.ai.content_safety_key
  langfuse_azure_ad_client_id          = var.langfuse_azure_ad_client_id
  langfuse_azure_ad_client_secret      = var.langfuse_azure_ad_client_secret
  langfuse_azure_ad_tenant_id          = var.langfuse_azure_ad_tenant_id
  email_assistant_langfuse_public_key  = var.email_assistant_langfuse_public_key
  email_assistant_langfuse_secret_key  = var.email_assistant_langfuse_secret_key
  rag_search_langfuse_public_key       = var.rag_search_langfuse_public_key
  rag_search_langfuse_secret_key       = var.rag_search_langfuse_secret_key
  graph_compliance_langfuse_public_key = var.graph_compliance_langfuse_public_key
  graph_compliance_langfuse_secret_key = var.graph_compliance_langfuse_secret_key
  servicebus_connection_string    = module.messaging.servicebus_connection_string
  redis_url                       = module.database.redis_connection_string
  apim_gateway_url                = try(module.apim[0].apim_gateway_url, "")
  apim_scope                      = try(module.apim[0].apim_app_identifier_uri, "") != "" ? "${try(module.apim[0].apim_app_identifier_uri, "")}/.default" : ""
  apim_taxpayer_url               = try(module.apim[0].apim_gateway_url, "") != "" ? "${try(module.apim[0].apim_gateway_url, "")}/agents/taxpayer" : ""
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

# ── APIM: identity gateway for agent tool calls (compute-platform-agnostic) ───
# Toggle with var.enable_apim. Works identically whether compute is ACA or AKS.

module "apim" {
  source = "./modules/apim"
  count  = var.enable_apim ? 1 : 0

  environment                  = var.environment
  resource_group_name          = data.azurerm_resource_group.main.name
  location                     = var.location
  publisher_email              = var.apim_publisher_email
  publisher_name               = var.apim_publisher_name
  sku_name                     = var.apim_sku_name
  ao_api_identity_principal_id = module.security.ao_api_identity_principal_id
  enable_test_sp               = var.enable_test_sp
  enable_no_role_test_sp       = var.enable_no_role_test_sp
  app_insights_instrumentation_key = module.observability.app_insights_instrumentation_key
  backend_urls = {
    # Points APIM at the email assistant's /taxpayer/{tin} endpoint.
    # Uses try() so this works even when compute_platform = "aks" (no ACA module).
    taxpayer_api = try(module.aca[0].email_assistant_url, "")
  }
  tags = var.tags
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

output "rag_search_url" {
  value = var.compute_platform == "aca" ? module.aca[0].rag_search_url : "kubectl port-forward or ingress — see AKS deployment docs"
}

output "graph_compliance_url" {
  value = var.compute_platform == "aca" ? module.aca[0].graph_compliance_url : "kubectl port-forward or ingress — see AKS deployment docs"
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

output "apim_gateway_url" {
  value       = try(module.apim[0].apim_gateway_url, null)
  description = "APIM gateway base URL — null when enable_apim = false"
}

output "apim_app_identifier_uri" {
  value       = try(module.apim[0].apim_app_identifier_uri, null)
  description = "Token scope for agents: {uri}/.default — null when enable_apim = false"
}

output "test_caller_client_id" {
  value       = try(module.apim[0].test_caller_client_id, null)
  description = "Test SP client ID for local APIM testing — null when enable_test_sp = false"
}

output "test_caller_client_secret" {
  value       = try(module.apim[0].test_caller_client_secret, null)
  sensitive   = true
  description = "Test SP client secret — null when enable_test_sp = false"
}

output "no_role_test_caller_client_id" {
  value       = try(module.apim[0].no_role_test_caller_client_id, null)
  description = "No-role test SP client ID for 403 testing — null when enable_no_role_test_sp = false"
}

output "no_role_test_caller_client_secret" {
  value       = try(module.apim[0].no_role_test_caller_client_secret, null)
  sensitive   = true
  description = "No-role test SP client secret — null when enable_no_role_test_sp = false"
}
