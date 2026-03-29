terraform {
  required_version = ">= 1.5"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }

  backend "azurerm" {
    # Configure in environments/*.tfvars
  }
}

provider "azurerm" {
  features {}
}

data "azurerm_resource_group" "main" {
  name = var.resource_group_name
}

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
  tags                = var.tags
}

module "observability" {
  source              = "./modules/observability"
  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.main.name
  location            = var.location
  tags                = var.tags
}

module "aks" {
  source              = "./modules/aks"
  environment         = var.environment
  resource_group_name = data.azurerm_resource_group.main.name
  aks_cluster_name    = var.aks_cluster_name
  acr_id              = module.registry.acr_id
  tags                = var.tags
}
