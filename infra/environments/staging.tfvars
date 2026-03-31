environment         = "staging"
subscription_id     = "78205397-1833-43c4-977e-d177b245a3ad"
resource_group_name = "rg-ao-staging"
location            = "southeastasia"
compute_platform    = "aca"
# postgres_admin_password — set via TF_VAR_postgres_admin_password

tags = {
  project     = "agent-orchestration"
  environment = "staging"
  managedBy   = "terraform"
}
