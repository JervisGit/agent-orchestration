# Azure API Management — identity gateway for agent tool calls
#
# COMPUTE-PLATFORM-AGNOSTIC: APIM validates JWTs regardless of where agents run.
# The token acquisition path differs, but APIM policy is identical in both cases:
#
#   ACA  : ManagedIdentityCredential(client_id=uami_client_id).get_token(scope)
#   AKS  : WorkloadIdentityCredential (pod UAMI via federated credential).get_token(scope)
#
# Switching from ACA to AKS requires no changes to this module at all.
#
# APP ROLES DESIGN (no new UAMI required to add a new permission scope):
#   - One Entra app registration ("apim-ao-{env}") owns App Role definitions.
#   - Roles are per permission scope (e.g. Agents.TaxpayerLookup), not per agent.
#   - The ao_api UAMI is assigned all roles for initial/dev setup.
#   - For per-agent isolation later: create per-agent UAMIs in the security module
#     and replace the blanket azuread_app_role_assignment below with targeted ones.
#     The App Role definitions and APIM policy do NOT change.
#   - To add a new permission scope: add an entry to var.agent_app_roles and add
#     the corresponding operation + operation_policy resource below. No new UAMI.

# ── Variables ──────────────────────────────────────────────────────

variable "environment" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }

variable "publisher_email" {
  type        = string
  description = "Publisher contact email for the APIM instance"
}

variable "publisher_name" {
  type        = string
  description = "Publisher organisation name shown in the APIM developer portal"
}

variable "sku_name" {
  type        = string
  default     = "Consumption_0"
  description = "'{tier}_{capacity}' — e.g. 'Consumption_0' (instant, pay-per-call) or 'Developer_1' (full-featured, ~45 min deploy). Consumption capacity is always 0."
}

variable "ao_api_identity_principal_id" {
  type        = string
  description = "Object ID of the ao_api UAMI service principal — granted all App Roles for initial setup"
}

# Permission scopes exposed via APIM.
# Each entry becomes an App Role + a named value in APIM.
# Adding a new scope here is the ONLY change needed to onboard a new tool category.
variable "agent_app_roles" {
  type = map(object({
    display_name = string
    description  = string
    value        = string
  }))
  default = {
    taxpayer_lookup = {
      display_name = "Agents.TaxpayerLookup"
      description  = "Read taxpayer records from the taxpayer database API"
      value        = "Agents.TaxpayerLookup"
    }
    tax_relief_read = {
      display_name = "Agents.TaxReliefRead"
      description  = "Read tax relief applications and eligibility data"
      value        = "Agents.TaxReliefRead"
    }
    compliance_read = {
      display_name = "Agents.ComplianceRead"
      description  = "Read compliance graph and entity relationship data"
      value        = "Agents.ComplianceRead"
    }
  }
}

variable "tags" { type = map(string) }

data "azurerm_client_config" "current" {}

# ── Stable UUIDs for App Role IDs ─────────────────────────────────
# random_uuid is stable per key across re-applies (IDs stored in tfstate).
# Never change a key — doing so would delete and re-create the App Role,
# breaking all existing token claims that carry the old role value.

resource "random_uuid" "app_role_id" {
  for_each = var.agent_app_roles
}

locals {
  # Identifier URI must contain the tenant ID to satisfy Azure AD app registration policies.
  # Format: api://{tenant_id}/{app_name} — valid for any organisation regardless of verified domain.
  # The scope agents request is: "{identifier_uri}/.default"
  apim_identifier_uri = "api://${data.azurerm_client_config.current.tenant_id}/apim-ao-${var.environment}"
}

# ── Entra App Registration — the API resource agents request tokens for ────────
# Audience  : api://apim-ao-{env}
# Scope     : api://apim-ao-{env}/.default  (used in ManagedIdentityCredential / OBO)
# App Roles : one per entry in var.agent_app_roles

