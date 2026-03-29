environment            = "dev"
resource_group_name    = "rg-ao-dev"
location               = "southeastasia"
aks_cluster_name       = "aks-dsai-dev"
postgres_admin_password = ""  # Set via TF_VAR_postgres_admin_password or pipeline secret
