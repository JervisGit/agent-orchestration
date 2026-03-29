"""Entra ID helpers — OBO token exchange, Managed Identity credential.

Provides two credential flows:
- ServiceIdentity: DefaultAzureCredential (Managed Identity in Azure, CLI locally)
- UserDelegated: OBO flow — exchange user's bearer token for downstream access token
"""

import logging

from azure.identity import DefaultAzureCredential, OnBehalfOfCredential

from ao.identity.context import IdentityContext, IdentityMode

logger = logging.getLogger(__name__)


class EntraCredentialProvider:
    """Resolves Azure credentials based on IdentityContext."""

    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self._client_id = client_id
        self._client_secret = client_secret
        self._default_credential: DefaultAzureCredential | None = None

    def get_credential(self, identity: IdentityContext):
        """Return an Azure TokenCredential for the given identity context."""
        if identity.mode == IdentityMode.SERVICE:
            return self._get_service_credential(identity)
        elif identity.mode == IdentityMode.USER_DELEGATED:
            return self._get_obo_credential(identity)
        raise ValueError(f"Unknown identity mode: {identity.mode}")

    def _get_service_credential(self, identity: IdentityContext):
        if self._default_credential is None:
            kwargs = {}
            mid = identity.managed_identity_client_id or self._client_id
            if mid:
                kwargs["managed_identity_client_id"] = mid
            self._default_credential = DefaultAzureCredential(**kwargs)
        return self._default_credential

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