resource "azuread_application" "apim_resource" {
  display_name     = "apim-ao-${var.environment}"
  identifier_uris  = [local.apim_identifier_uri]
  sign_in_audience = "AzureADMyOrg"

  api {
    # v2 tokens: cleaner claims, required for managed identity + OBO interop.
    # Also relaxes the identifier URI tenant-domain restriction for some policies.
    requested_access_token_version = 2
  }

  dynamic "app_role" {
    for_each = var.agent_app_roles
    content {
      allowed_member_types = ["Application"]
      display_name         = app_role.value.display_name
      description          = app_role.value.description
      enabled              = true
      id                   = random_uuid.app_role_id[app_role.key].result
      value                = app_role.value.value
    }
  }
}

resource "azuread_service_principal" "apim_resource" {
  client_id = azuread_application.apim_resource.client_id
}

# ── App Role assignments — ao_api UAMI gets all roles (initial/dev setup) ─────
# Migration path to per-agent isolation:
#   1. Add per-agent UAMIs to infra/modules/security/main.tf
#   2. Add targeted azuread_app_role_assignment blocks here (one per UAMI × role)
#   3. Remove or scope down the blanket assignment below
#   4. Update ao-manifest.yaml: add identity_client_id to each AgentConfig
# Nothing in this module's policy or named values needs to change.

resource "azuread_app_role_assignment" "ao_api" {
  for_each = var.agent_app_roles

  app_role_id         = random_uuid.app_role_id[each.key].result
  principal_object_id = var.ao_api_identity_principal_id
  resource_object_id  = azuread_service_principal.apim_resource.object_id
}

# ── APIM Instance ──────────────────────────────────────────────────

resource "azurerm_api_management" "ao" {
  name                = "apim-ao-${var.environment}"
  location            = var.location
  resource_group_name = var.resource_group_name
  publisher_name      = var.publisher_name
  publisher_email     = var.publisher_email
  sku_name            = var.sku_name

  identity {
    type = "SystemAssigned"
  }

  tags = var.tags
}

# ── Named Values (referenced in policy XML as {{name}}) ───────────
# These values are injected into policy at APIM runtime — not expanded by Terraform.

resource "azurerm_api_management_named_value" "tenant_id" {
  name                = "tenant-id"
  resource_group_name = var.resource_group_name
  api_management_name = azurerm_api_management.ao.name
  display_name        = "tenant-id"
  value               = data.azurerm_client_config.current.tenant_id
  secret              = false
}

resource "azurerm_api_management_named_value" "apim_audience" {
  name                = "apim-audience"
  resource_group_name = var.resource_group_name
  api_management_name = azurerm_api_management.ao.name
  display_name        = "apim-audience"
  value               = local.apim_identifier_uri
  secret              = false
}

# ── AO Agents API ──────────────────────────────────────────────────
# All DSAI tool operations live under path /agents.
# API-level policy: validate JWT audience (applies to every operation).
# Operation-level policy: check the required App Role for that specific endpoint.
# Decoupled: adding a new DSAI backend = new operation + operation_policy resource.

resource "azurerm_api_management_api" "ao_agents" {
  name                  = "ao-agents"
  resource_group_name   = var.resource_group_name
  api_management_name   = azurerm_api_management.ao.name
  revision              = "1"
  display_name          = "AO Agents API"
  path                  = "agents"
  protocols             = ["https"]
  subscription_required = false # Auth via JWT; no subscription key needed
}

