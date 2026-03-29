"""Unit tests for identity context and Entra credential provider."""

import pytest

from ao.identity.context import IdentityContext, IdentityMode


class TestIdentityContext:
    def test_user_delegated_mode(self):
        ctx = IdentityContext(
            mode=IdentityMode.USER_DELEGATED,
            tenant_id="tenant-abc",
            user_token="obo-token-xyz",
        )
        assert ctx.mode == IdentityMode.USER_DELEGATED
        assert ctx.tenant_id == "tenant-abc"
        assert ctx.user_token == "obo-token-xyz"

    def test_service_identity_mode(self):
        ctx = IdentityContext(
            mode=IdentityMode.SERVICE,
            tenant_id="tenant-abc",
            managed_identity_client_id="mi-123",
        )
        assert ctx.mode == IdentityMode.SERVICE
        assert ctx.user_token is None
        assert ctx.managed_identity_client_id == "mi-123"

    def test_identity_mode_enum(self):
        assert IdentityMode.USER_DELEGATED.value == "user_delegated"
        assert IdentityMode.SERVICE.value == "service"
        assert IdentityMode("service") == IdentityMode.SERVICE
