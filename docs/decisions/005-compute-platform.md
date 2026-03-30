# ADR-005: Compute Platform — ACA Default with AKS Toggle

## Status

Accepted

## Date

2026-03-30

## Context

The AO layer needs a compute platform to host the FastAPI API and background workers. Two Azure options were evaluated:

| Aspect | Azure Container Apps (ACA) | Azure Kubernetes Service (AKS) |
|---|---|---|
| Cost (dev/trial) | ~$0–5/month (scales to zero) | ~$70–150/month (always-on node pool) |
| Ops overhead | Near-zero (serverless) | Moderate (node upgrades, RBAC, networking) |
| Scale-to-zero | Yes (built-in) | No (cluster always running) |
| Company readiness | Not yet deployed | Already exists in the company |
| Flexibility | Simpler ingress, Dapr built-in | Full K8s: Helm, CRDs, service mesh |
| Langfuse hosting | Use Langfuse Cloud (free tier) | Self-host via Helm chart |

The team has a 30-day Azure free trial ($200 budget). AKS alone would consume 50–75% of that. However, the company already runs AKS in production, so a path to AKS must remain easy.

## Decision

**Default to ACA** for development and trial environments, with a **single Terraform variable toggle** (`compute_platform = "aca" | "aks"`) to switch to AKS when deploying on the company's existing cluster.

Both modules live side-by-side in `infra/modules/`. The root `main.tf` conditionally instantiates one or the other based on the toggle.

### What changes per toggle value

| Component | `compute_platform = "aca"` | `compute_platform = "aks"` |
|---|---|---|
| Compute | Container Apps Environment + 2 apps | AKS namespace + ACR pull role |
| Container Registry | ACR (Basic) | ACR (Basic+) |
| Langfuse | Langfuse Cloud (free tier, no infra) | Self-hosted on AKS via Helm |
| CI/CD deploy step | `az containerapp update` | `kubectl set image` |
| Ingress | ACA built-in HTTPS | K8s Ingress / Load Balancer |

## Consequences

- **Trial/dev**: ACA keeps costs under ~$35–40/month total, well within $200 budget.
- **Company migration**: Flip `compute_platform = "aks"` in tfvars + provide `aks_cluster_name`. No other module changes.
- **Two modules to maintain**: Marginal overhead — both are thin (ACA ~60 lines, AKS ~40 lines).
- **Langfuse strategy splits**: ACA uses Langfuse Cloud; AKS self-hosts. Both work identically from the SDK side (same env vars: `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`).
