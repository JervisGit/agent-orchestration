# Azure Deployment Guide

How to deploy AO and DSAI apps to Azure infrastructure. Supports two compute modes:

- **ACA (default)** — Azure Container Apps, serverless, scales to zero. Best for dev/trial.
- **AKS (toggle)** — Azure Kubernetes Service. Best for company clusters.

Switch between them with a single variable: `compute_platform = "aca"` or `"aks"` in your `.tfvars` file. See [ADR-005](../decisions/005-compute-platform.md) for the rationale.

---

## Prerequisites

| Tool | Purpose |
|---|---|
| Azure CLI (`az`) | Azure resource management |
| Terraform >= 1.5 | Infrastructure provisioning |
| Docker | Container image builds |
| GitHub repo access | CI/CD pipeline runs |
| kubectl *(AKS only)* | Cluster access |
| Helm *(AKS only)* | Langfuse self-hosted deployment |

### Required Azure permissions

- **Contributor** on the resource group
- **User Access Administrator** for role assignments (managed identities)
- **Key Vault Administrator** for secret management

---

## 1. Provision Infrastructure with Terraform

### First-time setup

```bash
cd infra

# Login to Azure
az login
az account set --subscription 78205397-1833-43c4-977e-d177b245a3ad

# Create resource group for AO resources
az group create -n rg-ao-dev -l southeastasia

# Terraform state backend (already created)
# Storage account: aoterraformstate1, container: tfstate, RG: rg-ao-tfstate
```

### Deploy (ACA mode — default)

```bash
terraform init \
  -backend-config="resource_group_name=rg-ao-tfstate" \
  -backend-config="storage_account_name=aoterraformstate1" \
  -backend-config="container_name=tfstate" \
  -backend-config="key=ao-dev.tfstate"

# Set the postgres password (don't commit this)
export TF_VAR_postgres_admin_password="<strong-password>"
# On Windows PowerShell:
# $env:TF_VAR_postgres_admin_password = "<strong-password>"

terraform plan -var-file=environments/dev.tfvars
terraform apply -var-file=environments/dev.tfvars
```

### Switch to AKS mode

Edit `environments/dev.tfvars`:
```hcl
compute_platform  = "aks"
aks_cluster_name  = "aks-dsai-dev"
```

Then re-run `terraform plan` and `terraform apply`. Terraform will destroy the ACA resources and create AKS bindings instead.

### What Terraform creates

| Resource | ACA Mode | AKS Mode |
|---|---|---|
| Container Apps Environment + 2 apps | Yes | No |
| AKS namespace + ACR pull role | No | Yes |
| PostgreSQL Flexible (B_Standard_B1ms) | Yes | Yes |
| Azure Cache Redis (Basic C0) | Yes | Yes |
| Azure Container Registry (Basic) | Yes | Yes |
| Azure OpenAI (gpt-4o + gpt-4o-mini + embeddings) | Yes | Yes |
| Service Bus (Basic) | Yes | Yes |
| Key Vault | Yes | Yes |
| Log Analytics + App Insights | Yes | Yes |
| Managed Identities (API + Worker) | Yes | Yes |

### Estimated monthly cost (dev/trial)

| Resource | ~Cost |
|---|---|
| PostgreSQL B1ms | $13 |
| Redis Basic C0 | $7 |
| Azure OpenAI (pay-per-token) | $5–15 |
| ACR Basic | $5 |
| Service Bus Basic | $0.05 |
| Container Apps (scale-to-zero) | $0–5 |
| Log Analytics (first 5 GB free) | $0 |
| **Total** | **~$30–45/month** |

Well within the $200/30-day trial budget.

---

## 2. Configure Langfuse

### ACA mode → Langfuse Cloud (free tier)

1. Sign up at https://cloud.langfuse.com
2. Create projects: `email-assistant`, `rag-search`, `graph-compliance`
3. Generate API keys per project
4. Store keys in Key Vault:
   ```bash
   VAULT=kv-ao-dev
   az keyvault secret set --vault-name $VAULT --name langfuse-host --value "https://cloud.langfuse.com"
   az keyvault secret set --vault-name $VAULT --name langfuse-email-public-key --value "pk-lf-..."
   az keyvault secret set --vault-name $VAULT --name langfuse-email-secret-key --value "sk-lf-..."
   ```

### AKS mode → Self-hosted Langfuse

```bash
# Get AKS credentials
az aks get-credentials --resource-group rg-ao-dev --name aks-dsai-dev

# Create namespace
kubectl create namespace ao-dev

# Deploy Langfuse via Helm
helm repo add langfuse https://langfuse.github.io/langfuse-k8s
helm repo update
helm install langfuse langfuse/langfuse \
  --namespace ao-dev \
  --set database.url="postgresql://aoadmin:<pw>@psql-ao-dev.postgres.database.azure.com:5432/ao?sslmode=require" \
  --set nextauth.secret="<generate-a-secret>" \
  --set nextauth.url="https://langfuse-dev.<your-domain>"
```

