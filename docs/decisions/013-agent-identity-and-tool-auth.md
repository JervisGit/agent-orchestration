# ADR-013: Agent Identity and Tool Authentication via Azure APIM App Roles

## Status
Accepted (dev topology deployed; per-agent UAMI isolation deferred to production hardening)

## Context

Agents in the AO platform call external tools — initially a taxpayer database — with no
authentication at all. The email assistant's `_tool_lookup_taxpayer` issued a direct
`psycopg` query without any identity context. This creates several risks:

1. **No authorisation boundary** — any code path in the container can query the database.
2. **No audit trail** — there is no record of which agent accessed which record, or when.
3. **No blast-radius containment** — a compromised agent container has unrestricted access
   to all tools, not just the ones it legitimately needs.
4. **Compute coupling** — any auth scheme must work identically whether agents run in ACA
   or AKS; managed identity mechanics differ between the two.

The question is: how should agents acquire identity and prove authorisation when calling
tools, in a way that is enforceable at the platform layer and not merely at the code layer?

## Decision

Use **Azure API Management (APIM) as a universal identity gateway** with **Entra App Roles**
as the permission model.

### Core design

```
Agent (ACA or AKS)
  │
  │  ManagedIdentityCredential(client_id=uami).get_token(scope)
  │                        [IMDS — Azure platform, not code]
  ▼
Entra ID  ──→  JWT with roles: ["Agents.TaxpayerLookup"]
  │
  ▼
APIM  ──→  Step 1: validate-jwt (audience check → 401 if invalid)
  │        Step 2: required-claims (role check → 403 if missing)
  │
  ▼
Backend API (FastAPI /taxpayer/{tin})
```

APIM validates every inbound call before it reaches any backend. No backend endpoint
performs its own JWT re-validation — by the time a request arrives at the FastAPI
endpoint, APIM has already enforced both the audience and the role claim.

### Why APIM rather than direct service-to-service auth

APIM is the stable layer. Whether agents run in ACA (IMDS `ManagedIdentityCredential`)
or AKS (Workload Identity / federated credential), the token acquisition mechanism
differs but the APIM policy is **identical in both cases**. Switching compute platform
requires no changes to this module.

### Why App Roles rather than per-agent UAMI per scope

Considered two models:

| Model | How it works | Problem |
|---|---|---|
| **UAMI-per-agent** | Each agent has its own UAMI; backend checks which UAMI made the call | ACA and AKS cannot enforce at the platform level which UAMI a container *uses* — only which UAMIs are *attached* to it. A container with two attached UAMIs can acquire a token for either. Isolation requires one UAMI per container, which requires one container per agent. |
| **App Roles** | One Entra app registration owns role definitions; UAMIs are assigned only the roles they need | Role check is enforced by Entra (token issuance) and APIM (claim inspection) — both platform services, not code. Adding a new permission scope = add a role, no new UAMI. |

App Roles provide the right enforcement point at the current deployment topology (all
agents in one container) while fully supporting true per-agent isolation later.

### Two-layer APIM policy

```xml
<!-- Layer 1 — API-level policy (every operation) -->
<validate-jwt failed-validation-httpcode="401">
  <audiences>
    <audience>{{apim-audience}}</audience>   <!-- identifier URI (v1 / OBO tokens) -->
    <audience>{{apim-client-id}}</audience>  <!-- GUID (v2 client-credentials tokens) -->
  </audiences>
</validate-jwt>

<!-- Layer 2 — Operation-level policy (per tool) -->
<validate-jwt failed-validation-httpcode="403">
  <required-claims>
    <claim name="roles" match="any">
      <value>Agents.TaxpayerLookup</value>
    </claim>
  </required-claims>
</validate-jwt>
```

Layer 1 rejects unauthenticated callers (401). Layer 2 rejects authenticated callers
that lack the specific role for that operation (403). The two audiences handle the v1/v2
token format difference: v1 tokens carry the identifier URI as `aud`; v2
client-credentials and managed identity tokens carry the GUID client_id.

### Current (dev) topology — shared UAMI

```
ca-email-assistant-dev
├── UAMI: id-ao-api-dev  ← assigned all 3 App Roles
├── agent: filing_extension    }
├── agent: assessment_relief   }  same process, same token
└── agent: penalty_waiver      }
```

The single UAMI holds all roles. Every agent's APIM call passes role inspection because
the token contains all roles. This is correct for dev — it reduces infrastructure
complexity while the enforcement mechanism is being validated.

