"""Reproducible champion/challenger evaluation and promotion gates."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Any, Callable, Sequence
from uuid import uuid4

from .domain import (
    BenchmarkCase,
    CandidateExecution,
    CandidateIdentity,
    CaseEvaluation,
    EvaluationOutcome,
    EvaluationRun,
)
from .storage import SQLiteRepository


CandidateRunner = Callable[[CandidateIdentity, BenchmarkCase], CandidateExecution]
CaseEvaluator = Callable[[BenchmarkCase, Any], CaseEvaluation]


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    minimum_cases: int = 5
    minimum_score_delta: float = 2.0
    maximum_case_regression: float = 10.0
    maximum_p95_latency_ratio: float = 1.5

    def __post_init__(self) -> None:
        if self.minimum_cases < 1:
            raise ValueError("minimum_cases must be positive")
        if self.minimum_score_delta < 0 or self.maximum_case_regression < 0:
            raise ValueError("score gate values must not be negative")
        if self.maximum_p95_latency_ratio <= 0:
            raise ValueError("latency ratio must be positive")


def benchmark_digest(cases: Sequence[BenchmarkCase]) -> str:
    canonical = json.dumps(
        [asdict(case) for case in sorted(cases, key=lambda item: item.case_id)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BenchmarkEvaluator:
    def __init__(
        self,
        repository: SQLiteRepository,
        runner: CandidateRunner,
        evaluator: CaseEvaluator,
        policy: PromotionPolicy | None = None,
    ):
        self.repository = repository
        self.runner = runner
        self.evaluator = evaluator
        self.policy = policy or PromotionPolicy()

    def evaluate(
        self,
        cases: Sequence[BenchmarkCase],
        champion: CandidateIdentity,
        challenger: CandidateIdentity,
    ) -> EvaluationRun:
        benchmark = tuple(cases)
        task_type = _validate_benchmark(benchmark)
        if champion.agent_id == challenger.agent_id:
            raise ValueError("champion and challenger must be different agents")
        run_id = f"evaluation-{uuid4().hex[:16]}"
        outcomes: list[EvaluationOutcome] = []

        for case in benchmark:
            for candidate in (champion, challenger):
                outcome = self._evaluate_case(run_id, case, candidate)
                self.repository.save_evaluation_outcome(outcome)
                outcomes.append(outcome)

        champion_outcomes = [
            outcome for outcome in outcomes
            if outcome.candidate_agent_id == champion.agent_id
        ]
        challenger_outcomes = [
            outcome for outcome in outcomes
            if outcome.candidate_agent_id == challenger.agent_id
        ]
        metrics, failed_gates = _apply_gates(
            champion_outcomes, challenger_outcomes, self.policy
        )
        run = EvaluationRun(
            run_id=run_id,
            task_type=task_type,
            benchmark_digest=benchmark_digest(benchmark),
            champion_agent_id=champion.agent_id,
            champion_model_id=champion.model_id,
            challenger_agent_id=challenger.agent_id,
            challenger_model_id=challenger.model_id,
            case_count=len(benchmark),
            metrics=metrics,
            recommended=not failed_gates,
            failed_gates=tuple(failed_gates),
        )
        self.repository.save_evaluation_run(run)
        return run

    def _evaluate_case(
        self,
        run_id: str,
        case: BenchmarkCase,
        candidate: CandidateIdentity,
    ) -> EvaluationOutcome:
        duration_ms = 0.0
        error = ""
        phase = "candidate execution"
        try:
            execution = self.runner(candidate, case)
            if not isinstance(execution, CandidateExecution):
                raise TypeError("runner returned an invalid result")
            duration_ms = float(execution.duration_ms)
            phase = "case evaluation"
            evaluation = self.evaluator(case, execution.output)
            if not isinstance(evaluation, CaseEvaluation):
                raise TypeError("evaluator returned an invalid result")
            passed = evaluation.passed
            score = evaluation.score
        except Exception as failure:
            passed = False
            score = 0.0
            error = f"{type(failure).__name__}: {phase} failed"
        return EvaluationOutcome(
            outcome_id=f"outcome-{uuid4().hex[:16]}",
            run_id=run_id,
            case_id=case.case_id,
            candidate_agent_id=candidate.agent_id,
            candidate_model_id=candidate.model_id,
            passed=passed,
            score=float(score),
            duration_ms=duration_ms,
            critical=case.critical,
            error=error,
        )


def _validate_benchmark(cases: Sequence[BenchmarkCase]) -> str:
    if not cases:
        raise ValueError("benchmark must contain at least one case")
    identifiers = [case.case_id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("benchmark contains duplicate case IDs")
    task_types = {case.task_type for case in cases}
    if len(task_types) != 1:
        raise ValueError("benchmark cases must share one task type")
    return next(iter(task_types))


def _apply_gates(
    champion: Sequence[EvaluationOutcome],
    challenger: Sequence[EvaluationOutcome],
    policy: PromotionPolicy,
) -> tuple[dict[str, Any], list[str]]:
    champion_by_case = {outcome.case_id: outcome for outcome in champion}
    challenger_by_case = {outcome.case_id: outcome for outcome in challenger}
    if champion_by_case.keys() != challenger_by_case.keys():
        raise ValueError("candidate outcomes do not cover the same cases")

    champion_pass_rate = _pass_rate(champion)
    challenger_pass_rate = _pass_rate(challenger)
    champion_mean = fmean(outcome.score for outcome in champion)
    challenger_mean = fmean(outcome.score for outcome in challenger)
    champion_p95 = _percentile_95([outcome.duration_ms for outcome in champion])
    challenger_p95 = _percentile_95([outcome.duration_ms for outcome in challenger])
    regressions = {
        case_id: champion_by_case[case_id].score - challenger_by_case[case_id].score
        for case_id in champion_by_case
    }
    critical_failures = sorted(
        outcome.case_id
        for outcome in challenger
        if outcome.critical and not outcome.passed
    )

    failed: list[str] = []
    if len(challenger) < policy.minimum_cases:
        failed.append("minimum_cases")
    if critical_failures:
        failed.append("critical_cases")
    if challenger_pass_rate < champion_pass_rate:
        failed.append("pass_rate")
    if challenger_mean - champion_mean < policy.minimum_score_delta:
        failed.append("mean_score")
    if max(regressions.values(), default=0.0) > policy.maximum_case_regression:
        failed.append("per_case_regression")
    if challenger_p95 > champion_p95 * policy.maximum_p95_latency_ratio:
        failed.append("p95_latency")

    metrics: dict[str, Any] = {
        "champion_pass_rate": round(champion_pass_rate, 6),
        "challenger_pass_rate": round(challenger_pass_rate, 6),
        "champion_mean_score": round(champion_mean, 6),
        "challenger_mean_score": round(challenger_mean, 6),
        "mean_score_delta": round(challenger_mean - champion_mean, 6),
        "champion_p95_latency_ms": round(champion_p95, 6),
        "challenger_p95_latency_ms": round(challenger_p95, 6),
        "maximum_case_regression": round(max(regressions.values(), default=0.0), 6),
        "critical_failures": critical_failures,
    }
    return metrics, failed


def _pass_rate(outcomes: Sequence[EvaluationOutcome]) -> float:
    return sum(outcome.passed for outcome in outcomes) / len(outcomes)


def _percentile_95(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
