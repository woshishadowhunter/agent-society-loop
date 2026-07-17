"""Dependency-inversion protocols used by the runtime engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .domain import (
    ApprovalRequest,
    Artifact,
    Attempt,
    Event,
    Goal,
    OutboxMessage,
    OutboxStatus,
    PerformanceRecord,
    Review,
    Task,
)
from .scheduler import ClaimedTask, TaskClaim, WorkerSession


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


@dataclass(frozen=True, slots=True)
class WorkerExecution:
    content: str
    outbox_messages: tuple[OutboxMessage, ...] = ()


class Planner(Protocol):
    def plan(self, goal: Goal, context: dict[str, Any]) -> Sequence[Task]: ...


class Worker(Protocol):
    agent_id: str

    def execute(
        self, task: Task, context: dict[str, Any]
    ) -> str | WorkerExecution: ...


class Reviewer(Protocol):
    def review(self, task: Task, artifact: str, attempt_no: int) -> Review: ...


class ModelProvider(Protocol):
    def complete(
        self, messages: Sequence[dict[str, str]], *, temperature: float = 0.0
    ) -> str: ...


@runtime_checkable
class SchedulerRepository(Protocol):
    """Persistence boundary required by lease-based scheduler workers."""

    def scheduler_now(self) -> str: ...

    def operational_counts(self, now: str) -> dict[str, int]: ...

    def register_worker(self, session: WorkerSession) -> WorkerSession: ...

    def heartbeat_worker(
        self,
        worker_id: str,
        session_id: str,
        *,
        now: str,
        ttl_seconds: int,
    ) -> WorkerSession: ...

    def get_worker(self, worker_id: str) -> WorkerSession | None: ...

    def list_workers(self) -> list[WorkerSession]: ...

    def claim_task(
        self,
        goal_id: str,
        task_id: str,
        worker_id: str,
        session_id: str,
        agent_id: str,
        *,
        now: str,
        lease_seconds: int,
    ) -> TaskClaim | None: ...

    def claim_next_task(
        self,
        worker_id: str,
        session_id: str,
        assignments: Mapping[str, str],
        *,
        now: str,
        lease_seconds: int,
    ) -> ClaimedTask | None: ...

    def renew_claim(
        self,
        claim_id: str,
        worker_id: str,
        session_id: str,
        fencing_token: int,
        *,
        now: str,
        lease_seconds: int,
    ) -> TaskClaim: ...

    def release_claim(
        self,
        claim_id: str,
        worker_id: str,
        session_id: str,
        fencing_token: int,
        *,
        now: str,
        reason: str = "",
    ) -> TaskClaim: ...

    def pause_claim_for_approval(
        self,
        claim: TaskClaim,
        approval: ApprovalRequest,
        *,
        now: str,
    ) -> TaskClaim: ...

    def reap_expired_claims(self, *, now: str) -> list[TaskClaim]: ...

    def commit_claim_outcome(
        self,
        claim: TaskClaim,
        task: Task,
        artifact: Artifact | None,
        attempt: Attempt,
        review: Review,
        performance: PerformanceRecord,
        events: Sequence[Event],
        outbox_messages: Sequence[OutboxMessage] = (),
        *,
        now: str,
    ) -> TaskClaim: ...

    def get_claim(self, claim_id: str) -> TaskClaim | None: ...

    def list_claims(self, goal_id: str | None = None) -> list[TaskClaim]: ...

    def enqueue_outbox(self, message: OutboxMessage) -> OutboxMessage: ...

    def claim_outbox(
        self,
        worker_id: str,
        *,
        topic: str | None = None,
        now: str,
        lease_seconds: int,
    ) -> OutboxMessage | None: ...

    def renew_outbox(
        self,
        message_id: str,
        worker_id: str,
        delivery_token: int,
        *,
        now: str,
        lease_seconds: int,
    ) -> OutboxMessage: ...

    def complete_outbox(
        self,
        message_id: str,
        worker_id: str,
        delivery_token: int,
        *,
        now: str,
        error: str = "",
        retry_seconds: int = 0,
    ) -> OutboxMessage: ...

    def list_outbox(
        self,
        *,
        status: OutboxStatus | None = None,
        limit: int | None = None,
    ) -> list[OutboxMessage]: ...

    def purge_outbox(self, *, before: str, limit: int) -> int: ...
