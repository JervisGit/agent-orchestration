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

variable "tags" {
  type = map(string)
  default = {
    project   = "agent-orchestration"
    managedBy = "terraform"
  }
}
