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
