# Azure Deployment Guide

How to deploy AO and DSAI apps to Azure infrastructure and validate end-to-end on real services.

---

## Prerequisites

| Tool | Purpose |
|---|---|
| Azure CLI (`az`) | Azure resource management |
| Terraform >= 1.5 | Infrastructure provisioning |
| kubectl | AKS cluster access |
| Helm | Langfuse deployment |
| Docker | Container image builds |
| GitHub repo access | CI/CD pipeline runs |

### Required Azure permissions

- **Contributor** on the resource group
- **User Access Administrator** for role assignments (managed identities)
- **Key Vault Administrator** for secret management
- **AKS Cluster Admin** for namespace/RBAC setup

---

## 1. Provision Infrastructure with Terraform

### First-time setup

```bash
cd infra

# Login to Azure
az login
az account set --subscription <subscription-id>

# Create backend storage for Terraform state (one-time)
az group create -n rg-ao-tfstate -l southeastasia
az storage account create -n aoterraformstate -g rg-ao-tfstate -l southeastasia --sku Standard_LRS
az storage container create -n tfstate --account-name aoterraformstate
```

### Deploy by environment

```bash
# Dev
terraform init -backend-config="storage_account_name=aoterraformstate" \
               -backend-config="container_name=tfstate" \
               -backend-config="key=ao-dev.tfstate"
terraform plan -var-file=environments/dev.tfvars -var="postgres_admin_password=<password>"
terraform apply -var-file=environments/dev.tfvars -var="postgres_admin_password=<password>"

# Staging
terraform init -backend-config="key=ao-staging.tfstate" -reconfigure
terraform plan -var-file=environments/staging.tfvars -var="postgres_admin_password=<password>"
terraform apply -var-file=environments/staging.tfvars -var="postgres_admin_password=<password>"

# Production
terraform init -backend-config="key=ao-prod.tfstate" -reconfigure
terraform plan -var-file=environments/prod.tfvars -var="postgres_admin_password=<password>"
terraform apply -var-file=environments/prod.tfvars -var="postgres_admin_password=<password>"
```

### Provisioning order

Terraform handles dependencies automatically, but for reference:

1. **security** — Key Vault + managed identities (everything depends on this)
2. **registry** — Azure Container Registry
3. **database** — PostgreSQL Flexible Server + pgvector + Redis
4. **messaging** — Azure Service Bus (dead-letter + workflow events)
5. **ai** — Azure OpenAI (gpt-4o, gpt-4o-mini, text-embedding-3-large)
6. **observability** — Log Analytics + App Insights
7. **aks** — ACR pull role for existing cluster

### What Terraform creates per environment

| Resource | Dev | Staging | Prod |
|---|---|---|---|
| PostgreSQL | B_Standard_B1ms / 32 GB | B_Standard_B1ms / 32 GB | GP_Standard_D2s_v3 / 64 GB |
| Redis | Basic C0 | Basic C0 | Standard C1 |
| ACR | Basic | Basic | Premium |
| Azure OpenAI (gpt-4o) | 10 TPM | 10 TPM | 30 TPM |
| Azure OpenAI (gpt-4o-mini) | 20 TPM | 20 TPM | 60 TPM |
| Backup retention | 7 days | 7 days | 35 days (geo-redundant) |

---

## 2. Deploy Langfuse to AKS

Langfuse is self-hosted on AKS via Helm chart (not a Terraform resource).

```bash
# Get AKS credentials
az aks get-credentials --resource-group rg-ao-dev --name aks-dsai-dev

# Create AO namespace
kubectl create namespace ao-dev

# Add Langfuse Helm repo
helm repo add langfuse https://langfuse.github.io/langfuse-k8s
helm repo update

# Deploy Langfuse
helm install langfuse langfuse/langfuse \
  --namespace ao-dev \
  --set database.url="postgresql://aoadmin:<password>@psql-ao-dev.postgres.database.azure.com:5432/ao?sslmode=require" \
  --set nextauth.secret="<generate-a-secret>" \
  --set nextauth.url="https://langfuse-dev.<your-domain>" \
  --set ingress.enabled=true \
  --set ingress.hosts[0].host="langfuse-dev.<your-domain>"
```

### Post-deploy: configure Langfuse

1. Access Langfuse UI at the ingress URL
2. Create projects: `email-assistant`, `rag-search`, `graph-compliance`
3. Generate API keys per project
4. Store keys in Key Vault:
   ```bash
   az keyvault secret set --vault-name kv-ao-dev --name langfuse-email-public-key --value pk-lf-...
   az keyvault secret set --vault-name kv-ao-dev --name langfuse-email-secret-key --value sk-lf-...
   ```

---

## 3. Store Secrets in Key Vault

All sensitive configuration goes in Key Vault — never in code, env files, or Terraform output.

```bash
VAULT=kv-ao-dev

# Database
# (already stored by Terraform: ao-postgres-connection, ao-redis-connection)

# Azure OpenAI
az keyvault secret set --vault-name $VAULT --name azure-openai-api-key --value <key>

# Langfuse (per app)
az keyvault secret set --vault-name $VAULT --name langfuse-email-public-key --value <key>
az keyvault secret set --vault-name $VAULT --name langfuse-email-secret-key --value <key>

# Service Bus (if not using managed identity)
az keyvault secret set --vault-name $VAULT --name servicebus-connection-string --value <conn-str>

# App-specific secrets
az keyvault secret set --vault-name $VAULT --name ai-search-api-key --value <key>
az keyvault secret set --vault-name $VAULT --name neo4j-credentials --value <user:pass>
```