**This is not true agent isolation.** A bug in `filing_extension` could call the
`assessment_relief` APIM endpoint and succeed, because both share the same token.

### Production hardening path — per-agent UAMI isolation

The path to true isolation differs by compute platform:

**ACA** — isolation boundary is the container app. IMDS only vends a token for a UAMI
that is attached to *that specific container app*. Therefore true per-agent isolation
on ACA requires one container app per agent.

**AKS (recommended for production isolation)** — isolation boundary is the pod.
Kubernetes Workload Identity binds a service account to a UAMI via a federated
credential. Each pod's projected service account token is exchanged for an Azure AD
token scoped to the UAMI assigned to that pod's service account — regardless of what
other pods run on the same node or in the same cluster. Multiple agents can coexist in
the same cluster with fully independent identities, with no topology split required.

AKS Workload Identity per-agent flow:

```
Pod (filing_extension)
  serviceAccountName: sa-filing-ext          ← K8s service account
  labels:
    azure.workload.identity/use: "true"

Federated credential (Terraform):
  azuread_service_principal: id-filing-ext   ← UAMI service principal
  subject: system:serviceaccount:agents:sa-filing-ext
  issuer: <AKS OIDC issuer URL>

At runtime:
  WorkloadIdentityCredential().get_token(scope)
  → exchanges projected SA token for JWT with roles: ["FilingExtensionRead"] only
  → APIM role check passes / fails based on that specific token
```

This is why APIM is the right enforcement layer: it is identical in both cases. The
token acquisition path changes (IMDS vs Workload Identity), but the `validate-jwt`
policy and the `roles` claim check are untouched.

The hardened per-agent topology for either platform:

```
ca-filing-extension          ca-assessment-relief
├── UAMI: id-filing-ext      ├── UAMI: id-assessment
│   └── FilingExtensionRead  │   └── AssessmentRelief
│                            │   └── TaxReliefRead
```

`id-filing-ext`'s token contains only `FilingExtensionRead`. If `filing_extension` tried
to call the assessment endpoint, APIM returns the **same 403** — the `required-claims`
check fails because the claim is absent from the token. This is enforced by:

1. **Entra (token issuer)** — only grants claims for roles that are assigned to the
   requesting service principal. A UAMI that was never assigned `AssessmentRelief`
   cannot obtain a token containing that claim.
2. **IMDS (platform)** — only returns a token for a UAMI that is attached to the
   container app. Code cannot acquire a token for a foreign UAMI.
3. **APIM (gateway)** — inspects the claim at call time. Zero trust of the backend.

No changes to APIM policy or the Entra app registration are required for this migration.
The App Role definitions and `validate-jwt` policies are already written for it.

### Migration path (when per-agent isolation is required)

Steps 1, 2, and 4 are identical regardless of compute platform. Step 3 differs:

1. Add per-agent UAMIs in `infra/modules/security/main.tf`
2. Replace the blanket `azuread_app_role_assignment.ao_api` in `infra/modules/apim/main.tf`
   with targeted assignments (one per UAMI × role)
3. **ACA path** — split into one container app per agent in `infra/modules/aca/main.tf`;
   attach only that agent's UAMI to its container app
   **AKS path** — create one Kubernetes service account per agent; create a federated
   credential in `infra/modules/aks/main.tf` binding each service account to its UAMI;
   set `azure.workload.identity/use: "true"` on each agent pod spec
4. Set `identity_client_id` per agent in `ao-manifest.yaml` — `ManifestExecutor` already
   reads this field and builds a `ManagedIdentityCredential` with the specific client ID

No changes to APIM policies, the Entra app registration, or application code in either path.

### Python implementation

```
ao-core/ao/identity/
  context.py     — IdentityContext (SERVICE | USER_DELEGATED), IdentityMode
  entra.py       — EntraCredentialProvider.get_token(identity, scope) with 60s token cache

ao-core/ao/tools/
  executor.py    — injects identity into tool fn only when the fn declares the parameter
                   (backward compatible; existing tools unchanged)
  registry.py    — ToolSpec.required_identity: IdentityMode | None

ao-core/ao/engine/
  manifest_executor.py  — _resolve_identity(): agent identity_client_id > state identity
  
ao-platform/api/
  identity.py    — FastAPI dependency: parses JWT claims from APIM-forwarded request
                   (no re-validation; APIM already verified the token)
```

Tool functions opt into identity by declaring the parameter:

