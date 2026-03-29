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
