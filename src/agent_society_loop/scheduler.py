"""Scheduler ownership contracts and deterministic safety diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
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