```python
async def _tool_lookup_taxpayer(tin: str, identity: IdentityContext | None = None):
    token = await credential_provider.get_token(identity, APIM_SCOPE)
    # call APIM with Bearer token
```

Tools that do not declare `identity` are called unchanged. This is backward compatible.

### APIM_TAXPAYER_URL decoupling

The APIM operation path (`/agents/taxpayer`) is injected via the `APIM_TAXPAYER_URL`
environment variable, assembled in Terraform as `{gateway_url}/agents/taxpayer` and
passed to the ACA container. App code has no knowledge of APIM's path structure; it
builds `f"{APIM_TAXPAYER_URL}/{tin}"`. If the APIM path changes, only Terraform changes.

## Alternatives Considered

| Option | Verdict |
|---|---|
| Direct service-to-service with network rules (VNet only) | Rejected: network policy is not portable across ACA/AKS without significant topology differences; no per-operation enforcement |
| Per-agent UAMI as the only identity model | Rejected for current phase: requires one container per agent today; no intermediate enforcement while topology is shared |
| OAuth2 scopes (not App Roles) | Rejected: scopes require user-delegated consent flows; App Roles (`roles` claim) work for application (service) identities with no user interaction |
| mTLS between agent and backend | Rejected: certificate lifecycle complexity; does not integrate with Entra audit logs |
| API key per agent | Rejected: static secrets, no expiry, no audit trail tied to identity |

## Consequences

### Positive
- **Compute-agnostic** — APIM policy is identical for ACA and AKS; no changes when switching
- **Audit trail** — every APIM call is logged to App Insights (logger + diagnostic wired) with caller `oid`, operation, latency, and status code
- **Zero code in the enforcement path** — Entra and APIM enforce claims; no custom middleware
- **Incremental isolation** — dev uses shared UAMI (simple); production adds per-agent UAMIs (one Terraform change per agent, no policy changes)
- **Testable** — the no-role test SP confirms 403 enforcement in CI without needing a real container

### Negative / trade-offs
- **APIM Consumption SKU** has no built-in developer portal or request history UI; App Insights integration is required to see request logs (now wired)
- **Token cache must be managed in process** — `EntraCredentialProvider` caches tokens with a 60s safety buffer; a cache miss adds ~200ms latency to the first tool call per agent invocation
- **Shared UAMI in dev is not true isolation** — accepted consciously; documented in migration path above

## References
- [infra/modules/apim/main.tf](../../infra/modules/apim/main.tf) — full APIM Terraform module
- [ao-core/ao/identity/entra.py](../../ao-core/ao/identity/entra.py) — credential provider
- [ao-core/ao/tools/executor.py](../../ao-core/ao/tools/executor.py) — identity injection
- [ao-core/ao/engine/manifest_executor.py](../../ao-core/ao/engine/manifest_executor.py) — `_resolve_identity()`
- [ao-platform/api/identity.py](../../ao-platform/api/identity.py) — FastAPI identity dependency
- [tests/unit/test_tools.py](../../tests/unit/test_tools.py) — `TestToolExecutorIdentity` suite

---

## Addendum: Logical Identity via APIM Named Values (`X-Agent-ID`)

### Problem

In organisations where provisioning a new Azure UAMI or App Registration requires a long approval process, the per-agent UAMI isolation model (described above) creates a barrier to onboarding new agents. An alternative that provides **per-agent access control without new Azure identities** is feasible using APIM's policy engine.

### Pattern — "X-Agent-ID" Virtual Identity

The Container App retains its single approved UAMI. Each sub-agent in `ManifestExecutor` attaches a `X-Agent-ID` custom header containing the agent's manifest name when making outbound APIM calls. APIM enforces a permissions map stored as a **Named Value**.

```
Agent Pod (ACA)
  └─ ManifestExecutor (agent_name="Researcher", from manifest)
       └─ HTTP POST /search
          X-Agent-ID: Researcher          ← injected by ManifestExecutor, never from user input
          Authorization: Bearer <UAMI token>

APIM
  ├─ validate-jwt (Layer 1: verify UAMI token — unchanged from ADR-013)
  └─ X-Agent-ID policy (Layer 2: check agent against Named Value map)
       AgentPermissions Named Value:
         { "Researcher": ["/search", "/read-docs"],
           "Writer":     ["/publish"],
           "Coder":      ["/git-push", "/debug"] }
       → 403 if agent not in map or path not in allowed list
```

### APIM policy fragment

