"""Extract user IdentityContext from FastAPI request headers.

Supports two sources:
1. EasyAuth (ACA built-in auth): injects X-MS-TOKEN-AAD-ACCESS-TOKEN (raw token)
   and X-MS-CLIENT-PRINCIPAL (base64 JSON of claims) when the token store is enabled.
2. Direct bearer token: Authorization: Bearer <token> — used in local dev and when
   the service is called directly (not via EasyAuth proxy).

When a token is found the USER_DELEGATED IdentityContext is returned.  The token
is stored as ``user_token`` so EntraCredentialProvider.get_token() can perform
the MSAL OBO exchange when a tool needs a downstream scoped token.

Usage in FastAPI endpoints::

    from fastapi import Request
    from ao.identity.extract import extract_identity

    @app.get("/api/search/stream")
    async def search_stream(q: str, request: Request):
        identity = extract_identity(request)
        state = {"input": q, "_identity": identity, ...}
        ...
"""

import base64
import json
import logging

from ao.identity.context import IdentityContext, IdentityMode

logger = logging.getLogger(__name__)


def extract_identity(request) -> IdentityContext:
    """Return a USER_DELEGATED IdentityContext from inbound request headers.

    Falls back to a SERVICE-mode system identity when no bearer token is present
    (e.g. internal health-check callers, local dev without auth).

    The returned context is always safe to use as ``_identity`` in graph state:
    tools that don't declare an ``identity`` parameter simply ignore it.
    """
    # ── EasyAuth: ACA token store injects raw access token ───────────
    access_token = request.headers.get("X-MS-TOKEN-AAD-ACCESS-TOKEN")

    # ── Direct: bearer token from Authorization header ────────────────
    if not access_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            access_token = auth_header[7:]

    if not access_token:
        # No token present — return a service-mode system identity.
        # Audit log entries will show user_id="system" for unauthenticated requests.
        return IdentityContext(
            mode=IdentityMode.SERVICE,
            tenant_id="",
            claims={"sub": "system"},
        )

    claims = _decode_jwt_claims(access_token)
    tenant_id = claims.get("tid", "")

    logger.debug(
        "Extracted user identity sub=%s tid=%s",
        claims.get("sub", "unknown"),
        tenant_id,
    )

    return IdentityContext(
        mode=IdentityMode.USER_DELEGATED,
        tenant_id=tenant_id,
        claims=claims,
        user_token=access_token,
    )


def get_user_id(identity: IdentityContext | None) -> str:
    """Return the canonical user identifier (OIDC ``sub`` claim) or 'system'."""
    if identity is None:
        return "system"
    if identity.claims:
        return identity.claims.get("sub", "system")
    return "system"


def _decode_jwt_claims(token: str) -> dict:
    """Decode the JWT payload segment without signature verification.

    Signature verification is already performed by EasyAuth / APIM upstream;
    we only need the claims for audit logging and keying user memory rows.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        # Restore base64 padding
        payload += "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:
        logger.debug("Could not decode JWT claims (non-critical): %s", exc)
        return {}
