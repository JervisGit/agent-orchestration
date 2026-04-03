# Variables — defined per environment via .tfvars

variable "environment" {
  type        = string
  description = "Deployment environment"
  default     = "dev"
}

variable "subscription_id" {
  type        = string
  description = "Azure subscription ID"
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type    = string
  default = "southeastasia"
}

variable "compute_platform" {
  type        = string
  description = "Compute platform: 'aca' (Container Apps) or 'aks' (Kubernetes Service)"
  default     = "aca"

  validation {
    condition     = contains(["aca", "aks"], var.compute_platform)
    error_message = "compute_platform must be 'aca' or 'aks'."
  }
}

variable "aks_cluster_name" {
  type        = string
  default     = "aks-dsai"
  description = "Name of existing AKS cluster (only used when compute_platform = 'aks')"
}

variable "postgres_admin_password" {
  type      = string
  sensitive = true
}

variable "openai_api_key" {
  type      = string
  sensitive = true
  default   = ""
  description = "OpenAI API key stored in Key Vault and injected into container env vars"
}

variable "langfuse_admin_password" {
  type      = string
  sensitive = true
  description = "Langfuse admin user initial password (seeded on first boot)"
}

variable "langfuse_azure_ad_client_id" {
  type        = string
  description = "Azure AD app registration client ID for Langfuse SSO"
  default     = ""
}

variable "langfuse_azure_ad_client_secret" {
  type        = string
  sensitive   = true
  description = "Azure AD app registration client secret for Langfuse SSO"
  default     = ""
}

variable "langfuse_azure_ad_tenant_id" {
  type        = string
  description = "Azure AD tenant ID for Langfuse SSO"
  default     = ""
}

variable "email_assistant_langfuse_public_key" {
  type        = string
  sensitive   = true
  description = "Langfuse public key for the email-assistant project"
}

variable "email_assistant_langfuse_secret_key" {
  type        = string
  sensitive   = true
  description = "Langfuse secret key for the email-assistant project"
}

variable "rag_search_langfuse_public_key" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Langfuse public key for the rag-search project (optional — omit to disable tracing)"
}

variable "rag_search_langfuse_secret_key" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Langfuse secret key for the rag-search project"
}

variable "graph_compliance_langfuse_public_key" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Langfuse public key for the graph-compliance project (optional — omit to disable tracing)"
}

variable "graph_compliance_langfuse_secret_key" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Langfuse secret key for the graph-compliance project"
}

variable "enable_content_safety" {
  type        = bool
  default     = true
  description = "Provision Azure AI Content Safety (F0 free tier, eastus). Set false to skip entirely."
}

variable "tags" {
  type = map(string)
  default = {
    project   = "agent-orchestration"
    managedBy = "terraform"
  }
}

# ── APIM ────────────────────────────────────────────────────────────

variable "enable_apim" {
  type        = bool
  default     = false
  description = "Provision Azure API Management as the identity gateway for agent tool calls. Set true to enable."
}

variable "apim_publisher_email" {
  type        = string
  default     = ""
  description = "Contact email for the APIM instance (required when enable_apim = true)"
}

variable "apim_publisher_name" {
  type        = string
  default     = "AO Platform Team"
  description = "Publisher organisation name shown in the APIM developer portal"
}

variable "apim_sku_name" {
  type        = string
  default     = "Consumption_0"
  description = "'{tier}_{capacity}' — e.g. 'Consumption_0' (instant, pay-per-call) or 'Developer_1' (full-featured, ~45 min deploy). Consumption capacity is always 0."
}

variable "enable_test_sp" {
  type        = bool
  default     = false
  description = "Create a test service principal with App Roles for local APIM testing. Set false in production."
}

variable "enable_no_role_test_sp" {
  type        = bool
  default     = false
  description = "Create a no-role test service principal for confirming APIM returns 403. Set false in production."
}
