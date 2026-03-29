"""Eval runner — periodic LLM quality and security evaluations.

Runs evaluation test suites against workflow outputs to track quality
over time. Results are stored for dashboard visualization.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class EvalCase:
    """A single evaluation test case."""

    name: str
    input_data: dict[str, Any]
    expected: str = ""  # Expected output (for reference-based eval)
    judge_fn: Callable[..., float] | None = None  # Custom scoring fn → 0.0-1.0


@dataclass
class EvalResult:
    """Result of running one eval case."""

    case_name: str
    score: float  # 0.0 - 1.0
    actual_output: str = ""
    expected_output: str = ""
    details: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class EvalSuite:
    """A collection of eval cases for a workflow."""

    name: str
    workflow_id: str
    cases: list[EvalCase] = field(default_factory=list)


class EvalRunner:
    """Runs evaluation suites and collects results."""

    def __init__(self):
        self._suites: dict[str, EvalSuite] = {}
        self._results: list[EvalResult] = []

    def register_suite(self, suite: EvalSuite) -> None:
        self._suites[suite.name] = suite

    async def run_suite(
        self,
        suite_name: str,
        run_fn: Callable[..., Any],
    ) -> list[EvalResult]:
        """Run all cases in a suite.

        Args:
            suite_name: Name of the registered suite.
            run_fn: Async callable that takes input_data and returns output string.

        Returns:
            List of EvalResults.
        """
        suite = self._suites.get(suite_name)
        if not suite:
            logger.error("Eval suite '%s' not found", suite_name)
            return []

        results = []
        for case in suite.cases:
            try:
                actual = await run_fn(case.input_data)
                actual_str = str(actual)

                if case.judge_fn:
                    score = case.judge_fn(actual_str, case.expected)
                elif case.expected:
                    # Simple exact match scoring
                    score = 1.0 if actual_str.strip() == case.expected.strip() else 0.0
                else:
                    score = 1.0  # No expectation = pass

                result = EvalResult(
                    case_name=case.name,
                    score=score,
                    actual_output=actual_str,
                    expected_output=case.expected,
                )
            except Exception as e:
                logger.exception("Eval case '%s' failed", case.name)
                result = EvalResult(
                    case_name=case.name,
                    score=0.0,
                    details=f"Error: {e}",
                )

            results.append(result)
            self._results.append(result)
            logger.info(
                "Eval %s/%s: score=%.2f",
                suite_name,
                case.name,
                result.score,
            )

        avg_score = sum(r.score for r in results) / len(results) if results else 0
        logger.info("Suite '%s' average score: %.2f", suite_name, avg_score)
        return results

    @property
    def all_results(self) -> list[EvalResult]:
        return list(self._results)
