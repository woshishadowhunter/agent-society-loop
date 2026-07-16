"""SQLite persistence for goals, memories, artifacts, and audit events."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .domain import (
    AgentProfile,
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    Attempt,
    Defect,
    Event,
    Goal,
    GoalStatus,
    KnowledgeItem,
    PerformanceRecord,
    PublicationRecord,
    PublicationStatus,
    Review,
    Task,
    TaskStatus,
    SpanStatus,
    TraceSpan,
    VerificationResult,
    Verdict,
    WorkspaceSnapshot,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default, sort_keys=True)


def _load(value: str) -> dict[str, Any]:
    return json.loads(value)


class SQLiteRepository:
    """A small transactional repository with a stable, inspectable schema."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT NOT NULL,
                goal_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(goal_id, task_id),
                FOREIGN KEY(goal_id) REFERENCES goals(goal_id)
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(goal_id, task_id, attempt_no)
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                goal_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS performance (
                agent_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(agent_id, task_type)
            );
            CREATE TABLE IF NOT EXISTS knowledge (
                knowledge_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                fingerprint TEXT UNIQUE NOT NULL,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS approvals_goal_idx
                ON approvals(goal_id, task_id);
            CREATE TABLE IF NOT EXISTS trace_spans (
                span_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                goal_id TEXT NOT NULL,
                task_id TEXT,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS trace_spans_goal_idx
                ON trace_spans(goal_id, task_id);
            CREATE TABLE IF NOT EXISTS workspace_snapshots (
                goal_id TEXT NOT NULL,
                path TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY(goal_id, path)
            );
            CREATE TABLE IF NOT EXISTS verification_results (
                result_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                check_name TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS verification_results_goal_idx
                ON verification_results(goal_id, task_id, check_name);
            CREATE TABLE IF NOT EXISTS publication_records (
                goal_id TEXT PRIMARY KEY,
                publication_id TEXT UNIQUE NOT NULL,
                payload_digest TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def save_goal(self, goal: Goal) -> None:
        self.connection.execute(
            "INSERT INTO goals(goal_id, payload) VALUES (?, ?) "
            "ON CONFLICT(goal_id) DO UPDATE SET payload=excluded.payload",
            (goal.goal_id, _dump(asdict(goal))),
        )
        self.connection.commit()

    def get_goal(self, goal_id: str) -> Goal | None:
        row = self.connection.execute(
            "SELECT payload FROM goals WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["status"] = GoalStatus(data["status"])
        return Goal(**data)

    def save_tasks(self, tasks: Iterable[Task]) -> None:
        with self.connection:
            for task in tasks:
                self.connection.execute(
                    "INSERT INTO tasks(task_id, goal_id, position, payload) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(goal_id, task_id) DO UPDATE SET "
                    "position=excluded.position, payload=excluded.payload",
                    (task.task_id, task.goal_id, task.position, _dump(asdict(task))),
                )

    def save_task(self, task: Task) -> None:
        self.save_tasks([task])

    def list_tasks(self, goal_id: str) -> list[Task]:
        rows = self.connection.execute(
            "SELECT payload FROM tasks WHERE goal_id=? ORDER BY position, task_id",
            (goal_id,),
        ).fetchall()
        tasks = []
        for row in rows:
            data = _load(row["payload"])
            data["dependencies"] = tuple(data["dependencies"])
            data["status"] = TaskStatus(data["status"])
            tasks.append(Task(**data))
        return tasks

    def save_artifact(self, artifact: Artifact) -> None:
        self.connection.execute(
            "INSERT INTO artifacts(artifact_id, goal_id, task_id, payload) VALUES (?, ?, ?, ?)",
            (artifact.artifact_id, artifact.goal_id, artifact.task_id, _dump(asdict(artifact))),
        )
        self.connection.commit()

    def list_artifacts(self, goal_id: str, task_id: str | None = None) -> list[Artifact]:
        if task_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM artifacts WHERE goal_id=? ORDER BY rowid", (goal_id,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM artifacts WHERE goal_id=? AND task_id=? ORDER BY rowid",
                (goal_id, task_id),
            ).fetchall()
        return [Artifact(**_load(row["payload"])) for row in rows]

    def save_review(self, review: Review) -> None:
        self.connection.execute(
            "INSERT INTO reviews(review_id, goal_id, task_id, attempt_no, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                review.review_id,
                review.goal_id,
                review.task_id,
                review.attempt_no,
                _dump(asdict(review)),
            ),
        )
        self.connection.commit()

    def list_reviews(self, goal_id: str, task_id: str | None = None) -> list[Review]:
        query = "SELECT payload FROM reviews WHERE goal_id=?"
        parameters: list[Any] = [goal_id]
        if task_id is not None:
            query += " AND task_id=?"
            parameters.append(task_id)
        query += " ORDER BY attempt_no, rowid"
        rows = self.connection.execute(query, parameters).fetchall()
        reviews = []
        for row in rows:
            data = _load(row["payload"])
            data["verdict"] = Verdict(data["verdict"])
            data["defects"] = tuple(Defect(**defect) for defect in data["defects"])
            reviews.append(Review(**data))
        return reviews

    def list_attempts(self, goal_id: str, task_id: str | None = None) -> list[Attempt]:
        query = "SELECT payload FROM attempts WHERE goal_id=?"
        parameters: list[Any] = [goal_id]
        if task_id is not None:
            query += " AND task_id=?"
            parameters.append(task_id)
        query += " ORDER BY attempt_no, rowid"
        rows = self.connection.execute(query, parameters).fetchall()
        return [Attempt(**_load(row["payload"])) for row in rows]

    def save_attempt_outcome(
        self,
        attempt: Attempt,
        review: Review,
        performance: PerformanceRecord,
        event: Event,
    ) -> None:
        """Commit the reviewed outcome and its audit evidence as one unit."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO attempts(attempt_id, goal_id, task_id, attempt_no, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    attempt.attempt_id,
                    attempt.goal_id,
                    attempt.task_id,
                    attempt.attempt_no,
                    _dump(asdict(attempt)),
                ),
            )
            self.connection.execute(
                "INSERT INTO reviews(review_id, goal_id, task_id, attempt_no, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    review.review_id,
                    review.goal_id,
                    review.task_id,
                    review.attempt_no,
                    _dump(asdict(review)),
                ),
            )
            self.connection.execute(
                "INSERT INTO performance(agent_id, task_type, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(agent_id, task_type) DO UPDATE SET payload=excluded.payload",
                (
                    performance.agent_id,
                    performance.task_type,
                    _dump(asdict(performance)),
                ),
            )
            self.connection.execute(
                "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
                (event.event_id, event.goal_id, _dump(asdict(event))),
            )

    def append_event(self, event: Event) -> Event:
        cursor = self.connection.execute(
            "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
            (event.event_id, event.goal_id, _dump(asdict(event))),
        )
        self.connection.commit()
        return replace(event, sequence=int(cursor.lastrowid))

    def list_events(self, goal_id: str) -> list[Event]:
        rows = self.connection.execute(
            "SELECT sequence, payload FROM events WHERE goal_id=? ORDER BY sequence",
            (goal_id,),
        ).fetchall()
        return [
            Event(**(_load(row["payload"]) | {"sequence": row["sequence"]}))
            for row in rows
        ]

    def save_agent(self, agent: AgentProfile) -> None:
        self.connection.execute(
            "INSERT INTO agents(agent_id, payload) VALUES (?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET payload=excluded.payload",
            (agent.agent_id, _dump(asdict(agent))),
        )
        self.connection.commit()

    def list_agents(self) -> list[AgentProfile]:
        rows = self.connection.execute(
            "SELECT payload FROM agents ORDER BY agent_id"
        ).fetchall()
        result = []
        for row in rows:
            data = _load(row["payload"])
            data["task_types"] = tuple(data["task_types"])
            result.append(AgentProfile(**data))
        return result

    def save_performance(self, performance: PerformanceRecord) -> None:
        self.connection.execute(
            "INSERT INTO performance(agent_id, task_type, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(agent_id, task_type) DO UPDATE SET payload=excluded.payload",
            (performance.agent_id, performance.task_type, _dump(asdict(performance))),
        )
        self.connection.commit()

    def get_performance(self, agent_id: str, task_type: str) -> PerformanceRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM performance WHERE agent_id=? AND task_type=?",
            (agent_id, task_type),
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["recent_results"] = tuple(data["recent_results"])
        return PerformanceRecord(**data)

    def list_performance(self) -> list[PerformanceRecord]:
        rows = self.connection.execute(
            "SELECT payload FROM performance ORDER BY agent_id, task_type"
        ).fetchall()
        records = []
        for row in rows:
            data = _load(row["payload"])
            data["recent_results"] = tuple(data["recent_results"])
            records.append(PerformanceRecord(**data))
        return records

    def save_knowledge(self, item: KnowledgeItem) -> None:
        self.connection.execute(
            "INSERT INTO knowledge(knowledge_id, payload) VALUES (?, ?)",
            (item.knowledge_id, _dump(asdict(item))),
        )
        self.connection.commit()

    def list_knowledge(self) -> list[KnowledgeItem]:
        rows = self.connection.execute(
            "SELECT payload FROM knowledge ORDER BY rowid"
        ).fetchall()
        result = []
        for row in rows:
            data = _load(row["payload"])
            data["tags"] = tuple(data["tags"])
            result.append(KnowledgeItem(**data))
        return result

    def save_approval(self, approval: ApprovalRequest) -> None:
        self.connection.execute(
            "INSERT INTO approvals(approval_id, fingerprint, goal_id, task_id, payload) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(approval_id) DO UPDATE SET payload=excluded.payload",
            (
                approval.approval_id,
                approval.fingerprint,
                approval.goal_id,
                approval.task_id,
                _dump(asdict(approval)),
            ),
        )
        self.connection.commit()

    def save_approval_resolution(
        self,
        approval: ApprovalRequest,
        events: Iterable[Event],
        goal: Goal | None = None,
    ) -> None:
        """Commit an approval decision, lifecycle state, and audit events atomically."""
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE approvals SET payload=? WHERE approval_id=?",
                (_dump(asdict(approval)), approval.approval_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"approval not found: {approval.approval_id}")
            if goal is not None:
                self.connection.execute(
                    "UPDATE goals SET payload=? WHERE goal_id=?",
                    (_dump(asdict(goal)), goal.goal_id),
                )
            for event in events:
                self.connection.execute(
                    "INSERT INTO events(event_id, goal_id, payload) VALUES (?, ?, ?)",
                    (event.event_id, event.goal_id, _dump(asdict(event))),
                )

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        row = self.connection.execute(
            "SELECT payload FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["status"] = ApprovalStatus(data["status"])
        return ApprovalRequest(**data)

    def get_approval_by_fingerprint(self, fingerprint: str) -> ApprovalRequest | None:
        row = self.connection.execute(
            "SELECT payload FROM approvals WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["status"] = ApprovalStatus(data["status"])
        return ApprovalRequest(**data)

    def list_approvals(self, goal_id: str | None = None) -> list[ApprovalRequest]:
        if goal_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM approvals ORDER BY rowid"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM approvals WHERE goal_id=? ORDER BY rowid",
                (goal_id,),
            ).fetchall()
        result = []
        for row in rows:
            data = _load(row["payload"])
            data["status"] = ApprovalStatus(data["status"])
            result.append(ApprovalRequest(**data))
        return result

    def save_span(self, span: TraceSpan) -> None:
        self.connection.execute(
            "INSERT INTO trace_spans(span_id, trace_id, goal_id, task_id, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (span.span_id, span.trace_id, span.goal_id, span.task_id, _dump(asdict(span))),
        )
        self.connection.commit()

    def list_spans(self, goal_id: str) -> list[TraceSpan]:
        rows = self.connection.execute(
            "SELECT payload FROM trace_spans WHERE goal_id=? ORDER BY rowid",
            (goal_id,),
        ).fetchall()
        result = []
        for row in rows:
            data = _load(row["payload"])
            data["status"] = SpanStatus(data["status"])
            result.append(TraceSpan(**data))
        return result

    def save_workspace_snapshot(self, snapshot: WorkspaceSnapshot) -> None:
        existing = self.get_workspace_snapshot(snapshot.goal_id, snapshot.path)
        if existing is not None and (
            existing.original_exists != snapshot.original_exists
            or existing.original_content != snapshot.original_content
            or existing.original_sha256 != snapshot.original_sha256
            or existing.created_at != snapshot.created_at
        ):
            raise ValueError("workspace snapshot original recovery point cannot change")
        self.connection.execute(
            "INSERT INTO workspace_snapshots(goal_id, path, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(goal_id, path) DO UPDATE SET payload=excluded.payload",
            (snapshot.goal_id, snapshot.path, _dump(asdict(snapshot))),
        )
        self.connection.commit()

    def get_workspace_snapshot(
        self, goal_id: str, path: str
    ) -> WorkspaceSnapshot | None:
        row = self.connection.execute(
            "SELECT payload FROM workspace_snapshots WHERE goal_id=? AND path=?",
            (goal_id, path),
        ).fetchone()
        return WorkspaceSnapshot(**_load(row["payload"])) if row is not None else None

    def list_workspace_snapshots(self, goal_id: str) -> list[WorkspaceSnapshot]:
        rows = self.connection.execute(
            "SELECT payload FROM workspace_snapshots WHERE goal_id=? ORDER BY path",
            (goal_id,),
        ).fetchall()
        return [WorkspaceSnapshot(**_load(row["payload"])) for row in rows]

    def save_verification_result(self, result: VerificationResult) -> None:
        self.connection.execute(
            "INSERT INTO verification_results(result_id, goal_id, task_id, check_name, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                result.result_id,
                result.goal_id,
                result.task_id,
                result.check_name,
                _dump(asdict(result)),
            ),
        )
        self.connection.commit()

    def list_verification_results(self, goal_id: str) -> list[VerificationResult]:
        rows = self.connection.execute(
            "SELECT payload FROM verification_results WHERE goal_id=? ORDER BY rowid",
            (goal_id,),
        ).fetchall()
        results = []
        for row in rows:
            data = _load(row["payload"])
            data["command"] = tuple(data["command"])
            results.append(VerificationResult(**data))
        return results

    def save_publication(self, publication: PublicationRecord) -> None:
        existing = self.get_publication(publication.goal_id)
        if existing is not None and existing.payload_digest != publication.payload_digest:
            raise ValueError("publication payload cannot change for a goal")
        if existing is not None and list(PublicationStatus).index(publication.status) < list(PublicationStatus).index(existing.status):
            raise ValueError("publication status cannot move backwards")
        self.connection.execute(
            "INSERT INTO publication_records(goal_id, publication_id, payload_digest, payload) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(goal_id) DO UPDATE SET payload=excluded.payload",
            (publication.goal_id, publication.publication_id, publication.payload_digest, _dump(asdict(publication))),
        )
        self.connection.commit()

    def get_publication(self, goal_id: str) -> PublicationRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM publication_records WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if row is None:
            return None
        data = _load(row["payload"])
        data["status"] = PublicationStatus(data["status"])
        data["changed_paths"] = tuple(data["changed_paths"])
        data["check_names"] = tuple(data["check_names"])
        return PublicationRecord(**data)
