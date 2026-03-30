environment             = "staging"
subscription_id         = "" # Set to staging subscription
resource_group_name     = "rg-ao-staging"
location                = "southeastasia"
compute_platform        = "aks" # Company AKS for staging+
aks_cluster_name        = "aks-dsai-staging"
postgres_admin_password = "" # Set via TF_VAR_postgres_admin_password
