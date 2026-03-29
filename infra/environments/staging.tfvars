environment            = "staging"
resource_group_name    = "rg-ao-staging"
location               = "southeastasia"
aks_cluster_name       = "aks-dsai-staging"
postgres_admin_password = ""  # Set via TF_VAR_postgres_admin_password or pipeline secret
