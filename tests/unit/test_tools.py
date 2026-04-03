"""Unit tests for tool registry and executor."""

import asyncio

import pytest

from ao.identity.context import IdentityContext, IdentityMode
from ao.tools.registry import ToolRegistry, ToolSpec


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        reg.register(
            name="search",
            fn=lambda **kw: {"results": []},
            description="Search the knowledge base",
            parameters={"query": {"type": "string"}},
        )
        spec = reg.get("search")
        assert spec is not None
        assert spec.name == "search"
        assert reg.get("unknown") is None

    def test_list_tools(self):
        reg = ToolRegistry()
        reg.register(name="a", fn=lambda: None, description="A")
        reg.register(name="b", fn=lambda: None, description="B")
        names = [t.name for t in reg.list_tools()]
        assert "a" in names
        assert "b" in names

    def test_duplicate_overwrites(self):
        reg = ToolRegistry()
        reg.register(name="x", fn=lambda: 1, description="v1")
        reg.register(name="x", fn=lambda: 2, description="v2")
        assert reg.get("x").description == "v2"

    def test_to_langchain_tools(self):
        reg = ToolRegistry()
        reg.register(
            name="echo",
            fn=lambda text="": text,
            description="Echo input",
        )
        lc_tools = reg.to_langchain_tools()
        assert len(lc_tools) == 1
        assert lc_tools[0].name == "echo"


class TestToolExecutorIdentity:
    """Verify ToolExecutor enforces required_identity and injects identity correctly."""

    def _make_service_identity(self) -> IdentityContext:
        return IdentityContext(
            mode=IdentityMode.SERVICE,
            tenant_id="00000000-0000-0000-0000-000000000000",
            managed_identity_client_id="00000000-0000-0000-0000-000000000001",
        )

    def _make_delegated_identity(self) -> IdentityContext:
        return IdentityContext(
            mode=IdentityMode.USER_DELEGATED,
            tenant_id="00000000-0000-0000-0000-000000000000",
            user_token="dummy-obo-token",
            claims={"oid": "00000000-0000-0000-0000-000000000002", "upn": "agent@test.local"},
        )

    def _make_registry_with_tool(self, required_identity=None, accept_identity_param=False):
        from ao.tools.executor import ToolExecutor

        if accept_identity_param:
            async def tool_fn(identity=None):
                return {"captured_identity": identity}
        else:
            async def tool_fn():
                return {"ok": True}

        reg = ToolRegistry()
        reg.register(
            name="taxpayer_lookup",
            fn=tool_fn,
            description="Look up taxpayer record",
            required_identity=required_identity,
        )
        return reg, ToolExecutor(reg)

    def test_service_tool_allows_service_caller(self):
        reg, executor = self._make_registry_with_tool(
            required_identity=IdentityMode.SERVICE
        )
        identity = self._make_service_identity()
        result = asyncio.run(executor.execute("taxpayer_lookup", identity))
        assert result == {"ok": True}

    def test_service_tool_blocks_delegated_caller(self):
        """An agent UAMI calling a SERVICE-only tool must raise PermissionError."""
        reg, executor = self._make_registry_with_tool(
            required_identity=IdentityMode.SERVICE
        )
        identity = self._make_delegated_identity()
        with pytest.raises(PermissionError, match="requires service"):
            asyncio.run(executor.execute("taxpayer_lookup", identity))

    def test_delegated_tool_blocks_service_caller(self):
        """A tool that requires delegated context (e.g. OBO) must block service identity."""
        reg, executor = self._make_registry_with_tool(
            required_identity=IdentityMode.USER_DELEGATED
        )
        identity = self._make_service_identity()
        with pytest.raises(PermissionError, match="requires user_delegated"):
            asyncio.run(executor.execute("taxpayer_lookup", identity))

    def test_unrestricted_tool_allows_any_identity(self):
        """required_identity=None means any caller is accepted."""
        reg, executor = self._make_registry_with_tool(required_identity=None)
        for identity in (self._make_service_identity(), self._make_delegated_identity()):
            result = asyncio.run(executor.execute("taxpayer_lookup", identity))
            assert result == {"ok": True}

    def test_identity_injected_when_param_declared(self):
        """Identity context flows into the tool fn when it declares the `identity` param."""
        reg, executor = self._make_registry_with_tool(
            required_identity=None, accept_identity_param=True
        )
        identity = self._make_service_identity()
        result = asyncio.run(executor.execute("taxpayer_lookup", identity))
        assert result["captured_identity"] is identity

    def test_identity_not_injected_when_param_absent(self):
        """Tools without an `identity` param must not receive it — no TypeError."""
        reg, executor = self._make_registry_with_tool(
            required_identity=None, accept_identity_param=False
        )
        identity = self._make_service_identity()
        # Would raise TypeError if `identity` were injected into a fn that doesn't expect it
        result = asyncio.run(executor.execute("taxpayer_lookup", identity))
        assert result == {"ok": True}

    def test_unknown_tool_raises_value_error(self):
        from ao.tools.executor import ToolExecutor

        executor = ToolExecutor(ToolRegistry())
        with pytest.raises(ValueError, match="not found"):
            asyncio.run(
                executor.execute("nonexistent", self._make_service_identity())
            )

