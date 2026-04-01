# ADR-008: Manifest Ownership — App Repo vs. AO Platform

**Date:** 2026-04-01  
**Status:** Accepted  
**Deciders:** AO team

---

## Context

`ao-manifest.yaml` declares an app's agents, SOPs, routing patterns, tool access, and
HITL conditions. It is the primary domain-specific input to `ManifestExecutor`.

Two ownership models are possible:

**Model A — App repo owns the manifest** (current)  
Each DSAI app ships `ao-manifest.yaml` alongside its application code. The file is read
from disk at startup. Deployment of manifest changes requires a container redeploy.

**Model B — AO Platform owns the manifest**  
Manifests are stored and versioned in the AO Platform DB. Apps call
`GET /api/apps/{app_id}/manifest` at startup to fetch their configuration.
The AO team (or dashboard operators) can update an agent's SOP or HITL condition
without touching the app's repo or triggering a redeploy.

---

## Decision

**Model A — App repo owns the manifest.**

---

## Rationale

| Concern | Model A (app repo) | Model B (platform DB) |
|---|---|---|
| Source of truth | Git — auditable, reviewable, rollback is `git revert` | Platform DB — changes happen out-of-band from code |
| Change accountability | App team owns changes; PR review enforced | AO team becomes a dependency; drift from code |
| Incident blast radius | Broken manifest fails one app's deploy | Broken platform API blocks all app startups |
| Compliance / audit | Changes traceable to commits and authors | Requires separate audit trail on DB mutations |
| Operational burden | Manifest change = container redeploy (~2 min in ACA) | Faster hotfix, but bypasses CI/CD gates |

The strongest objection to Model B is **accountability drift**: if an agent's SOP is
edited in the dashboard and subsequently causes incorrect outputs, the responsible party
is ambiguous — the AO team stored and served the config, but the app team owns the
business logic. Git-based ownership keeps that boundary clean.

The AO Platform's `POST /api/apps/{app_id}/manifest` endpoint (Phase 4) is retained as
a **read-only registration surface** — apps POST their manifest at startup so the
platform can index agents and tools for the dashboard. The platform does not serve the
manifest back to apps. It is a mirror, not the source of truth.

---

## Consequences

- App teams manage `ao-manifest.yaml` in their own repositories.
- Manifest changes go through normal PR review and trigger a container redeploy.
- At startup, each app POSTs its parsed manifest to the AO Platform to keep the
  dashboard's Apps tab current (best-effort; failure does not block startup).
- The AO Platform may not always be in sync with the latest manifest if the app has
  been restarted without network access to the platform — this is acceptable.
- If a future requirement demands live SOP editing without redeployment (e.g. rapid
  response to regulatory guidance), revisit Model B with an explicit audit trail and
  a signed-manifest verification mechanism.

---

## Alternatives Considered

**Hybrid: Git-backed manifest served via platform**  
Store the manifest in Git but have the platform serve it via a GitOps sync (e.g.
pull from a known branch on a schedule). Rejected: adds infrastructure complexity
without meaningfully improving the audit trail over direct file reads.
