environment             = "prod"
subscription_id         = "" # Set to prod subscription
resource_group_name     = "rg-ao-prod"
location                = "southeastasia"
compute_platform        = "aks" # Company AKS for production
aks_cluster_name        = "aks-dsai-prod"
postgres_admin_password = "" # Set via TF_VAR_postgres_admin_password
