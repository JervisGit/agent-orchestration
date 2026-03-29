"""Eval tests — LLM quality evaluation framework.

These tests define evaluation cases that measure workflow output quality.
They run against mock LLM responses (no real API calls) and demonstrate
the evaluation patterns that would run with real LLMs in CI.
"""

import asyncio

import pytest

from ao.engine.patterns.linear import LinearState, build_linear_chain


# ── Evaluation Helpers ─────────────────────────────────────────────


def keyword_match_score(output: str, expected_keywords: list[str]) -> float:
    """Simple keyword-based eval: fraction of expected keywords found."""
    if not expected_keywords:
        return 1.0
    found = sum(1 for kw in expected_keywords if kw.lower() in output.lower())
    return found / len(expected_keywords)


def response_length_ok(output: str, min_len: int = 10, max_len: int = 2000) -> bool:
    """Check that response length is in acceptable range."""
    return min_len <= len(output) <= max_len


# ── Eval Cases ─────────────────────────────────────────────────────


class TestEmailClassificationEval:
    """Evaluate the email classifier accuracy on a test corpus."""

    @pytest.mark.parametrize(
        "input_text,expected_category",
        [
            ("My order arrived broken and I want a refund", "complaint"),
            ("What are your business hours?", "inquiry"),
            ("Thank you for the great service!", "positive"),
            ("I need to reset my password", "inquiry"),
            ("This product is terrible, worst purchase ever", "complaint"),
        ],
    )
    def test_classification_accuracy(self, input_text, expected_category):
        """Each email should be classified correctly."""

        def classify(state: LinearState):
            text = state["input"].lower()
            if any(w in text for w in ["broken", "refund", "terrible", "worst"]):
                cat = "complaint"
            elif any(w in text for w in ["thank", "great", "excellent"]):
                cat = "positive"
            else:
                cat = "inquiry"
            return {"output": cat, "messages": [{"role": "classifier", "content": cat}]}

        graph = build_linear_chain([("classify", classify)])
        compiled = graph.compile()
        result = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke({"input": input_text, "messages": [], "output": ""})
        )
        assert result["output"] == expected_category


class TestResponseQualityEval:
    """Evaluate generated response quality (keyword match, length)."""

    def test_complaint_response_quality(self):
        def respond(state: LinearState):
            return {
                "output": "We sincerely apologize for the inconvenience. A refund has been initiated.",
                "messages": [],
            }

        graph = build_linear_chain([("respond", respond)])
        compiled = graph.compile()
        result = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke({"input": "broken item", "messages": [], "output": ""})
        )
        score = keyword_match_score(
            result["output"], ["apologize", "refund", "inconvenience"]
        )
        assert score >= 0.66, f"Keyword match score too low: {score}"
        assert response_length_ok(result["output"])

    def test_inquiry_response_quality(self):
        def respond(state: LinearState):
            return {
                "output": "Our business hours are Monday to Friday, 9 AM to 5 PM.",
                "messages": [],
            }

        graph = build_linear_chain([("respond", respond)])
        compiled = graph.compile()
        result = asyncio.get_event_loop().run_until_complete(
            compiled.ainvoke({"input": "hours?", "messages": [], "output": ""})
        )
        score = keyword_match_score(
            result["output"], ["hours", "Monday", "Friday"]
        )
        assert score >= 0.66
        assert response_length_ok(result["output"])
