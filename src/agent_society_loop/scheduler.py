"""Scheduler ownership contracts and deterministic safety diagnostics."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable
from uuid import uuid4


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_MAX_DURATION_SECONDS = 86_400


class WorkerSessionRejected(RuntimeError):
    """The worker session is missing, expired, or has been superseded."""


class StaleClaim(RuntimeError):
    """A claim no longer grants authority to mutate task state."""


class ClaimStatus(str, Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


def _safe_id(name: str, value: str) -> str:
    normalized = str(value).strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError(f"{name} must be a safe identifier")
    return normalized


def validate_duration(seconds: int) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError("duration must be an integer number of seconds")
    if not 1 <= seconds <= _MAX_DURATION_SECONDS:
        raise ValueError(f"duration must be between 1 and {_MAX_DURATION_SECONDS} seconds")
    return seconds


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty UTC ISO-8601 string")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("timestamp must be a valid UTC ISO-8601 string") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must include the UTC offset")
    return parsed.astimezone(timezone.utc)


def utc_after(value: str, seconds: int) -> str:
    return (parse_utc(value) + timedelta(seconds=validate_duration(seconds))).isoformat()


def _bounded_reason(reason: str) -> str:
    return " ".join(str(reason).split()).strip()[:256]


@dataclass(frozen=True, slots=True)
class WorkerSession:
    worker_id: str
    session_id: str
    capabilities: tuple[str, ...]
    started_at: str
    last_heartbeat_at: str
    expires_at: str

    @classmethod
    def create(
        cls,
        worker_id: str,
        session_id: str,
        capabilities: Iterable[str],
        *,
        now: str,
        ttl_seconds: int,
    ) -> WorkerSession:
        current = parse_utc(now).isoformat()
        normalized_capabilities = tuple(
            sorted({_safe_id("capabilities", item) for item in capabilities})
        )
        return cls(
            worker_id=_safe_id("worker_id", worker_id),
            session_id=_safe_id("session_id", session_id),
            capabilities=normalized_capabilities,
            started_at=current,
            last_heartbeat_at=current,
            expires_at=utc_after(current, ttl_seconds),
        )

    def is_expired(self, now: str) -> bool:
        return parse_utc(self.expires_at) <= parse_utc(now)

    def heartbeat(self, *, now: str, ttl_seconds: int) -> WorkerSession:
        current = parse_utc(now).isoformat()
        if self.is_expired(current):
            raise WorkerSessionRejected("worker session has expired")
        if parse_utc(current) < parse_utc(self.last_heartbeat_at):
            raise ValueError("heartbeat time must not move backwards")
        return replace(
            self,
            last_heartbeat_at=current,
            expires_at=utc_after(current, ttl_seconds),
        )


@dataclass(frozen=True, slots=True)
class TaskClaim:
    claim_id: str
    goal_id: str
    task_id: str
    worker_id: str
    session_id: str
    agent_id: str
    fencing_token: int
    acquired_at: str
    renewed_at: str
    expires_at: str
    status: ClaimStatus = ClaimStatus.ACTIVE
    finished_at: str = ""
    reason: str = ""

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        session: WorkerSession,
        agent_id: str,
        fencing_token: int,
        *,
        now: str,
        lease_seconds: int,
    ) -> TaskClaim:
        if isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token < 1:
            raise ValueError("fencing_token must be a positive integer")
        current = parse_utc(now).isoformat()
        if session.is_expired(current):
            raise WorkerSessionRejected("worker session has expired")
        return cls(
            claim_id=f"claim-{uuid4().hex[:16]}",
            goal_id=_safe_id("goal_id", goal_id),
            task_id=_safe_id("task_id", task_id),
            worker_id=session.worker_id,
            session_id=session.session_id,
            agent_id=_safe_id("agent_id", agent_id),
            fencing_token=fencing_token,
            acquired_at=current,
            renewed_at=current,
            expires_at=utc_after(current, lease_seconds),
        )

    def is_expired(self, now: str) -> bool:
        return parse_utc(self.expires_at) <= parse_utc(now)

    def renew(self, *, now: str, lease_seconds: int) -> TaskClaim:
        if self.status != ClaimStatus.ACTIVE:
            raise ValueError("only an active claim can be renewed")
        current = parse_utc(now).isoformat()
        if self.is_expired(current):
            raise StaleClaim("claim lease has expired")
        if parse_utc(current) < parse_utc(self.renewed_at):
            raise ValueError("renewal time must not move backwards")
        return replace(
            self,
            renewed_at=current,
            expires_at=utc_after(current, lease_seconds),
        )

    def finish(
        self,
        status: ClaimStatus,
        *,
        now: str,
        reason: str = "",
    ) -> TaskClaim:
        if self.status != ClaimStatus.ACTIVE:
            raise ValueError("only an active claim can become terminal")
        if status not in {
            ClaimStatus.COMMITTED,
            ClaimStatus.RELEASED,
            ClaimStatus.EXPIRED,
        }:
            raise ValueError("claim target status must be terminal")
        current = parse_utc(now).isoformat()
        if parse_utc(current) < parse_utc(self.acquired_at):
            raise ValueError("finish time must not precede acquisition")
        return replace(
            self,
            status=status,
            finished_at=current,
            reason=_bounded_reason(reason),
        )


def run_scheduler_self_test(
    database_path: str | Path | None = None,
) -> dict[str, object]:
    """Run a deterministic two-connection scheduler safety campaign."""

    names = (
        "exclusive_claim",
        "lease_renewal",
        "monotonic_reclaim",
        "stale_commit_rejected",
        "current_commit",
    )
    checks = {
        name: {"name": name, "passed": False, "detail": "not reached"}
        for name in names
    }

    def execute(path: Path) -> None:
        from .domain import (
            Artifact,
            Attempt,
            Event,
            Goal,
            GoalStatus,
            PerformanceRecord,
            Review,
            Task,
            TaskStatus,
            Verdict,
        )
        from .storage import SQLiteRepository

        at = "2026-07-16T00:00:00+00:00"
        plus_5 = "2026-07-16T00:00:05+00:00"
        plus_10 = "2026-07-16T00:00:10+00:00"
        plus_11 = "2026-07-16T00:00:11+00:00"
        first = SQLiteRepository(path)
        second = SQLiteRepository(path)
        try:
            goal = replace(
                Goal.create("Scheduler self-test", "Verify lease safety", goal_id="self-test"),
                status=GoalStatus.RUNNING,
            )
            first.save_goal(goal)
            first.save_task(
                Task.create("self-test", "claim", "analysis", "Verify ownership")
            )
            for worker_id, session_id in (
                ("worker-a", "session-a"),
                ("worker-b", "session-b"),
            ):
                first.register_worker(
                    WorkerSession.create(
                        worker_id,
                        session_id,
                        ("analysis",),
                        now=at,
                        ttl_seconds=60,
                    )
                )

            old = first.claim_task(
                "self-test", "claim", "worker-a", "session-a", "agent-a",
                now=at, lease_seconds=10,
            )
            contender = second.claim_task(
                "self-test", "claim", "worker-b", "session-b", "agent-b",
                now=plus_5, lease_seconds=10,
            )
            exclusive = old is not None and contender is None
            checks["exclusive_claim"].update(
                passed=exclusive,
                detail="one active owner across two SQLite connections",
            )
            if old is None:
                raise RuntimeError("initial claim was not acquired")

            renewed = first.renew_claim(
                old.claim_id,
                old.worker_id,
                old.session_id,
                old.fencing_token,
                now=plus_5,
                lease_seconds=5,
            )
            checks["lease_renewal"].update(
                passed=renewed.expires_at == plus_10,
                detail="exact owner renewed lease to the expected deadline",
            )

            first.reap_expired_claims(now=plus_10)
            current = second.claim_task(
                "self-test", "claim", "worker-b", "session-b", "agent-b",
                now=plus_10, lease_seconds=10,
            )
            monotonic = (
                current is not None
                and current.fencing_token > renewed.fencing_token
            )
            checks["monotonic_reclaim"].update(
                passed=monotonic,
                detail="replacement claim received a larger fencing token",
            )
            if current is None:
                raise RuntimeError("replacement claim was not acquired")

            stale_artifact = Artifact.create(
                "self-test", "claim", "agent-a", "stale result"
            )
            stale_review = Review.create(
                "self-test", "claim", 1, Verdict.PASS, 90, [], "stale"
            )
            stale_attempt = Attempt.create(
                "self-test", "claim", "agent-a", 1, 1.0,
                stale_artifact.artifact_id, stale_review.review_id,
            )
            stale_task = replace(
                first.list_tasks("self-test")[0],
                status=TaskStatus.SUCCEEDED,
                artifact_id=stale_artifact.artifact_id,
            )
            rejected = False
            try:
                first.commit_claim_outcome(
                    old,
                    stale_task,
                    stale_artifact,
                    stale_attempt,
                    stale_review,
                    PerformanceRecord("agent-a", "analysis", 1, 1, 90.0, 1.0),
                    [Event.create("self-test", "task.attempt_completed", {})],
                    now=plus_11,
                )
            except StaleClaim:
                rejected = True
            no_partial_write = (
                not first.list_artifacts("self-test", "claim")
                and not first.list_attempts("self-test", "claim")
                and not first.list_reviews("self-test", "claim")
            )
            checks["stale_commit_rejected"].update(
                passed=rejected and no_partial_write,
                detail="expired token was rejected without partial outcome records",
            )

            artifact = Artifact.create(
                "self-test", "claim", "agent-b", "current result"
            )
            review = Review.create(
                "self-test", "claim", 1, Verdict.PASS, 95, [], "current"
            )
            attempt = Attempt.create(
                "self-test", "claim", "agent-b", 1, 1.0,
                artifact.artifact_id, review.review_id,
            )
            succeeded = replace(
                second.list_tasks("self-test")[0],
                status=TaskStatus.SUCCEEDED,
                artifact_id=artifact.artifact_id,
            )
            committed = second.commit_claim_outcome(
                current,
                succeeded,
                artifact,
                attempt,
                review,
                PerformanceRecord("agent-b", "analysis", 1, 1, 95.0, 1.0),
                [Event.create("self-test", "task.attempt_completed", {})],
                now=plus_11,
            )
            current_ok = (
                committed.status == ClaimStatus.COMMITTED
                and second.list_tasks("self-test")[0].status == TaskStatus.SUCCEEDED
                and len(second.list_artifacts("self-test", "claim")) == 1
                and len(second.list_attempts("self-test", "claim")) == 1
            )
            checks["current_commit"].update(
                passed=current_ok,
                detail="current token committed one complete durable outcome",
            )
        finally:
            second.close()
            first.close()

    try:
        if database_path is None:
            with tempfile.TemporaryDirectory() as directory:
                execute(Path(directory) / "scheduler-self-test.db")
        else:
            execute(Path(database_path))
    except Exception as error:
        first_failed = next(
            (item for item in checks.values() if not item["passed"]), None
        )
        if first_failed is not None:
            first_failed["detail"] = f"{type(error).__name__}: {_bounded_reason(str(error))}"

    ordered = [checks[name] for name in names]
    return {"passed": all(item["passed"] for item in ordered), "checks": ordered}
