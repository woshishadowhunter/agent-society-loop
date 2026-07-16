"""Dependency-inversion protocols used by the runtime engine."""

from __future__ import annotations

from typing import Any, Protocol, Sequence

from .domain import Goal, Review, Task


class WorkerBlocked(RuntimeError):
    """A worker cannot be retried automatically without losing safety evidence."""

    _EVIDENCE_KEYS = {
        "delegation_id",
        "card_sha256",
        "remote_task_id",
        "status",
        "error_category",
        "policy_decision_id",
        "policy_digest",
        "policy_rule_id",
        "policy_verdict",
        "policy_reason",
    }

    def __init__(self, reason: str, evidence: dict[str, Any] | None = None):
        normalized_reason = " ".join(reason.split()).strip()[:256]
        if not normalized_reason:
            raise ValueError("blocked worker reason must not be empty")
        self.reason = normalized_reason
        self.evidence = {
            key: value[:256] if isinstance(value, str) else value
            for key, value in dict(evidence or {}).items()
            if key in self._EVIDENCE_KEYS
            and isinstance(value, (str, int, float, bool))
        }
        super().__init__(self.reason)


class Planner(Protocol):
    def plan(self, goal: Goal, context: dict[str, Any]) -> Sequence[Task]: ...


class Worker(Protocol):
    agent_id: str

    def execute(self, task: Task, context: dict[str, Any]) -> str: ...


class Reviewer(Protocol):
    def review(self, task: Task, artifact: str, attempt_no: int) -> Review: ...


class ModelProvider(Protocol):
    def complete(
        self, messages: Sequence[dict[str, str]], *, temperature: float = 0.0
    ) -> str: ...
