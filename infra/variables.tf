# Variables — defined per environment via .tfvars

variable "environment" {
  type        = string
  description = "Deployment environment (dev, staging, prod)"
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type    = string
  default = "southeastasia"
}

variable "aks_cluster_name" {
  type    = string
  default = "aks-dsai"
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
