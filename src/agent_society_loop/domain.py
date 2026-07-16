"""Typed domain contracts for the Agent Society runtime."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Sequence
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_sensitive(value: Any, key: str = "") -> Any:
    sensitive = ("key", "token", "secret", "authorization", "password")
    if key and any(part in key.casefold() for part in sensitive):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            name: _redact_sensitive(item, str(name)) for name, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    return value


class GoalStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class ToolRisk(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class SpanStatus(str, Enum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


class PublicationStatus(str, Enum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    PUSHED = "pushed"
    PULL_REQUEST_CREATED = "pull_request_created"


class EvaluationStatus(str, Enum):
    EVALUATED = "evaluated"
    PROMOTED = "promoted"


@dataclass(frozen=True, slots=True)
class Goal:
    goal_id: str
    title: str
    description: str
    status: GoalStatus = GoalStatus.CREATED
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    failure_reason: str = ""

    @classmethod
    def create(cls, title: str, description: str, goal_id: str | None = None) -> Goal:
        if not title.strip():
            raise ValueError("goal title must not be empty")
        if not description.strip():
            raise ValueError("goal description must not be empty")
        return cls(goal_id or f"goal-{uuid4().hex[:12]}", title.strip(), description.strip())


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    goal_id: str
    task_type: str
    description: str
    assigned_role: str = "worker"
    acceptance_criteria: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    context: dict[str, Any] = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    max_attempts: int = 3
    position: int = 0
    assigned_agent_id: str | None = None
    artifact_id: str | None = None

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        task_type: str,
        description: str,
        *,
        assigned_role: str = "worker",
        acceptance_criteria: dict[str, Any] | None = None,
        dependencies: Sequence[str] = (),
        context: dict[str, Any] | None = None,
        max_attempts: int = 3,
        position: int = 0,
    ) -> Task:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        for name, value in (
            ("goal_id", goal_id),
            ("task_id", task_id),
            ("task_type", task_type),
            ("description", description),
            ("assigned_role", assigned_role),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        return cls(
            task_id=task_id,
            goal_id=goal_id,
            task_type=task_type,
            description=description.strip(),
            assigned_role=assigned_role,
            acceptance_criteria=dict(acceptance_criteria or {}),
            dependencies=tuple(dependencies),
            context=dict(context or {}),
            max_attempts=max_attempts,
            position=position,
        )


@dataclass(frozen=True, slots=True)
class Defect:
    location: str
    issue: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    goal_id: str
    task_id: str
    agent_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        agent_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        if not content.strip():
            raise ValueError("artifact content must not be empty")
        return cls(
            f"artifact-{uuid4().hex[:12]}",
            goal_id,
            task_id,
            agent_id,
            content,
            dict(metadata or {}),
        )


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    goal_id: str
    task_id: str
    agent_id: str
    attempt_no: int
    duration_ms: float
    artifact_id: str | None
    review_id: str
    error: str = ""
    completed_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        agent_id: str,
        attempt_no: int,
        duration_ms: float,
        artifact_id: str | None,
        review_id: str,
        error: str = "",
    ) -> Attempt:
        if attempt_no < 1:
            raise ValueError("attempt_no must be positive")
        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        return cls(
            f"attempt-{uuid4().hex[:12]}",
            goal_id,
            task_id,
            agent_id,
            attempt_no,
            float(duration_ms),
            artifact_id,
            review_id,
            error,
        )


@dataclass(frozen=True, slots=True)
class Review:
    review_id: str
    goal_id: str
    task_id: str
    attempt_no: int
    verdict: Verdict
    score: float
    defects: tuple[Defect, ...]
    summary: str
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        attempt_no: int,
        verdict: Verdict,
        score: float,
        defects: Iterable[Defect],
        summary: str,
    ) -> Review:
        if not 0 <= score <= 100:
            raise ValueError("review score must be between 0 and 100")
        if attempt_no < 1:
            raise ValueError("attempt_no must be positive")
        return cls(
            review_id=f"review-{uuid4().hex[:12]}",
            goal_id=goal_id,
            task_id=task_id,
            attempt_no=attempt_no,
            verdict=verdict,
            score=float(score),
            defects=tuple(defects),
            summary=summary,
        )


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    goal_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str = field(default_factory=utc_now)
    sequence: int = 0

    @classmethod
    def create(cls, goal_id: str, event_type: str, payload: dict[str, Any]) -> Event:
        if not event_type.strip():
            raise ValueError("event_type must not be empty")
        return cls(f"event-{uuid4().hex[:12]}", goal_id, event_type, dict(payload))


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    approval_id: str
    fingerprint: str
    goal_id: str
    task_id: str
    tool_name: str
    arguments: dict[str, Any]
    reason: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: str = field(default_factory=utc_now)
    decided_at: str = ""
    decided_by: str = ""

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str,
    ) -> ApprovalRequest:
        for name, value in (
            ("goal_id", goal_id),
            ("task_id", task_id),
            ("tool_name", tool_name),
            ("reason", reason),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        canonical = json.dumps(
            {
                "goal_id": goal_id,
                "task_id": task_id,
                "tool_name": tool_name,
                "arguments": arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(
            approval_id=f"approval-{fingerprint[:16]}",
            fingerprint=fingerprint,
            goal_id=goal_id,
            task_id=task_id,
            tool_name=tool_name,
            arguments=_redact_sensitive(arguments),
            reason=reason.strip(),
        )

    def resolve(self, status: ApprovalStatus, decided_by: str) -> ApprovalRequest:
        if self.status != ApprovalStatus.PENDING:
            raise ValueError("approval request is already resolved")
        if status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("approval resolution must be approved or rejected")
        if not decided_by.strip():
            raise ValueError("decided_by must not be empty")
        return replace(
            self,
            status=status,
            decided_at=utc_now(),
            decided_by=decided_by.strip(),
        )


@dataclass(frozen=True, slots=True)
class TraceSpan:
    span_id: str
    trace_id: str
    goal_id: str
    task_id: str | None
    agent_id: str | None
    parent_span_id: str | None
    kind: str
    name: str
    status: SpanStatus
    started_at: str
    ended_at: str = ""
    duration_ms: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    error_category: str = ""

    @classmethod
    def start(
        cls,
        goal_id: str,
        task_id: str | None,
        agent_id: str | None,
        kind: str,
        name: str,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> TraceSpan:
        if not goal_id.strip() or not kind.strip() or not name.strip():
            raise ValueError("goal_id, kind, and name must not be empty")
        return cls(
            span_id=f"span-{uuid4().hex[:16]}",
            trace_id=trace_id or goal_id,
            goal_id=goal_id,
            task_id=task_id,
            agent_id=agent_id,
            parent_span_id=parent_span_id,
            kind=kind.strip(),
            name=name.strip(),
            status=SpanStatus.RUNNING,
            started_at=utc_now(),
            attributes=dict(attributes or {}),
        )

    def finish(
        self,
        status: SpanStatus,
        *,
        error_category: str = "",
        attributes: dict[str, Any] | None = None,
    ) -> TraceSpan:
        if self.status != SpanStatus.RUNNING:
            raise ValueError("trace span is already finished")
        if status == SpanStatus.RUNNING:
            raise ValueError("finished trace span cannot remain running")
        ended_at = utc_now()
        started = datetime.fromisoformat(self.started_at)
        ended = datetime.fromisoformat(ended_at)
        merged = dict(self.attributes)
        merged.update(attributes or {})
        return replace(
            self,
            status=status,
            ended_at=ended_at,
            duration_ms=max(0.0, (ended - started).total_seconds() * 1000.0),
            attributes=merged,
            error_category=error_category,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    goal_id: str
    path: str
    original_exists: bool
    original_content: str
    original_sha256: str
    previous_sha256: str
    latest_sha256: str
    restored: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        path: str,
        original_exists: bool,
        original_content: str,
        original_sha256: str,
        latest_sha256: str,
    ) -> WorkspaceSnapshot:
        for name, value in (
            ("goal_id", goal_id),
            ("path", path),
            ("original_sha256", original_sha256),
            ("latest_sha256", latest_sha256),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not original_exists and original_content:
            raise ValueError("missing original file cannot have content")
        return cls(
            goal_id.strip(),
            path.strip(),
            original_exists,
            original_content,
            original_sha256.strip(),
            original_sha256.strip(),
            latest_sha256.strip(),
        )

    def advance(self, latest_sha256: str, *, restored: bool = False) -> WorkspaceSnapshot:
        if not latest_sha256.strip():
            raise ValueError("latest_sha256 must not be empty")
        return replace(
            self,
            previous_sha256=self.latest_sha256,
            latest_sha256=latest_sha256.strip(),
            restored=restored,
            updated_at=utc_now(),
        )


@dataclass(frozen=True, slots=True)
class VerificationResult:
    result_id: str
    goal_id: str
    task_id: str
    check_name: str
    command: tuple[str, ...]
    passed: bool
    exit_code: int
    duration_ms: float
    stdout: str
    stderr: str
    workspace_digest: str
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        goal_id: str,
        task_id: str,
        check_name: str,
        command: Sequence[str],
        passed: bool,
        exit_code: int,
        duration_ms: float,
        stdout: str,
        stderr: str,
        workspace_digest: str,
    ) -> VerificationResult:
        if not goal_id.strip() or not task_id.strip() or not check_name.strip():
            raise ValueError("goal_id, task_id, and check_name must not be empty")
        normalized_command = tuple(str(part) for part in command)
        if not normalized_command or any(not part for part in normalized_command):
            raise ValueError("verification command must not be empty")
        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")
        if not workspace_digest.strip():
            raise ValueError("workspace_digest must not be empty")
        return cls(
            f"verification-{uuid4().hex[:16]}",
            goal_id.strip(),
            task_id.strip(),
            check_name.strip(),
            normalized_command,
            bool(passed),
            int(exit_code),
            float(duration_ms),
            stdout,
            stderr,
            workspace_digest.strip(),
        )


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    publication_id: str
    goal_id: str
    payload_digest: str
    repository: str
    remote: str
    branch: str
    base_branch: str
    title: str
    body: str
    base_head_sha: str
    changed_paths: tuple[str, ...]
    workspace_digest: str
    check_names: tuple[str, ...]
    status: PublicationStatus = PublicationStatus.PREPARED
    commit_sha: str = ""
    pull_request_number: int = 0
    pull_request_url: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(cls, goal_id: str, payload: dict[str, Any]) -> PublicationRecord:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        required = ("repository", "remote", "branch", "base_branch", "title", "body", "base_head_sha", "workspace_digest")
        if not goal_id.strip() or any(not str(payload.get(name, "")).strip() for name in required):
            raise ValueError("publication identity fields must not be empty")
        paths = tuple(sorted(str(item) for item in payload.get("changed_paths", ())))
        checks = tuple(sorted(str(item) for item in payload.get("check_names", ())))
        if not paths or not checks:
            raise ValueError("publication requires changed paths and checks")
        return cls(
            f"publication-{digest[:16]}", goal_id, digest,
            str(payload["repository"]), str(payload["remote"]), str(payload["branch"]),
            str(payload["base_branch"]), str(payload["title"]), str(payload["body"]),
            str(payload["base_head_sha"]), paths, str(payload["workspace_digest"]), checks,
        )

    def advance(
        self,
        status: PublicationStatus,
        *,
        commit_sha: str = "",
        pull_request_number: int = 0,
        pull_request_url: str = "",
    ) -> PublicationRecord:
        order = list(PublicationStatus)
        if order.index(status) < order.index(self.status):
            raise ValueError("publication status cannot move backwards")
        if order.index(status) > order.index(self.status) + 1:
            raise ValueError("publication status cannot skip a state")
        return replace(
            self,
            status=status,
            commit_sha=commit_sha or self.commit_sha,
            pull_request_number=pull_request_number or self.pull_request_number,
            pull_request_url=pull_request_url or self.pull_request_url,
            updated_at=utc_now(),
        )


@dataclass(frozen=True, slots=True)
class AgentProfile:
    agent_id: str
    role: str
    model_id: str
    task_types: tuple[str, ...] = ("*",)
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class PerformanceRecord:
    agent_id: str
    task_type: str
    attempts: int = 0
    passes: int = 0
    avg_score: float = 0.0
    avg_duration_ms: float = 0.0
    recent_results: tuple[dict[str, Any], ...] = ()

    @property
    def success_rate(self) -> float:
        return self.passes / self.attempts if self.attempts else 0.5


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    task_type: str
    input: dict[str, Any]
    acceptance_criteria: dict[str, Any]
    context: dict[str, Any] = field(default_factory=dict)
    critical: bool = False

    @classmethod
    def create(
        cls,
        case_id: str,
        task_type: str,
        input: dict[str, Any],
        acceptance_criteria: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        critical: bool = False,
    ) -> BenchmarkCase:
        if not case_id.strip() or not task_type.strip():
            raise ValueError("benchmark case ID and task type must not be empty")
        if not acceptance_criteria:
            raise ValueError("benchmark acceptance criteria must not be empty")
        return cls(
            case_id.strip(),
            task_type.strip(),
            dict(input),
            dict(acceptance_criteria),
            dict(context or {}),
            bool(critical),
        )


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    agent_id: str
    model_id: str

    def __post_init__(self) -> None:
        if not self.agent_id.strip() or not self.model_id.strip():
            raise ValueError("candidate agent and model IDs must not be empty")


@dataclass(frozen=True, slots=True)
class CandidateExecution:
    output: Any
    duration_ms: float

    def __post_init__(self) -> None:
        if self.duration_ms < 0:
            raise ValueError("candidate duration must not be negative")


@dataclass(frozen=True, slots=True)
class CaseEvaluation:
    passed: bool
    score: float

    def __post_init__(self) -> None:
        if not 0 <= self.score <= 100:
            raise ValueError("case score must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    outcome_id: str
    run_id: str
    case_id: str
    candidate_agent_id: str
    candidate_model_id: str
    passed: bool
    score: float
    duration_ms: float
    critical: bool
    error: str = ""
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    run_id: str
    task_type: str
    benchmark_digest: str
    champion_agent_id: str
    champion_model_id: str
    challenger_agent_id: str
    challenger_model_id: str
    case_count: int
    metrics: dict[str, Any]
    recommended: bool
    failed_gates: tuple[str, ...]
    status: EvaluationStatus = EvaluationStatus.EVALUATED
    promoted_by: str = ""
    promoted_at: str = ""
    created_at: str = field(default_factory=utc_now)

    def promote(self, promoted_by: str) -> EvaluationRun:
        if not self.recommended:
            raise ValueError("evaluation is not recommended for promotion")
        if self.status != EvaluationStatus.EVALUATED:
            raise ValueError("evaluation is already promoted")
        if not promoted_by.strip():
            raise ValueError("promoted_by must not be empty")
        return replace(
            self,
            status=EvaluationStatus.PROMOTED,
            promoted_by=promoted_by.strip(),
            promoted_at=utc_now(),
        )


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    task_type: str
    champion_agent_id: str
    champion_model_id: str
    source_run_id: str
    promoted_by: str
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    agent_id: str
    total_score: float
    components: dict[str, float]
    considered: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class KnowledgeItem:
    knowledge_id: str
    title: str
    content: str
    tags: tuple[str, ...]
    created_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls, title: str, content: str, tags: Sequence[str] = ()
    ) -> KnowledgeItem:
        if not title.strip() or not content.strip():
            raise ValueError("knowledge title and content must not be empty")
        return cls(
            f"knowledge-{uuid4().hex[:12]}",
            title.strip(),
            content.strip(),
            tuple(tag.strip() for tag in tags if tag.strip()),
        )


@dataclass(frozen=True, slots=True)
class RunBudget:
    max_actions: int = 100
    min_passing_score: float = 70.0

    def __post_init__(self) -> None:
        if self.max_actions < 1:
            raise ValueError("max_actions must be positive")
        if not 0 <= self.min_passing_score <= 100:
            raise ValueError("min_passing_score must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class RunReport:
    goal_id: str
    status: GoalStatus
    tasks_total: int
    tasks_succeeded: int
    actions: int
    attempts: int
    retries: int
    artifacts: int
    reason: str = ""


_GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    GoalStatus.CREATED: frozenset({GoalStatus.PLANNING}),
    GoalStatus.PLANNING: frozenset({GoalStatus.RUNNING, GoalStatus.FAILED}),
    GoalStatus.RUNNING: frozenset(
        {GoalStatus.PAUSED, GoalStatus.SUCCEEDED, GoalStatus.FAILED, GoalStatus.BLOCKED}
    ),
    GoalStatus.PAUSED: frozenset({GoalStatus.RUNNING, GoalStatus.FAILED}),
    GoalStatus.SUCCEEDED: frozenset(),
    GoalStatus.FAILED: frozenset(),
    GoalStatus.BLOCKED: frozenset(),
}


def transition_goal(goal: Goal, target: GoalStatus, reason: str = "") -> Goal:
    if goal.status in {GoalStatus.SUCCEEDED, GoalStatus.FAILED, GoalStatus.BLOCKED}:
        raise ValueError(f"goal is terminal in state {goal.status.value}")
    if target not in _GOAL_TRANSITIONS[goal.status]:
        raise ValueError(f"invalid goal transition: {goal.status.value} -> {target.value}")
    return replace(goal, status=target, updated_at=utc_now(), failure_reason=reason)


def validate_task_graph(tasks: Sequence[Task]) -> Sequence[Task]:
    identifiers = [task.task_id for task in tasks]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("task graph contains duplicate task IDs")

    known = set(identifiers)
    for task in tasks:
        for dependency in task.dependencies:
            if dependency not in known:
                raise ValueError(
                    f"task {task.task_id} has missing dependency {dependency}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()
    dependencies = {task.task_id: task.dependencies for task in tasks}

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("task graph contains a dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for identifier in identifiers:
        visit(identifier)
    return tasks
