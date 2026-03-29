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