> From the SDK side, both modes are identical. The AO code only reads `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` env vars.

---

## 3. Store Secrets in Key Vault

```bash
VAULT=kv-ao-dev

# Database (stored automatically by Terraform)
# ao-postgres-connection, ao-redis-connection

# Azure OpenAI
az keyvault secret set --vault-name $VAULT --name azure-openai-api-key --value <key>

# Langfuse
az keyvault secret set --vault-name $VAULT --name langfuse-host --value "https://cloud.langfuse.com"
az keyvault secret set --vault-name $VAULT --name langfuse-email-public-key --value <key>
az keyvault secret set --vault-name $VAULT --name langfuse-email-secret-key --value <key>

# App-specific
az keyvault secret set --vault-name $VAULT --name neo4j-credentials --value <user:pass>
```

---

## 4. Build & Push Container Images

```bash
# Login to ACR
az acr login --name craodev

# Build and push
docker build -f docker/Dockerfile.ao-api -t craodev.azurecr.io/ao-api:latest .
docker push craodev.azurecr.io/ao-api:latest

docker build -f docker/Dockerfile.ao-worker -t craodev.azurecr.io/ao-worker:latest .
docker push craodev.azurecr.io/ao-worker:latest
```

---

## 5. Deploy AO Services

### ACA mode

Container Apps are created by Terraform. To update the image after a new build:

```bash
az containerapp update \
  --name ca-ao-api-dev \
  --resource-group rg-ao-dev \
  --image craodev.azurecr.io/ao-api:latest

az containerapp update \
  --name ca-ao-worker-dev \
  --resource-group rg-ao-dev \
  --image craodev.azurecr.io/ao-worker:latest
```

Set environment variables (secrets from Key Vault):
```bash
az containerapp update \
  --name ca-ao-api-dev \
  --resource-group rg-ao-dev \
  --set-env-vars \
    "DATABASE_URL=secretref:ao-postgres-connection" \
    "REDIS_URL=secretref:ao-redis-connection" \
    "LANGFUSE_HOST=https://cloud.langfuse.com"
```

Verify:
```bash
# Get the FQDN
az containerapp show --name ca-ao-api-dev --resource-group rg-ao-dev --query "properties.configuration.ingress.fqdn" -o tsv

# Health check
curl https://<fqdn>/health
```

### AKS mode

```bash
az aks get-credentials --resource-group rg-ao-dev --name aks-dsai-dev

kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ao-api
  namespace: ao-dev
spec:
  replicas: 2
  selector:
    matchLabels:
      app: ao-api
  template:
    metadata:
      labels:
        app: ao-api
    spec:
      containers:
      - name: ao-api
        image: craodev.azurecr.io/ao-api:latest
        ports:
        - containerPort: 8000
        env:
        - name: REDIS_URL
          valueFrom:
            secretKeyRef:
              name: ao-secrets
              key: redis-connection
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: ao-secrets
              key: postgres-connection
        - name: LANGFUSE_HOST
          value: "http://langfuse:3000"
---
apiVersion: v1
kind: Service
metadata:
  name: ao-api
  namespace: ao-dev
spec:
  selector:
    app: ao-api
  ports:
  - port: 8000
    targetPort: 8000
EOF

kubectl get pods -n ao-dev
```

---

## 6. Onboard a DSAI App

1. Install the SDK: `pip install ao-core`
2. Create `ao-manifest.yaml` (see `examples/`)
3. Store app secrets in Key Vault
4. Build workflow using AO patterns
5. Verify traces in Langfuse

See the app manifest examples in `examples/email_assistant/ao-manifest.yaml`.

---

## 7. End-to-End Validation Checklist

| # | Check | How |
|---|---|---|
| 1 | AO API health | `curl https://<api-url>/health` |
| 2 | PostgreSQL | Run a simple query from the API container |
| 3 | Redis | Verify session state write/read |
| 4 | Azure OpenAI | Run a completion from inside the container |
| 5 | Langfuse traces | Run a demo workflow, check traces in Langfuse UI |
| 6 | HITL flow | Submit approval, verify in `/api/hitl/pending` |
| 7 | Policy enforcement | Send PII input, verify redaction/blocking |
| 8 | Service Bus | Trigger dead-letter, verify DLQ message |

---

## Rollback

### ACA mode
```bash
# List revisions
az containerapp revision list --name ca-ao-api-dev --resource-group rg-ao-dev -o table

# Activate a previous revision
az containerapp revision activate --name <revision-name> --resource-group rg-ao-dev
```

### AKS mode
```bash
kubectl rollout undo deployment/ao-api -n ao-dev
```

### Terraform
```bash
terraform plan -var-file=environments/dev.tfvars   # Review changes
terraform apply -target=module.database -var-file=environments/dev.tfvars  # Targeted rollback
```