# API-level policy — validates JWT audience on every call.
# Role enforcement is deferred to each operation's policy (Step 2 below in each op).
resource "azurerm_api_management_api_policy" "ao_agents" {
  api_name            = azurerm_api_management_api.ao_agents.name
  api_management_name = azurerm_api_management.ao.name
  resource_group_name = var.resource_group_name

  xml_content = <<-XML
    <policies>
      <inbound>
        <!-- Step 1 (all operations): verify the token is issued for this APIM instance -->
        <validate-jwt header-name="Authorization"
                      failed-validation-httpcode="401"
                      failed-validation-error-message="Access denied: valid bearer token required.">
          <openid-config url="https://login.microsoftonline.com/{{tenant-id}}/v2.0/.well-known/openid-configuration" />
          <audiences>
            <audience>{{apim-audience}}</audience>
          </audiences>
        </validate-jwt>
        <base />
      </inbound>
      <backend><base /></backend>
      <outbound><base /></outbound>
      <on-error><base /></on-error>
    </policies>
  XML
}

# ── taxpayer lookup ────────────────────────────────────────────────
# Requires: Agents.TaxpayerLookup App Role
# Backend : taxpayer-api-backend (update url in azurerm_api_management_backend below)

resource "azurerm_api_management_api_operation" "taxpayer_lookup" {
  operation_id        = "get-taxpayer"
  api_name            = azurerm_api_management_api.ao_agents.name
  api_management_name = azurerm_api_management.ao.name
  resource_group_name = var.resource_group_name
  display_name        = "Get Taxpayer"
  method              = "GET"
  url_template        = "/taxpayer/{tin}"
  description         = "Look up a taxpayer record by TIN. Requires Agents.TaxpayerLookup App Role."

  template_parameter {
    name     = "tin"
    required = true
    type     = "string"
  }
}

resource "azurerm_api_management_api_operation_policy" "taxpayer_lookup" {
  operation_id        = azurerm_api_management_api_operation.taxpayer_lookup.operation_id
  api_name            = azurerm_api_management_api.ao_agents.name
  api_management_name = azurerm_api_management.ao.name
  resource_group_name = var.resource_group_name

  xml_content = <<-XML
    <policies>
      <inbound>
        <base />
        <!-- Step 2 (this operation): verify caller holds Agents.TaxpayerLookup role -->
        <validate-jwt header-name="Authorization"
                      failed-validation-httpcode="403"
                      failed-validation-error-message="Access denied: Agents.TaxpayerLookup role required.">
          <openid-config url="https://login.microsoftonline.com/{{tenant-id}}/v2.0/.well-known/openid-configuration" />
          <audiences>
            <audience>{{apim-audience}}</audience>
          </audiences>
          <required-claims>
            <claim name="roles" match="any">
              <value>Agents.TaxpayerLookup</value>
            </claim>
          </required-claims>
        </validate-jwt>
        <set-backend-service backend-id="taxpayer-api-backend" />
      </inbound>
      <backend><base /></backend>
      <outbound><base /></outbound>
      <on-error><base /></on-error>
    </policies>
  XML
}

# Backend — replace url with the actual DSAI taxpayer API endpoint.
# This is the only resource that changes when the downstream API is deployed.
resource "azurerm_api_management_backend" "taxpayer_api" {
  name                = "taxpayer-api-backend"
  resource_group_name = var.resource_group_name
  api_management_name = azurerm_api_management.ao.name
  protocol            = "http"
  url                 = "https://placeholder.internal"
  description         = "Taxpayer database API — set url to the DSAI app's internal API endpoint once deployed"
}

# ── Outputs ────────────────────────────────────────────────────────

output "apim_gateway_url" {
  value       = azurerm_api_management.ao.gateway_url
  description = "Base URL for all APIM calls: {gateway_url}/agents/{operation}"
}

output "apim_id" {
  value = azurerm_api_management.ao.id
}

output "apim_app_client_id" {
  value       = azuread_application.apim_resource.client_id
  description = "Client ID of the APIM Entra app registration"
}

output "apim_app_identifier_uri" {
  value       = local.apim_identifier_uri
  description = "Token scope to request: {identifier_uri}/.default"
}

output "agent_app_role_ids" {
  value       = { for k, _ in var.agent_app_roles : k => random_uuid.app_role_id[k].result }
  description = "Map of role key to stable UUID — for reference in targeted role assignments"
}
