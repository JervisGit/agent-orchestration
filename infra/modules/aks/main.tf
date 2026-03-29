# AKS namespace and RBAC for AO services
#
# Assumes the AKS cluster already exists (shared DSAI cluster).
# This module creates the AO namespace, RBAC bindings, and ACR pull role.

variable "environment" { type = string }
variable "resource_group_name" { type = string }
variable "aks_cluster_name" { type = string }
variable "acr_id" { type = string }
variable "tags" { type = map(string) }

data "azurerm_kubernetes_cluster" "dsai" {
  name                = var.aks_cluster_name
  resource_group_name = var.resource_group_name
}

# Grant AKS kubelet identity pull access to ACR
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                = var.acr_id
  role_definition_name = "AcrPull"
  principal_id         = data.azurerm_kubernetes_cluster.dsai.kubelet_identity[0].object_id
}

# AKS namespace and workloads are managed via Helm/kubectl in CI/CD.
# Kubernetes manifests for the AO namespace:
#
# apiVersion: v1
# kind: Namespace
# metadata:
#   name: ao-${var.environment}
#   labels:
#     app.kubernetes.io/part-of: agent-orchestration

output "aks_cluster_id" {
  value = data.azurerm_kubernetes_cluster.dsai.id
}

output "kube_config" {
  value     = data.azurerm_kubernetes_cluster.dsai.kube_config_raw
  sensitive = true
}
