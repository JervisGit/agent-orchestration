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

# Module references — uncomment as implemented
# module "aks"           { source = "./modules/aks" }
# module "database"      { source = "./modules/database" }
# module "messaging"     { source = "./modules/messaging" }
# module "observability" { source = "./modules/observability" }
# module "security"      { source = "./modules/security" }
# module "ai"            { source = "./modules/ai" }
# module "registry"      { source = "./modules/registry" }