```xml
<inbound>
    <base />
    <!-- Layer 1: JWT validation (unchanged) -->
    <validate-jwt failed-validation-httpcode="401">
      <audiences><audience>{{apim-client-id}}</audience></audiences>
    </validate-jwt>

    <!-- Layer 2: Logical agent identity -->
    <set-variable name="agentId"
                  value="@(context.Request.Headers.GetValueOrDefault("X-Agent-ID","Unknown"))" />
    <set-variable name="permissions"
                  value="@(JObject.Parse("{{AgentPermissions}}"))" />
    <choose>
        <when condition="@{
            var id    = (string)context.Variables["agentId"];
            var perms = (JObject)context.Variables["permissions"];
            var path  = context.Request.Url.Path.ToLower();
            if (perms.ContainsKey(id)) {
                return !perms[id].ToObject<List<string>>()
                                 .Any(p => path.StartsWith(p.ToLower()));
            }
            return true; // unknown agent → block
        }">
            <return-response>
                <set-status code="403" reason="Forbidden" />
                <set-body>@("Access denied for Agent: " + (string)context.Variables["agentId"])</set-body>
            </return-response>
        </when>
    </choose>
</inbound>
```

The `AgentPermissions` Named Value is a plain JSON string maintained in APIM. Adding a new agent = update one Named Value — no Terraform, no Azure identity provisioning.

### Is the `X-Agent-ID` header deterministic?

Yes, **provided it is always set by `ManifestExecutor` from the manifest definition, not from user input.**

In the current codebase, all outbound APIM calls originate from tool functions called by `ManifestExecutor`. The agent name is resolved from the manifest step before execution (same `_resolve_identity()` path). `ManifestExecutor` will inject `X-Agent-ID` from `self._manifest.name` (or the active step's agent name) at the HTTP call layer.

**The header MUST never be derived from the incoming chat request or any user-supplied data.** If a user sends `X-Agent-ID: Admin` in their chat message, it must never propagate to the APIM call. `ManifestExecutor` owns the header; user input is never in the header-setting path.

### Comparison with UAMI-per-agent model

| Aspect | UAMI-per-agent (ADR-013 production path) | X-Agent-ID Logical Identity |
|---|---|---|
| Cryptographic proof | Yes — JWT signed by Entra | No — header set by code |
| New Azure IDs needed | Yes (one UAMI per agent) | No |
| Approval required | Yes (Director/security team) | No — Named Value update |
| Audit trail | Entra logs + APIM (tied to real identity) | APIM logs (tied to header value) |
| Forgeable by insider threat? | No (IMDS token is platform-bound) | Only via code modification |
| Best for | Production, regulated, high-assurance | Dev, constrained orgs, rapid iteration |

### Recommendation

Use **X-Agent-ID + Named Value** as the enforcement mechanism when UAMI provisioning is blocked by approval processes. This is not a replacement for the ADR-013 UAMI model — it is a pragmatic intermediate that delivers per-agent access control today with no infrastructure changes. When per-UAMI isolation is approved, migrate to the production path described above; the two layers are additive (APIM can enforce both simultaneously).

All tool calls that bypass APIM (e.g. direct DB queries) are not covered by this pattern — all agent resource access must route through APIM for the logical identity check to be effective.

### Estimated implementation scope

Given the current codebase, the changes are small and follow the **same `identity` injection pattern** already in `ToolExecutor.execute()`:

| File | Change | ~Lines |
|---|---|---|
| `ao-core/ao/tools/executor.py` | Add `agent_name: str \| None = None` param to `execute()`; inject it when tool declares the parameter (same 3-line pattern as `identity`) | ~5 |
| `ao-core/ao/engine/manifest_executor.py` | In `_execute_tool_call()`, resolve agent name from the active step/manifest and pass to `ToolExecutor.execute()` | ~4 |
| `examples/email_assistant/backend/app.py` | Add `agent_name: str \| None = None` to `_tool_lookup_taxpayer`; include `X-Agent-ID` header in the httpx call | ~4 |
| `infra/modules/apim/main.tf` | Add `azurerm_api_management_named_value` block for `AgentPermissions` JSON; extend operation-level inbound policy with the X-Agent-ID choice block | ~35 |

**Total: ~15 lines of Python + ~35 lines of Terraform/APIM policy XML.** No new Azure resources, no schema changes, no new tests beyond the existing `TestToolExecutorIdentity` suite (add one parametrized case for `agent_name` injection).
