environment            = "prod"
resource_group_name    = "rg-ao-prod"
location               = "southeastasia"
aks_cluster_name       = "aks-dsai-prod"
postgres_admin_password = ""  # Set via TF_VAR_postgres_admin_password or pipeline secret
