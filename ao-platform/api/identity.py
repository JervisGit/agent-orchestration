"""FastAPI identity dependency — extract IdentityContext from the inbound request.

How this fits in the architecture
----------------------------------
APIM validates the JWT signature and enforces App Role claims before the request
reaches ao-platform.  By the time we see the request here, the token is already
trusted.  This module therefore only *parses* (does not re-validate) the token to
extract the claims we need to build an IdentityContext.

For USER_DELEGATED mode the raw token is preserved so that EntraCredentialProvider
can perform the OBO exchange when a tool call needs a downstream token.

Usage in a route
----------------
    from api.identity import get_identity_context

    @router.post("/run")
    async def run(body: RunRequest, identity: IdentityContext = Depends(get_identity_context)):
        state["_identity"] = identity
        ...

Environment variables
---------------------
AO_TENANT_ID             — Entra tenant ID (used as fallback when not in token claims)
AO_CLIENT_ID             — AO Platform app registration client ID (required for OBO)
AO_CLIENT_SECRET         — AO Platform app registration client secret (required for OBO)
APIM_GATEWAY_URL         — set by Terraform only when enable_apim = true; presence used
                            to decide whether to expect delegated tokens
"""

import base64
import json
import logging
import os

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ao.identity.context import IdentityContext, IdentityMode

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

_TENANT_ID = os.getenv("AO_TENANT_ID", "")
_APIM_ENABLED = bool(os.getenv("APIM_GATEWAY_URL", ""))


def _parse_jwt_claims(token: str) -> dict:
    """Decode JWT payload without verifying the signature.

    Signature verification is APIM's responsibility.  We only need the
    claims (tid, oid, preferred_username, roles) to build IdentityContext.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        # Add padding so base64 decodes cleanly regardless of token length
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _identity_from_claims(claims: dict, raw_token: str) -> IdentityContext:
    """Build an IdentityContext from parsed JWT claims."""
    tenant_id = claims.get("tid", _TENANT_ID)

    # scp/scope claim is present in delegated tokens; absent in app-only (service) tokens
    has_delegated_scope = bool(claims.get("scp") or claims.get("scope"))
    # idtyp="app" explicitly marks client-credential / managed-identity tokens
    is_app_token = claims.get("idtyp") == "app" or (not has_delegated_scope and not claims.get("upn"))

    if is_app_token:
        return IdentityContext(
            mode=IdentityMode.SERVICE,
            tenant_id=tenant_id,
            managed_identity_client_id=claims.get("appid") or claims.get("azp"),
            claims=claims,
        )
    else:
        return IdentityContext(
            mode=IdentityMode.USER_DELEGATED,
            tenant_id=tenant_id,
            user_token=raw_token,
            claims=claims,
        )


async def get_identity_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> IdentityContext:
    """FastAPI dependency — parse the inbound Bearer token into an IdentityContext.

    When APIM is not yet enabled (APIM_GATEWAY_URL unset), this dependency falls
    back to a SERVICE context using the AO_TENANT_ID env var so existing routes
    continue to work during the transition period.
    """
    if not credentials or not credentials.credentials:
        if not _APIM_ENABLED:
            # Pre-APIM: return a no-op service context so existing code is unaffected
            return IdentityContext(
                mode=IdentityMode.SERVICE,
                tenant_id=_TENANT_ID,
            )
        raise HTTPException(status_code=401, detail="Authorization header required")

    token = credentials.credentials
    claims = _parse_jwt_claims(token)
    if not claims:
        raise HTTPException(status_code=401, detail="Malformed authorization token")

    identity = _identity_from_claims(claims, raw_token=token)
    logger.debug(
        "Resolved identity: mode=%s oid=%s",
        identity.mode.value,
        claims.get("oid", "—"),
    )
    return identity


async def get_identity_context_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> IdentityContext | None:
    """Same as get_identity_context but returns None instead of raising 401.

    Use on endpoints that want to work with or without an identity (e.g. health check).
    """
    if not credentials or not credentials.credentials:
        return None
    try:
        return await get_identity_context(request, credentials)
    except HTTPException:
        return None
