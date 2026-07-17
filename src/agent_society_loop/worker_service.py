"""Lease-owned worker process for asynchronously planned goals."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from threading import Event as ThreadEvent, Thread
from time import perf_counter
from typing import Callable, Mapping

from .domain import (
    Artifact,
    Attempt,
    Defect,
    Event,
    Review,
    TaskStatus,
    Verdict,
    utc_now,
)
from .memory import MemoryManager
from .ports import Reviewer, Worker, WorkerBlocked
from .scheduler import StaleClaim, TaskClaim, WorkerSession, WorkerSessionRejected
from .tools import ApprovalRequired


class WorkerRunStatus(str, Enum):
    IDLE = "idle"
    COMMITTED = "committed"
    PAUSED = "paused"
    LOST = "lost"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class WorkerServiceConfig:
    worker_id: str
    session_id: str
    heartbeat_ttl_seconds: int = 60
    lease_seconds: int = 30
    renew_interval_seconds: float = 10.0
    poll_interval_seconds: float = 1.0
    min_passing_score: float = 80.0

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or not self.session_id.strip():
            raise ValueError("worker_id and session_id must not be empty")
        if not 0 < self.renew_interval_seconds < self.lease_seconds:
            raise ValueError("renew interval must be shorter than the claim lease")
        if self.lease_seconds >= self.heartbeat_ttl_seconds:
            raise ValueError("worker heartbeat TTL must be longer than the claim lease")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        if not 0 <= self.min_passing_score <= 100:
            raise ValueError("minimum passing score must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    status: WorkerRunStatus
    goal_id: str = ""
    task_id: str = ""
    claim_id: str = ""
    fencing_token: int = 0
    task_status: TaskStatus | None = None
    reason: str = ""


class _LeaseMaintainer:
    def __init__(
        self,
        repository_factory: Callable[[], object],
        claim: TaskClaim,
        config: WorkerServiceConfig,
        clock: Callable[[], str],
    ):
        self.repository_factory = repository_factory
        self.claim = claim
        self.config = config
        self.clock = clock
        self._stop = ThreadEvent()
        self._lost = ThreadEvent()
        self._reason = ""
        self._thread = Thread(
            target=self._run,
            name=f"lease-{claim.claim_id}",
            daemon=True,
        )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        repository = None
        try:
            repository = self.repository_factory()
            while not self._stop.wait(self.config.renew_interval_seconds):
                now = self.clock()
                repository.heartbeat_worker(
                    self.claim.worker_id,
                    self.claim.session_id,
                    now=now,
                    ttl_seconds=self.config.heartbeat_ttl_seconds,
                )
                repository.renew_claim(
                    self.claim.claim_id,
                    self.claim.worker_id,
                    self.claim.session_id,
                    self.claim.fencing_token,
                    now=now,
                    lease_seconds=self.config.lease_seconds,
                )
        except Exception as error:
            self._reason = str(error) or type(error).__name__
            self._lost.set()
        finally:
            if repository is not None:
                close = getattr(repository, "close", None)
                if close is not None:
                    close()


class WorkerService:
    """Discover and execute one fenced task at a time."""

    def __init__(
        self,
        *,
        repository,
        repository_factory: Callable[[], object],
        workers: Mapping[str, Worker],
        assignments: Mapping[str, str],
        reviewer: Reviewer,
        config: WorkerServiceConfig,
        clock: Callable[[], str] = utc_now,
        stop_event: ThreadEvent | None = None,
    ):
        self.repository = repository
        self.repository_factory = repository_factory
        self.workers = dict(workers)
        self.assignments = dict(assignments)
        self.reviewer = reviewer
        self.config = config
        self.clock = clock
        self.stop_event = stop_event or ThreadEvent()
        self.memory = MemoryManager(repository)
        self._session: WorkerSession | None = None
        if not self.assignments:
            raise ValueError("worker service requires at least one task assignment")
        missing = set(self.assignments.values()) - set(self.workers)
        if missing:
            raise ValueError(f"worker implementation is missing: {sorted(missing)[0]}")

    def _ensure_session(self, now: str) -> WorkerSession:
        if self._session is None:
            self._session = WorkerSession.create(
                self.config.worker_id,
                self.config.session_id,
                self.assignments,
                now=now,
                ttl_seconds=self.config.heartbeat_ttl_seconds,
            )
            self.repository.register_worker(self._session)
        else:
            self._session = self.repository.heartbeat_worker(
                self.config.worker_id,
                self.config.session_id,
                now=now,
                ttl_seconds=self.config.heartbeat_ttl_seconds,
            )
        return self._session

    def run_once(self) -> WorkerRunResult:
        now = self.clock()
        self._ensure_session(now)
        claimed = self.repository.claim_next_task(
            self.config.worker_id,
            self.config.session_id,
            self.assignments,
            now=now,
            lease_seconds=self.config.lease_seconds,
        )
        if claimed is None:
            return WorkerRunResult(WorkerRunStatus.IDLE)

        claim = claimed.claim
        task = claimed.task
        goal = self.repository.get_goal(task.goal_id)
        if goal is None:
            raise RuntimeError(f"claimed goal disappeared: {task.goal_id}")
        attempt_no = len(self.repository.list_reviews(task.goal_id, task.task_id)) + 1
        worker = self.workers[claim.agent_id]
        maintainer = _LeaseMaintainer(
            self.repository_factory, claim, self.config, self.clock
        )
        maintainer.start()
        approval_required: ApprovalRequired | None = None
        outcome = None
        try:
            started = perf_counter()
            artifact: Artifact | None = None
            blocked_error: WorkerBlocked | None = None
            try:
                content = worker.execute(task, self.memory.build_context(goal, task))
                artifact = Artifact.create(
                    task.goal_id, task.task_id, claim.agent_id, content
                )
                review = self.reviewer.review(task, content, attempt_no)
            except ApprovalRequired as error:
                approval_required = error
            except WorkerBlocked as error:
                blocked_error = error
                review = Review.create(
                    task.goal_id,
                    task.task_id,
                    attempt_no,
                    Verdict.FAIL,
                    0,
                    (
                        Defect(
                            "execution",
                            error.reason,
                            "operator review is required before resuming",
                        ),
                    ),
                    error.reason,
                )
            except Exception as error:
                review = Review.create(
                    task.goal_id,
                    task.task_id,
                    attempt_no,
                    Verdict.FAIL,
                    0,
                    (Defect("execution", type(error).__name__, str(error)),),
                    f"Execution failed: {error}",
                )

            if approval_required is None:
                duration_ms = (perf_counter() - started) * 1000.0
                passed = (
                    artifact is not None
                    and review.verdict == Verdict.PASS
                    and review.score >= self.config.min_passing_score
                )
                resulting_status = (
                    TaskStatus.BLOCKED
                    if blocked_error is not None
                    else TaskStatus.SUCCEEDED
                    if passed
                    else TaskStatus.FAILED
                    if attempt_no >= task.max_attempts
                    else TaskStatus.PENDING
                )
                resulting_task = replace(
                    task,
                    status=resulting_status,
                    assigned_agent_id=(
                        claim.agent_id if resulting_status != TaskStatus.PENDING else None
                    ),
                    artifact_id=(
                        artifact.artifact_id if passed and artifact is not None else None
                    ),
                )
                performance = self.memory.calculate_outcome(
                    claim.agent_id, task.task_type, passed, review.score, duration_ms
                )
                attempt = Attempt.create(
                    task.goal_id,
                    task.task_id,
                    claim.agent_id,
                    attempt_no,
                    duration_ms,
                    artifact.artifact_id if artifact is not None else None,
                    review.review_id,
                    review.summary if artifact is None else "",
                )
                events = (
                    Event.create(
                        task.goal_id,
                        "task.attempt_completed",
                        {
                            "task_id": task.task_id,
                            "attempt_no": attempt_no,
                            "agent_id": claim.agent_id,
                            "passed": passed,
                            "score": review.score,
                            **(
                                blocked_error.evidence
                                if blocked_error is not None
                                else {}
                            ),
                        },
                    ),
                    Event.create(
                        task.goal_id,
                        (
                            "task.blocked"
                            if blocked_error is not None
                            else "task.succeeded"
                            if passed
                            else "task.review_failed"
                        ),
                        {"task_id": task.task_id, "attempt_no": attempt_no},
                    ),
                )
                outcome = (
                    resulting_task,
                    artifact,
                    attempt,
                    review,
                    performance,
                    events,
                    resulting_status,
                )
        finally:
            maintainer.stop()
        if maintainer.lost:
            return self._lost_result(claim, maintainer.reason)
        try:
            now = self.clock()
            self._session = self.repository.heartbeat_worker(
                claim.worker_id,
                claim.session_id,
                now=now,
                ttl_seconds=self.config.heartbeat_ttl_seconds,
            )
            self.repository.renew_claim(
                claim.claim_id,
                claim.worker_id,
                claim.session_id,
                claim.fencing_token,
                now=now,
                lease_seconds=self.config.lease_seconds,
            )
            if approval_required is not None:
                released = self.repository.pause_claim_for_approval(
                    claim,
                    approval_required.approval,
                    now=self.clock(),
                )
                return WorkerRunResult(
                    WorkerRunStatus.PAUSED,
                    goal_id=task.goal_id,
                    task_id=task.task_id,
                    claim_id=released.claim_id,
                    fencing_token=released.fencing_token,
                    task_status=TaskStatus.PENDING,
                    reason=str(approval_required),
                )
            if outcome is None:
                raise RuntimeError("worker outcome was not constructed")
            (
                resulting_task,
                artifact,
                attempt,
                review,
                performance,
                events,
                resulting_status,
            ) = outcome
            committed: TaskClaim = self.repository.commit_claim_outcome(
                claim,
                resulting_task,
                artifact,
                attempt,
                review,
                performance,
                events,
                now=self.clock(),
            )
        except (StaleClaim, WorkerSessionRejected) as error:
            return self._lost_result(claim, str(error))
        return WorkerRunResult(
            WorkerRunStatus.COMMITTED,
            goal_id=task.goal_id,
            task_id=task.task_id,
            claim_id=committed.claim_id,
            fencing_token=committed.fencing_token,
            task_status=resulting_status,
        )

    def run(self, *, max_tasks: int | None = None) -> WorkerRunResult:
        if max_tasks is not None and (
            isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks < 1
        ):
            raise ValueError("max_tasks must be a positive integer")
        committed = 0
        while not self.stop_event.is_set():
            result = self.run_once()
            if result.status in {WorkerRunStatus.LOST, WorkerRunStatus.PAUSED}:
                return result
            if result.status == WorkerRunStatus.COMMITTED:
                committed += 1
                if max_tasks is not None and committed >= max_tasks:
                    return result
                continue
            self.stop_event.wait(self.config.poll_interval_seconds)
        return WorkerRunResult(
            WorkerRunStatus.STOPPED,
            reason="worker stop requested after draining the active claim",
        )

    def stop(self) -> None:
        self.stop_event.set()

    @staticmethod
    def _lost_result(claim: TaskClaim, reason: str) -> WorkerRunResult:
        return WorkerRunResult(
            WorkerRunStatus.LOST,
            goal_id=claim.goal_id,
            task_id=claim.task_id,
            claim_id=claim.claim_id,
            fencing_token=claim.fencing_token,
            task_status=TaskStatus.RUNNING,
            reason=reason,
        )