---

## 4. Build & Push Container Images

### Manual build (for debugging)

```bash
# Login to ACR
az acr login --name craodev

# Build and push
docker build -f docker/Dockerfile.ao-api -t craodev.azurecr.io/ao-api:latest .
docker push craodev.azurecr.io/ao-api:latest

docker build -f docker/Dockerfile.ao-worker -t craodev.azurecr.io/ao-worker:latest .
docker push craodev.azurecr.io/ao-worker:latest
```

### CI/CD build (normal flow)

Push to `main` → GitHub Actions builds, tests, and pushes images automatically. See `.github/workflows/ci.yml`.

---

## 5. Deploy AO Services to AKS

```bash
# Apply Kubernetes manifests
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
```

### Verify deployment

```bash
kubectl get pods -n ao-dev
kubectl logs deployment/ao-api -n ao-dev --tail=20
curl https://ao-api-dev.<your-domain>/health
```

---

## 6. Onboard a DSAI App

### Step-by-step for the app team

1. **Install the SDK** in their app repo:
   ```bash
   pip install ao-core    # From internal PyPI or Git
   ```

2. **Create `ao-manifest.yaml`** in their repo root (see examples in `examples/`):
   ```yaml
   app_id: my_app
   display_name: My DSAI App
   identity_mode: user_delegated
   llm_endpoint: https://aoai-ao-dev.openai.azure.com/
   llm_api_key_secret: azure-openai-api-key
   langfuse_project: my-app
   agents:
     - name: main_agent
       model: gpt-4o
       tools: [my_tool]
   tools:
     - name: my_tool
       type: api
       endpoint: https://internal-api.company.com/v1
       connection_secret: internal-api-key
   ```

3. **Store secrets** in Key Vault:
   ```bash
   az keyvault secret set --vault-name kv-ao-dev --name internal-api-key --value <key>
   ```

4. **Build the workflow** in their app code:
   ```python
   from ao.config.manifest import AppManifest
   from ao.engine.patterns.router import build_router
   from ao.llm.azure_openai import AzureOpenAIProvider

   manifest = AppManifest.from_yaml("ao-manifest.yaml")
   llm = AzureOpenAIProvider(
       endpoint=manifest.llm_endpoint,
       api_key=os.getenv("AZURE_OPENAI_API_KEY"),
   )
   # Build and run workflows using AO patterns...
   ```

5. **Verify in Langfuse** — check that traces appear in their project.

---

## 7. End-to-End Validation Checklist

After deployment, verify each component works:

| # | Check | Command / Action |
|---|---|---|
| 1 | AO API health | `curl https://ao-api-dev.<domain>/health` |
| 2 | PostgreSQL connectivity | `kubectl exec -it <ao-api-pod> -- python -c "import psycopg; ..."` |
| 3 | Redis connectivity | `kubectl exec -it <ao-api-pod> -- python -c "import redis; ..."` |
| 4 | Azure OpenAI | Run a simple completion from inside the cluster |
| 5 | Langfuse traces | Run a demo workflow, check traces appear in Langfuse UI |
| 6 | HITL flow | Submit an approval, verify it appears in `/api/hitl/pending` |
| 7 | Policy enforcement | Send PII-containing input, verify it's redacted/blocked |
| 8 | Service Bus | Trigger a dead-letter scenario, verify message lands in DLQ |

---

## 8. Environment Comparison

| Aspect | Local (Docker Compose) | Dev (Azure) | Staging | Prod |
|---|---|---|---|---|
| LLM | Ollama (local) | Azure OpenAI | Azure OpenAI | Azure OpenAI |
| Database | pgvector:pg16 container | PG Flexible Server (Basic) | PG Flexible (Basic) | PG Flexible (GP) |
| Redis | redis:7-alpine container | Azure Cache (Basic) | Azure Cache (Basic) | Azure Cache (Standard) |
| Langfuse | Container | Helm on AKS | Helm on AKS | Helm on AKS |
| HITL | Auto-approve | Configurable | Required in sensitive steps | Required |
| Identity | None / mock | Managed Identity | Managed Identity | Managed Identity + OBO |
| Observability | Console logs | App Insights + Langfuse | App Insights + Langfuse | App Insights + Langfuse |

---

## CI/CD Pipeline Flow

```
Push to main
    ↓
┌─────────┐
│  Lint   │ (ruff check + format)
└────┬────┘
     ↓
┌──────────┐
│  Unit    │ (51 tests)
│  Tests   │
└────┬─────┘
     ↓
┌──────────┬──────────┬──────────┐
│ Integr.  │  Eval    │ Security │  (parallel)
│ Tests    │  Tests   │ Tests    │
└────┬─────┴────┬─────┴────┬─────┘
     └──────────┼──────────┘
                ↓
        ┌──────────────┐
        │ Build & Push │ (Docker → ACR)
        └──────┬───────┘
               ↓
        ┌──────────────┐
        │   Staging    │ (deploy + smoke test)
        └──────┬───────┘
               ↓
        ┌──────────────┐
        │  Production  │ (manual gate)
        └──────────────┘
```

GitHub Actions workflow: `.github/workflows/ci.yml`

---

## Rollback

```bash
# Roll back to previous deployment
kubectl rollout undo deployment/ao-api -n ao-<env>

# Roll back Terraform
terraform plan -var-file=environments/<env>.tfvars  # Review what will change
# If needed, target specific resources
terraform apply -target=module.database -var-file=environments/<env>.tfvars
```
