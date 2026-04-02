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
variable "langfuse_database_url" {
  type      = string
  sensitive = true
}
variable "langfuse_nextauth_secret" {
  type      = string
  sensitive = true
}
variable "langfuse_salt" {
  type      = string
  sensitive = true
}
variable "langfuse_admin_email" {
  type    = string
  default = "theofficialjjarvis@gmail.com"
}
variable "langfuse_admin_password" {
  type      = string
  sensitive = true
}
variable "langfuse_init_public_key" {
  type      = string
  sensitive = true
}
variable "langfuse_init_secret_key" {
  type      = string
  sensitive = true
}
variable "langfuse_azure_ad_client_id" {
  type    = string
  default = ""
}
variable "langfuse_azure_ad_client_secret" {
  type      = string
  sensitive = true
  default   = ""
}
variable "langfuse_azure_ad_tenant_id" {
  type    = string
  default = ""
}
variable "email_assistant_langfuse_public_key" {
  type      = string
  sensitive = true
}
variable "email_assistant_langfuse_secret_key" {
  type      = string
  sensitive = true
}
variable "content_safety_endpoint" {
  type    = string
  default = ""
}
variable "content_safety_key" {
  type      = string
  sensitive = true
  default   = ""
}

locals {
  langfuse_url = "https://ca-langfuse-${var.environment}.${azurerm_container_app_environment.ao.default_domain}"
}
variable "servicebus_connection_string" {
  type      = string
  sensitive = true
  default   = ""
}
variable "redis_url" {
  type      = string
  sensitive = true
  default   = ""
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

  lifecycle {
    ignore_changes = [template[0].container[0].image]
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

  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }

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
    value = var.email_assistant_langfuse_public_key
  }
  secret {
    name  = "langfuse-secret-key"
    value = var.email_assistant_langfuse_secret_key
  }
  secret {
    name  = "servicebus-connection-string"
    value = var.servicebus_connection_string
  }
  secret {
    name  = "redis-url"
    value = var.redis_url
  }
  dynamic "secret" {
    for_each = var.content_safety_key != "" ? [1] : []
    content {
      name  = "content-safety-key"
      value = var.content_safety_key
    }
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
        value = local.langfuse_url
      }
      env {
        name        = "SERVICEBUS_CONNECTION_STRING"
        secret_name = "servicebus-connection-string"
      }
      env {
        name        = "REDIS_URL"
        secret_name = "redis-url"
      }
      env {
        name  = "AZURE_CONTENT_SAFETY_ENDPOINT"
        value = var.content_safety_endpoint
      }
      dynamic "env" {
        for_each = var.content_safety_key != "" ? [1] : []
        content {
          name        = "AZURE_CONTENT_SAFETY_KEY"
          secret_name = "content-safety-key"
        }
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

  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }

  tags = var.tags
}

# ── Outputs ────────────────────────────────────────────────────────

# ── Langfuse (Self-Hosted) Container App ─────────────────────────
#
# Langfuse v2 is deployed directly into the ACA environment so that
# all LLM trace data stays within the Azure tenant.
# min_replicas = 1: Next.js SSR + NextAuth session store cannot
# tolerate scale-to-zero cold starts (breaks auth + drops first trace).

resource "azurerm_container_app" "langfuse" {
  name                         = "ca-langfuse-${var.environment}"
  container_app_environment_id = azurerm_container_app_environment.ao.id
  resource_group_name          = var.resource_group_name
  revision_mode                = "Single"

  secret {
    name  = "database-url"
    value = var.langfuse_database_url
  }
  secret {
    name  = "nextauth-secret"
    value = var.langfuse_nextauth_secret
  }
  secret {
    name  = "salt"
    value = var.langfuse_salt
  }
  secret {
    name  = "admin-password"
    value = var.langfuse_admin_password
  }
  secret {
    name  = "init-public-key"
    value = var.langfuse_init_public_key
  }
  secret {
    name  = "init-secret-key"
    value = var.langfuse_init_secret_key
  }
  secret {
    name  = "azure-ad-client-secret"
    value = var.langfuse_azure_ad_client_secret
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "langfuse"
      image  = "langfuse/langfuse:2"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "NEXTAUTH_SECRET"
        secret_name = "nextauth-secret"
      }
      env {
        name  = "NEXTAUTH_URL"
        value = local.langfuse_url
      }
      env {
        name        = "SALT"
        secret_name = "salt"
      }
      env {
        name  = "TELEMETRY_ENABLED"
        value = "false"
      }
      # Seed initial org / project / admin user on first boot.
      # Idempotent: skipped if the IDs already exist in the database.
      env {
        name  = "LANGFUSE_INIT_ORG_ID"
        value = "org-ao-${var.environment}"
      }
      env {
        name  = "LANGFUSE_INIT_ORG_NAME"
        value = "AO Platform"
      }
      env {
        name  = "LANGFUSE_INIT_PROJECT_ID"
        value = "proj-ao-${var.environment}"
      }
      env {
        name  = "LANGFUSE_INIT_PROJECT_NAME"
        value = "agent-orchestration"
      }
      env {
        name        = "LANGFUSE_INIT_PROJECT_PUBLIC_KEY"
        secret_name = "init-public-key"
      }
      env {
        name        = "LANGFUSE_INIT_PROJECT_SECRET_KEY"
        secret_name = "init-secret-key"
      }
      env {
        name  = "LANGFUSE_INIT_USER_EMAIL"
        value = var.langfuse_admin_email
      }
      # Azure AD SSO — only active when client_id is non-empty
      dynamic "env" {
        for_each = var.langfuse_azure_ad_client_id != "" ? [1] : []
        content {
          name  = "AUTH_AZURE_AD_CLIENT_ID"
          value = var.langfuse_azure_ad_client_id
        }
      }
      dynamic "env" {
        for_each = var.langfuse_azure_ad_client_id != "" ? [1] : []
        content {
          name        = "AUTH_AZURE_AD_CLIENT_SECRET"
          secret_name = "azure-ad-client-secret"
        }
      }
      dynamic "env" {
        for_each = var.langfuse_azure_ad_tenant_id != "" ? [1] : []
        content {
          name  = "AUTH_AZURE_AD_TENANT_ID"
          value = var.langfuse_azure_ad_tenant_id
        }
      }
      env {
        name  = "LANGFUSE_INIT_USER_NAME"
        value = "AO Admin"
      }
      env {
        name        = "LANGFUSE_INIT_USER_PASSWORD"
        secret_name = "admin-password"
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 3000
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

output "langfuse_url" {
  value = "https://${azurerm_container_app.langfuse.ingress[0].fqdn}"
}
