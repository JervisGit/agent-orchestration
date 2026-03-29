"""Unit tests for memory modules — knowledge source and shared state."""

import asyncio

import pytest

from ao.memory.knowledge import InMemoryKnowledgeSource, KnowledgeResult
from ao.memory.shared import MessageBus, SharedState


# ── SharedState ────────────────────────────────────────────────────


class TestSharedState:
    def test_set_and_get(self):
        ss = SharedState()
        ss.set("wf-1", "key", "value")
        assert ss.get("wf-1", "key") == "value"

    def test_get_default(self):
        ss = SharedState()
        assert ss.get("wf-1", "missing") is None
        assert ss.get("wf-1", "missing", 42) == 42

    def test_namespace_isolation(self):
        ss = SharedState()
        ss.set("wf-1", "x", 1)
        ss.set("wf-2", "x", 2)
        assert ss.get("wf-1", "x") == 1
        assert ss.get("wf-2", "x") == 2

    def test_clear(self):
        ss = SharedState()
        ss.set("wf-1", "k", "v")
        ss.clear("wf-1")
        assert ss.get("wf-1", "k") is None


# ── MessageBus (local dev mode) ───────────────────────────────────


class TestMessageBus:
    def test_publish_and_consume_local(self):
        bus = MessageBus()
        asyncio.get_event_loop().run_until_complete(
            bus.publish("topic.a", {"data": 1}, sender_workflow_id="wf-1")
        )
        asyncio.get_event_loop().run_until_complete(
            bus.publish("topic.a", {"data": 2}, sender_workflow_id="wf-2")
        )
        asyncio.get_event_loop().run_until_complete(
            bus.publish("topic.b", {"data": 3}, sender_workflow_id="wf-3")
        )

        msgs_a = asyncio.get_event_loop().run_until_complete(bus.consume_local("topic.a"))
        assert len(msgs_a) == 2
        assert msgs_a[0]["payload"]["data"] == 1

        msgs_b = asyncio.get_event_loop().run_until_complete(bus.consume_local("topic.b"))
        assert len(msgs_b) == 1

    def test_consume_empties_queue(self):
        bus = MessageBus()
        asyncio.get_event_loop().run_until_complete(
            bus.publish("t", {"x": 1})
        )
        asyncio.get_event_loop().run_until_complete(bus.consume_local("t"))
        msgs = asyncio.get_event_loop().run_until_complete(bus.consume_local("t"))
        assert len(msgs) == 0


# ── InMemoryKnowledgeSource ───────────────────────────────────────


class TestInMemoryKnowledgeSource:
    def test_search_matches(self):
        kb = InMemoryKnowledgeSource()
        kb.add_document("The refund policy allows 30-day returns.", {"cat": "policy"})
        kb.add_document("Premium support is 24/7.", {"cat": "support"})

        results = asyncio.get_event_loop().run_until_complete(kb.search("refund"))
        assert len(results) == 1
        assert results[0].score == 1.0
        assert "refund" in results[0].content.lower()

    def test_search_no_match(self):
        kb = InMemoryKnowledgeSource()
        kb.add_document("Hello world")
        results = asyncio.get_event_loop().run_until_complete(kb.search("shipping"))
        assert len(results) == 0

    def test_top_k_limit(self):
        kb = InMemoryKnowledgeSource()
        for i in range(10):
            kb.add_document(f"Document about topic {i}")
        results = asyncio.get_event_loop().run_until_complete(kb.search("topic", top_k=3))
        assert len(results) == 3

    def test_search_case_insensitive(self):
        kb = InMemoryKnowledgeSource()
        kb.add_document("Azure OpenAI is great.")
        results = asyncio.get_event_loop().run_until_complete(kb.search("azure openai"))
        assert len(results) == 1
