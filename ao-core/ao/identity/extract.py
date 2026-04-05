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

    # ── EasyAuth (no token store): ACA sidecar injects principal headers ─
    # When the Token Store is not configured, ACA EasyAuth injects
    # X-MS-CLIENT-PRINCIPAL (base64 JSON) and X-MS-CLIENT-PRINCIPAL-NAME
    # (email) but does NOT inject X-MS-TOKEN-AAD-ACCESS-TOKEN.
    if not access_token:
        principal_claims = _decode_client_principal(request)
        if principal_claims:
            tenant_id = principal_claims.get("tid", "")
            logger.debug(
                "Extracted user identity (principal header) sub=%s tid=%s",
                principal_claims.get("sub", "unknown"),
                tenant_id,
            )
            return IdentityContext(
                mode=IdentityMode.USER_DELEGATED,
                tenant_id=tenant_id,
                claims=principal_claims,
            )

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


def _decode_client_principal(request) -> dict:
    """Decode the X-MS-CLIENT-PRINCIPAL header injected by ACA EasyAuth.

    The header is a base64-encoded JSON object whose ``claims`` array uses
    WS-Federation ``typ``/``val`` pairs.  We normalise them into a flat dict
    that mirrors the JWT claims shape expected by the rest of the codebase.

    Returns an empty dict when the header is absent or cannot be decoded.
    """
    raw = request.headers.get("X-MS-CLIENT-PRINCIPAL", "")
    if not raw:
        return {}
    try:
        raw += "=" * ((4 - len(raw) % 4) % 4)
        principal = json.loads(base64.b64decode(raw).decode("utf-8"))
        flat: dict = {}
        for claim in principal.get("claims", []):
            typ = claim.get("typ", "")
            val = claim.get("val", "")
            # Map WS-Fed long URN types to short names where useful
            if typ == "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier":
                flat.setdefault("sub", val)
            elif typ == "http://schemas.microsoft.com/identity/claims/objectidentifier":
                flat.setdefault("sub", val)
            elif typ == "http://schemas.microsoft.com/identity/claims/tenantid":
                flat.setdefault("tid", val)
            elif typ == "preferred_username":
                flat.setdefault("preferred_username", val)
            elif typ == "name":
                flat.setdefault("name", val)
            elif typ == "oid":
                flat.setdefault("sub", val)
            elif typ == "tid":
                flat.setdefault("tid", val)
            else:
                # Keep short-name claims as-is
                if "/" not in typ:
                    flat[typ] = val
        # Fallback: X-MS-CLIENT-PRINCIPAL-NAME is the email/UPN
        if "preferred_username" not in flat:
            upn = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "")
            if upn:
                flat["preferred_username"] = upn
        if "sub" not in flat:
            oid = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID", "")
            if oid:
                flat["sub"] = oid
        return flat
    except Exception as exc:
        logger.debug("Could not decode X-MS-CLIENT-PRINCIPAL (non-critical): %s", exc)
        return {}


def get_display_name(identity: IdentityContext | None) -> tuple[str, str]:
    """Return ``(display_name, email)`` for the given identity.

    Falls back to empty strings for unauthenticated (system) identities.
    """
    if identity is None or not identity.claims:
        return "", ""
    claims = identity.claims
    name = claims.get("name", "")
    email = claims.get("preferred_username", claims.get("email", ""))
    if not name:
        name = email.split("@")[0] if "@" in email else email
    return name, email
