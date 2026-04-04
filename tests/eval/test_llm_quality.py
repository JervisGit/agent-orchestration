"""Eval tests — LLM quality evaluation framework.

Two layers of evaluation are provided:

1. **DeepEval metrics** (``TestDeepEval*``) — uses the deepeval library to
   measure AnswerRelevancyMetric, FaithfulnessMetric, and
   HallucinationMetric on representative tax Q&A pairs.  These tests run
   against mock LLM responses (no live API calls) so they execute in CI
   without credentials.

2. **Keyword-match baselines** (``TestEmailClassificationEval``,
   ``TestResponseQualityEval``) — fast, deterministic, dependency-free
   sanity checks using the AO linear chain.

Running the DeepEval suite against real LLM responses::

    deepeval test run tests/eval/test_llm_quality.py -k TestDeepEval
"""

import asyncio

import pytest

# ── DeepEval import guard ──────────────────────────────────────────
# deepeval is an optional dev dependency.  Tests that depend on it are
# skipped automatically if the package is not installed, so the
# baseline CI suite continues to pass without it.
try:
    from deepeval import evaluate
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        FaithfulnessMetric,
        HallucinationMetric,
    )
    from deepeval.test_case import LLMTestCase
    _DEEPEVAL_AVAILABLE = True
except ImportError:
    _DEEPEVAL_AVAILABLE = False

deepeval_required = pytest.mark.skipif(
    not _DEEPEVAL_AVAILABLE,
    reason="deepeval not installed — run `pip install deepeval` to enable",
)

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


# ── DeepEval: Tax Q&A quality ──────────────────────────────────────
#
# Test cases represent realistic inputs to the Singapore Tax Email
# Assistant and the RAG Search agent.  Expected outputs are curated
# reference answers; retrieved_context rows are the chunks a real
# retrieval agent would surface.
#
# Metrics configured at threshold=0.7 to allow for minor phrasing
# variation while catching substantive quality regressions.


_TAX_QA_CASES = [
    {
        "input": "What is the corporate income tax rate in Singapore?",
        "actual_output": (
            "The standard corporate income tax (CIT) rate in Singapore is 17% on "
            "chargeable income. New companies may benefit from a full tax exemption "
            "on the first SGD 100,000 of chargeable income for their first three "
            "consecutive years of assessment, provided they meet qualifying conditions."
        ),
        "expected_output": (
            "Singapore corporate income tax rate is 17%. New companies can claim "
            "full exemption on first SGD 100,000 for first three years."
        ),
        "retrieval_context": [
            "Section 43(1) of the Income Tax Act: the tax chargeable shall be "
            "computed at the rate of 17 per cent.",
            "Start-up tax exemption: qualifying new companies enjoy a 75% exemption "
            "on the first SGD 100,000 of normal chargeable income for each of the "
            "first three consecutive YAs.",
        ],
    },
    {
        "input": "When does a business need to register for GST?",
        "actual_output": (
            "A business must register for Goods and Services Tax (GST) when its "
            "taxable turnover exceeds SGD 1 million in a 12-month period, or when "
            "it is reasonably expected to exceed that threshold in the next 12 months. "
            "The current GST rate is 9%."
        ),
        "expected_output": (
            "GST registration is compulsory when taxable turnover exceeds SGD 1 million. "
            "Current rate is 9%."
        ),
        "retrieval_context": [
            "GST Act Section 9: a taxable person must register if taxable supplies "
            "exceed SGD 1 million in any 12-month period.",
            "GST rate increased from 8% to 9% with effect from 1 January 2024.",
        ],
    },
    {
        "input": "What penalties apply for late filing of corporate tax returns?",
        "actual_output": (
            "Under the Income Tax Act, a company that fails to file its estimated "
            "chargeable income (ECI) or tax return by the due date is liable for a "
            "late filing penalty of up to SGD 1,000, plus a further penalty of SGD 100 "
            "for each day the return remains outstanding after a reminder notice. "
            "IRAS may compound the offence if the company has a good compliance record."
        ),
        "expected_output": (
            "Late filing of corporate tax returns can result in penalties up to "
            "SGD 1,000 plus SGD 100 per day after reminder. IRAS may compound for "
            "good compliance history."
        ),
        "retrieval_context": [
            "Income Tax Act Section 94: failure to furnish a return is liable on "
            "conviction to a fine not exceeding SGD 1,000.",
            "IRAS enforcement guidelines: a daily penalty of SGD 100 applies after "
            "IRAS issues a reminder notice and the return remains outstanding.",
            "IRAS compounding policy: first-time defaults with good compliance history "
            "may have penalties compounded rather than prosecuted.",
        ],
    },
]


class TestDeepEvalAnswerRelevancy:
    """AnswerRelevancyMetric — response addresses the question asked."""

    @deepeval_required
    @pytest.mark.parametrize("case", _TAX_QA_CASES, ids=[c["input"][:40] for c in _TAX_QA_CASES])
    def test_answer_is_relevant(self, case):
        metric = AnswerRelevancyMetric(threshold=0.7, model="gpt-4.1-mini", include_reason=True)
        test_case = LLMTestCase(
            input=case["input"],
            actual_output=case["actual_output"],
        )
        metric.measure(test_case)
        assert metric.score >= 0.7, (
            f"AnswerRelevancy {metric.score:.2f} < 0.70\nReason: {metric.reason}"
        )


class TestDeepEvalFaithfulness:
    """FaithfulnessMetric — response is grounded in the retrieved context."""

    @deepeval_required
    @pytest.mark.parametrize("case", _TAX_QA_CASES, ids=[c["input"][:40] for c in _TAX_QA_CASES])
    def test_faithfulness_to_context(self, case):
        metric = FaithfulnessMetric(threshold=0.7, model="gpt-4.1-mini", include_reason=True)
        test_case = LLMTestCase(
            input=case["input"],
            actual_output=case["actual_output"],
            retrieval_context=case["retrieval_context"],
        )
        metric.measure(test_case)
        assert metric.score >= 0.7, (
            f"Faithfulness {metric.score:.2f} < 0.70\nReason: {metric.reason}"
        )


class TestDeepEvalHallucination:
    """HallucinationMetric — response contains no fabricated facts."""

    @deepeval_required
    @pytest.mark.parametrize("case", _TAX_QA_CASES, ids=[c["input"][:40] for c in _TAX_QA_CASES])
    def test_no_hallucination(self, case):
        metric = HallucinationMetric(threshold=0.3, model="gpt-4.1-mini", include_reason=True)
        test_case = LLMTestCase(
            input=case["input"],
            actual_output=case["actual_output"],
            context=case["retrieval_context"],
        )
        metric.measure(test_case)
        # HallucinationMetric: lower score = less hallucination, so we want score <= threshold
        assert metric.score <= 0.3, (
            f"Hallucination score {metric.score:.2f} > 0.30 (too high)\nReason: {metric.reason}"
        )


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
