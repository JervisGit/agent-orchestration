# Azure Container Apps Environment + AO services
#
# Serverless compute — scales to zero, built-in HTTPS ingress.
# Used when compute_platform = "aca".

variable "environment" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "log_analytics_workspace_id" { type = string }
variable "acr_login_server" { type = string }
variable "acr_id" { type = string }
variable "ao_api_identity_id" { type = string }
variable "ao_worker_identity_id" { type = string }
variable "ao_api_identity_principal_id" { type = string }
variable "ao_worker_identity_principal_id" { type = string }
variable "database_url" {
  type      = string
  sensitive = true
  default   = ""
}
variable "openai_api_key" {
  type      = string
  sensitive = true
  default   = ""
}
variable "langfuse_public_key" {
  type      = string
  sensitive = true
  default   = ""
}
variable "langfuse_secret_key" {
  type      = string
  sensitive = true
  default   = ""
}
variable "langfuse_host" {
  type    = string
  default = "https://cloud.langfuse.com"
}
variable "tags" { type = map(string) }

# ── Container Apps Environment ─────────────────────────────────────

resource "azurerm_container_app_environment" "ao" {
  name                       = "cae-ao-${var.environment}"
  location                   = var.location
  resource_group_name        = var.resource_group_name
  log_analytics_workspace_id = var.log_analytics_workspace_id

  tags = var.tags
}

# ── ACR Pull role for managed identities ───────────────────────────

resource "azurerm_role_assignment" "api_acr_pull" {
  scope                = var.acr_id
  role_definition_name = "AcrPull"
  principal_id         = var.ao_api_identity_principal_id
}

resource "azurerm_role_assignment" "worker_acr_pull" {
  scope                = var.acr_id
  role_definition_name = "AcrPull"
  principal_id         = var.ao_worker_identity_principal_id
}

# ── AO API Container App ──────────────────────────────────────────

resource "azurerm_container_app" "ao_api" {
  name                         = "ca-ao-api-${var.environment}"
  container_app_environment_id = azurerm_container_app_environment.ao.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [var.ao_api_identity_id]
  }

  registry {
    server   = var.acr_login_server
    identity = var.ao_api_identity_id
  }

  secret {
    name  = "database-url"
    value = var.database_url
  }

  template {
    min_replicas = 0
    max_replicas = 3

    container {
      name   = "ao-api"
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = var.tags
}

# ── AO Worker Container App ───────────────────────────────────────

resource "azurerm_container_app" "ao_worker" {
  name                         = "ca-ao-worker-${var.environment}"
  container_app_environment_id = azurerm_container_app_environment.ao.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [var.ao_worker_identity_id]
  }

  registry {
    server   = var.acr_login_server
    identity = var.ao_worker_identity_id
  }

  template {
    min_replicas = 0
    max_replicas = 2

    container {
      name   = "ao-worker"
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
    }
  }

  # Worker has no ingress — processes from Service Bus queues

  tags = var.tags
}

# ── Email Assistant Container App ─────────────────────────────────

resource "azurerm_container_app" "email_assistant" {
  name                         = "ca-email-assistant-${var.environment}"
  container_app_environment_id = azurerm_container_app_environment.ao.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [var.ao_api_identity_id]
  }

  registry {
    server   = var.acr_login_server
    identity = var.ao_api_identity_id
  }

  secret {
    name  = "database-url"
    value = var.database_url
  }
  secret {
    name  = "openai-api-key"
    value = var.openai_api_key
  }
  secret {
    name  = "langfuse-public-key"
    value = var.langfuse_public_key
  }
  secret {
    name  = "langfuse-secret-key"
    value = var.langfuse_secret_key
  }

  template {
    min_replicas = 0
    max_replicas = 2

    container {
      name   = "email-assistant"
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "ENVIRONMENT"
        value = var.environment
      }
      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "OPENAI_API_KEY"
        secret_name = "openai-api-key"
      }
      env {
        name        = "LANGFUSE_PUBLIC_KEY"
        secret_name = "langfuse-public-key"
      }
      env {
        name        = "LANGFUSE_SECRET_KEY"
        secret_name = "langfuse-secret-key"
      }
      env {
        name  = "LANGFUSE_HOST"
        value = var.langfuse_host
      }
      env {
        name  = "AO_PLATFORM_URL"
        value = "https://${azurerm_container_app.ao_api.ingress[0].fqdn}"
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8001
    transport        = "http"

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = var.tags
}

# ── Outputs ────────────────────────────────────────────────────────

output "api_fqdn" {
  value = azurerm_container_app.ao_api.ingress[0].fqdn
}

output "api_url" {
  value = "https://${azurerm_container_app.ao_api.ingress[0].fqdn}"
}

output "email_assistant_url" {
  value = "https://${azurerm_container_app.email_assistant.ingress[0].fqdn}"
}

output "environment_id" {
  value = azurerm_container_app_environment.ao.id
}
