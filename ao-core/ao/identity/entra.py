"""Entra ID helpers — OBO token exchange, Managed Identity credential.

Provides two credential flows:
- ServiceIdentity: DefaultAzureCredential (Managed Identity in Azure, CLI locally)
- UserDelegated: OBO flow — exchange user's bearer token for downstream access token

Token cache
-----------
get_token() caches the acquired token in-process until 60 seconds before its
expiry so that every tool call does not incur a round-trip to Entra.  The cache
key is (identity_mode, managed_identity_client_id|user_token_hash, scope).
azure-identity's own credential objects also cache internally, but the outer
cache here avoids even constructing the credential or awaiting get_token() on
successive calls within the same token lifetime.
"""

import hashlib
import logging
import time
from typing import NamedTuple

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential, OnBehalfOfCredential

from ao.identity.context import IdentityContext, IdentityMode

logger = logging.getLogger(__name__)

_TOKEN_EXPIRY_BUFFER_SECS = 60  # refresh this many seconds before actual expiry


class _CacheEntry(NamedTuple):
    token: str
    expires_at: float  # unix timestamp


class EntraCredentialProvider:
    """Resolves Azure credentials based on IdentityContext."""

    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self._client_id = client_id
        self._client_secret = client_secret
        # Keyed by (identity_mode.value, identity_key, scope)
        self._token_cache: dict[tuple[str, str, str], _CacheEntry] = {}

    async def get_token(self, identity: IdentityContext, scope: str) -> str:
        """Return a raw Bearer token string for the given identity and scope.

        Results are cached in-process until TOKEN_EXPIRY_BUFFER_SECS before
        expiry to avoid per-call round-trips to Entra.

        scope — the full resource scope URI, e.g.:
            Service   : "api://apim-ao-dev/.default"
            Delegated : "api://apim-ao-dev/.default"  (same; OBO handles the exchange)
        """
        cache_key = self._cache_key(identity, scope)
        entry = self._token_cache.get(cache_key)
        if entry and time.time() < entry.expires_at:
            return entry.token

        credential = self.get_credential(identity)

        # azure-identity credentials are synchronous; run in executor to avoid
        # blocking the event loop on network I/O.
        import asyncio
        loop = asyncio.get_event_loop()
        token_obj = await loop.run_in_executor(
            None, lambda: credential.get_token(scope)
        )

        expires_at = token_obj.expires_on - _TOKEN_EXPIRY_BUFFER_SECS
        self._token_cache[cache_key] = _CacheEntry(token=token_obj.token, expires_at=expires_at)
        logger.debug(
            "Acquired token for scope=%s mode=%s expires_in=%.0fs",
            scope,
            identity.mode.value,
            token_obj.expires_on - time.time(),
        )
        return token_obj.token

    def get_credential(self, identity: IdentityContext):
        """Return an Azure TokenCredential for the given identity context."""
        if identity.mode == IdentityMode.SERVICE:
            return self._get_service_credential(identity)
        elif identity.mode == IdentityMode.USER_DELEGATED:
            return self._get_obo_credential(identity)
        raise ValueError(f"Unknown identity mode: {identity.mode}")

    # ── Internal ──────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(identity: IdentityContext, scope: str) -> tuple[str, str, str]:
        """Stable cache key that does not store the raw user token."""
        if identity.mode == IdentityMode.SERVICE:
            identity_key = identity.managed_identity_client_id or "__default__"
        else:
            # Hash the user token so the raw JWT is never kept in the cache dict key
            raw = identity.user_token or ""
            identity_key = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return (identity.mode.value, identity_key, scope)

    def _get_service_credential(self, identity: IdentityContext):
        mid = identity.managed_identity_client_id or self._client_id
        if mid:
            # Use a targeted UAMI; ManagedIdentityCredential is lighter than
            # DefaultAzureCredential when the client_id is already known.
            return ManagedIdentityCredential(client_id=mid)
        # No specific UAMI → fall back to system-assigned MI or Azure CLI locally
        return DefaultAzureCredential()

    def _get_obo_credential(self, identity: IdentityContext):
        if not identity.user_token:
            raise ValueError("UserDelegated mode requires user_token")
        if not self._client_id or not self._client_secret:
            raise ValueError("OBO flow requires client_id and client_secret")
        return OnBehalfOfCredential(
            tenant_id=identity.tenant_id,
            client_id=self._client_id,
            client_secret=self._client_secret,
            user_assertion=identity.user_token,
        )