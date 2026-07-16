import tempfile
import unittest
from pathlib import Path

from agent_society_loop.domain import (
    BenchmarkCase,
    CandidateExecution,
    CandidateIdentity,
    CaseEvaluation,
)
from agent_society_loop.evaluation import (
    BenchmarkEvaluator,
    PromotionPolicy,
    benchmark_digest,
)
from agent_society_loop.storage import SQLiteRepository


class ScriptedRunner:
    def __init__(self, scores, durations=None):
        self.scores = scores
        self.durations = durations or {}

    def __call__(self, candidate, case):
        score = self.scores[candidate.agent_id][case.case_id]
        duration = self.durations.get(candidate.agent_id, {}).get(case.case_id, 100.0)
        return CandidateExecution({"score": score}, duration)


def score_output(case, output):
    score = float(output["score"])
    return CaseEvaluation(score >= 70, score)


def cases(count=5, *, critical=()):
    return tuple(
        BenchmarkCase.create(
            f"case-{index}",
            "analysis",
            {"prompt": f"question {index}"},
            {"minimum_score": 70},
            critical=index in critical,
        )
        for index in range(1, count + 1)
    )


class BenchmarkEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.champion = CandidateIdentity("champion", "model-a")
        self.challenger = CandidateIdentity("challenger", "model-b")

    def tearDown(self):
        self.repository.close()

    def evaluate(self, benchmark, champion_scores, challenger_scores, durations=None):
        runner = ScriptedRunner(
            {
                "champion": champion_scores,
                "challenger": challenger_scores,
            },
            durations,
        )
        return BenchmarkEvaluator(
            self.repository, runner, score_output, PromotionPolicy()
        ).evaluate(benchmark, self.champion, self.challenger)

    def test_recommends_challenger_only_when_every_gate_passes(self):
        benchmark = cases()
        champion_scores = {case.case_id: 80 for case in benchmark}
        challenger_scores = {case.case_id: 84 for case in benchmark}

        run = self.evaluate(benchmark, champion_scores, challenger_scores)

        self.assertTrue(run.recommended)
        self.assertEqual(run.failed_gates, ())
        self.assertEqual(run.metrics["mean_score_delta"], 4.0)
        outcomes = self.repository.list_evaluation_outcomes(run.run_id)
        self.assertEqual(len(outcomes), 10)
        self.assertEqual(
            {outcome.candidate_agent_id for outcome in outcomes},
            {"champion", "challenger"},
        )

    def test_critical_failure_and_per_case_regression_block_recommendation(self):
        benchmark = cases(critical=(3,))
        champion_scores = {case.case_id: 80 for case in benchmark}
        challenger_scores = {
            "case-1": 100,
            "case-2": 100,
            "case-3": 60,
            "case-4": 100,
            "case-5": 100,
        }

        run = self.evaluate(benchmark, champion_scores, challenger_scores)

        self.assertFalse(run.recommended)
        self.assertIn("critical_cases", run.failed_gates)
        self.assertIn("per_case_regression", run.failed_gates)

    def test_minimum_cases_pass_rate_score_and_latency_are_independent_gates(self):
        benchmark = cases(4)
        champion_scores = {case.case_id: 80 for case in benchmark}
        challenger_scores = {case.case_id: 60 for case in benchmark}
        durations = {
            "champion": {case.case_id: 100 for case in benchmark},
            "challenger": {case.case_id: 200 for case in benchmark},
        }

        run = self.evaluate(
            benchmark, champion_scores, challenger_scores, durations=durations
        )

        self.assertEqual(
            set(run.failed_gates),
            {"minimum_cases", "pass_rate", "mean_score", "per_case_regression", "p95_latency"},
        )

    def test_runner_failure_is_persisted_as_sanitized_failed_outcome(self):
        benchmark = cases()

        def failing_runner(candidate, case):
            if candidate.agent_id == "challenger" and case.case_id == "case-2":
                raise RuntimeError("private provider response")
            return CandidateExecution({"score": 80}, 10)

        run = BenchmarkEvaluator(
            self.repository, failing_runner, score_output, PromotionPolicy()
        ).evaluate(benchmark, self.champion, self.challenger)
        outcome = next(
            item
            for item in self.repository.list_evaluation_outcomes(run.run_id)
            if item.candidate_agent_id == "challenger" and item.case_id == "case-2"
        )

        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.score, 0)
        self.assertEqual(outcome.error, "RuntimeError: candidate execution failed")
        self.assertNotIn("private", outcome.error)

    def test_benchmark_digest_is_stable_and_sensitive_to_criteria(self):
        first = cases()
        equivalent = tuple(
            BenchmarkCase.create(
                case.case_id,
                case.task_type,
                dict(reversed(list(case.input.items()))),
                dict(reversed(list(case.acceptance_criteria.items()))),
                critical=case.critical,
            )
            for case in first
        )
        changed = list(first)
        changed[0] = BenchmarkCase.create(
            "case-1", "analysis", {"prompt": "question 1"}, {"minimum_score": 90}
        )

        self.assertEqual(benchmark_digest(first), benchmark_digest(equivalent))
        self.assertEqual(benchmark_digest(first), benchmark_digest(tuple(reversed(first))))
        self.assertNotEqual(benchmark_digest(first), benchmark_digest(changed))

    def test_evaluator_failure_is_classified_even_when_execution_has_zero_latency(self):
        benchmark = cases()

        def runner(candidate, case):
            return CandidateExecution({"score": 80}, 0)

        def failing_evaluator(case, output):
            raise RuntimeError("private judge response")

        run = BenchmarkEvaluator(
            self.repository, runner, failing_evaluator, PromotionPolicy()
        ).evaluate(benchmark, self.champion, self.challenger)

        outcomes = self.repository.list_evaluation_outcomes(run.run_id)
        self.assertTrue(outcomes)
        self.assertTrue(
            all(outcome.error == "RuntimeError: case evaluation failed" for outcome in outcomes)
        )

    def test_evaluation_run_and_outcomes_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evaluation.db"
            repository = SQLiteRepository(path)
            benchmark = cases()
            scores = {case.case_id: 80 for case in benchmark}
            better = {case.case_id: 84 for case in benchmark}
            run = BenchmarkEvaluator(
                repository,
                ScriptedRunner({"champion": scores, "challenger": better}),
                score_output,
                PromotionPolicy(),
            ).evaluate(benchmark, self.champion, self.challenger)
            repository.close()

            reopened = SQLiteRepository(path)
            self.assertEqual(reopened.get_evaluation_run(run.run_id), run)
            self.assertEqual(len(reopened.list_evaluation_outcomes(run.run_id)), 10)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
