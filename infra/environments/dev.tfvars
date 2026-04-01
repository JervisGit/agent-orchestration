environment         = "dev"
subscription_id     = "78205397-1833-43c4-977e-d177b245a3ad"
resource_group_name = "rg-ao-dev"
location            = "southeastasia"
compute_platform    = "aca" # Flip to "aks" to switch compute
# aks_cluster_name     = "aks-dsai-dev"        # Uncomment when compute_platform = "aks"
postgres_admin_password = "Test12345678" # Set via TF_VAR_postgres_admin_password
