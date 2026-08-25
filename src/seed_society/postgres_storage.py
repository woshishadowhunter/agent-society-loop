"""PostgreSQL execution-plane persistence for multi-host worker services."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass, replace
from datetime import timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .domain import (
    AgentGenome,
    AgentProfile,
    AgentSelfModel,
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    Attempt,
    Defect,
    DeploymentRecord,
    Event,
    ExperienceRecord,
    Goal,
    GoalStatus,
    KnowledgeItem,
    OutboxMessage,
    OutboxStatus,
    PerformanceRecord,
    Review,
    Task,
    TaskStatus,
    SpanStatus,
    TraceSpan,
    Verdict,
    transition_goal,
)
from .scheduler import (
    ClaimedTask,
    ClaimStatus,
    StaleClaim,
    TaskClaim,
    WorkerSession,
    WorkerSessionRejected,
    parse_utc,
)


_SCHEMA = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        return json.loads(value)
    raise TypeError("database payload must be a JSON object")


def _goal(value: Any) -> Goal:
    data = _payload(value)
    data["status"] = GoalStatus(data["status"])
    return Goal(**data)


def _task(value: Any) -> Task:
    data = _payload(value)
    data["dependencies"] = tuple(data["dependencies"])
    data["status"] = TaskStatus(data["status"])
    return Task(**data)


def _review(value: Any) -> Review:
    data = _payload(value)
    data["verdict"] = Verdict(data["verdict"])
    data["defects"] = tuple(Defect(**item) for item in data["defects"])
    return Review(**data)


def _approval(value: Any) -> ApprovalRequest:
    data = _payload(value)
    data["status"] = ApprovalStatus(data["status"])
    return ApprovalRequest(**data)


def _span(value: Any) -> TraceSpan:
    data = _payload(value)
    data["status"] = SpanStatus(data["status"])
    return TraceSpan(**data)


def _worker(value: Any) -> WorkerSession:
    data = _payload(value)
    data["capabilities"] = tuple(data["capabilities"])
    return WorkerSession(**data)


def _claim(value: Any) -> TaskClaim:
    data = _payload(value)
    data["status"] = ClaimStatus(data["status"])
    return TaskClaim(**data)


def _outbox(value: Any) -> OutboxMessage:
    data = _payload(value)
    data["status"] = OutboxStatus(data["status"])
    return OutboxMessage(**data)


def _genome(value: Any) -> AgentGenome:
    data = _payload(value)
    self_model = data["self_model"]
    if isinstance(self_model, dict):
        self_model["success_signals"] = tuple(self_model.get("success_signals", ()))
        self_model["failure_modes"] = tuple(self_model.get("failure_modes", ()))
        data["self_model"] = AgentSelfModel(**self_model)
    data["traits"] = tuple(data.get("traits", ()))
    data["tool_profile"] = tuple(data.get("tool_profile", ()))
    data["parents"] = tuple(data.get("parents", ()))
    return AgentGenome(**data)


def _experience(value: Any) -> ExperienceRecord:
    data = _payload(value)
    data["lessons"] = tuple(data.get("lessons", ()))
    data["tags"] = tuple(data.get("tags", ()))
    return ExperienceRecord(**data)


class PostgreSQLRepository:
    """One transactional authority for distributed worker execution state."""

    def __init__(self, database_url: str, *, schema: str = "agent_society"):
        if not _SCHEMA.fullmatch(schema):
            raise ValueError("PostgreSQL schema must be a safe identifier")
        try:
            import psycopg
            from psycopg import sql
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as error:
            raise RuntimeError(
                "PostgreSQL support requires the postgres extra: "
                "pip install 'seed-society[postgres]'"
            ) from error
        self.database_url = database_url
        self.schema = schema
        self._sql = sql
        self._Jsonb = Jsonb
        self.connection = psycopg.connect(
            database_url, autocommit=True, row_factory=dict_row
        )
        self.connection.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
        )
        self.connection.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
        )
        self._create_schema()

    def _j(self, value: Any):
        return self._Jsonb(_jsonable(value))

    def scheduler_now(self) -> str:
        row = self.connection.execute(
            "SELECT clock_timestamp() AS now"
        ).fetchone()
        return row["now"].astimezone(timezone.utc).isoformat()

    def operational_counts(self, now: str) -> dict[str, int]:
        workers = self.connection.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE expires_at<=%s) AS expired
               FROM scheduler_workers""",
            (now,),
        ).fetchone()
        claims = self.connection.execute(
            """SELECT COUNT(*) AS active,
                      COUNT(*) FILTER (WHERE expires_at<=%s) AS expired_active
               FROM task_claims WHERE status='active'""",
            (now,),
        ).fetchone()
        approvals = self.connection.execute(
            """SELECT COUNT(*) AS pending FROM approvals
               WHERE payload->>'status'='pending'"""
        ).fetchone()
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM outbox_messages GROUP BY status"
        ).fetchall()
        outbox = {row["status"]: int(row["count"]) for row in rows}
        return {
            "workers_total": int(workers["total"]),
            "workers_expired": int(workers["expired"]),
            "claims_active": int(claims["active"]),
            "claims_expired_active": int(claims["expired_active"]),
            "approvals_pending": int(approvals["pending"]),
            "outbox_pending": outbox.get("pending", 0),
            "outbox_delivering": outbox.get("delivering", 0),
            "outbox_delivered": outbox.get("delivered", 0),
            "outbox_failed": outbox.get("failed", 0),
        }

    def _create_schema(self) -> None:
        statements = (
            """CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS tasks (
                goal_id TEXT NOT NULL REFERENCES goals(goal_id), task_id TEXT NOT NULL,
                position INTEGER NOT NULL, task_type TEXT NOT NULL, status TEXT NOT NULL,
                assigned_agent_id TEXT, payload JSONB NOT NULL,
                PRIMARY KEY(goal_id, task_id))""",
            """CREATE INDEX IF NOT EXISTS tasks_ready_idx
                ON tasks(status, task_type, goal_id, position, task_id)""",
            """CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, task_id TEXT NOT NULL,
                payload JSONB NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS reviews (
                review_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL, payload JSONB NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, task_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL, payload JSONB NOT NULL,
                UNIQUE(goal_id, task_id, attempt_no))""",
            """CREATE TABLE IF NOT EXISTS events (
                sequence BIGSERIAL PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                goal_id TEXT NOT NULL, event_type TEXT NOT NULL, payload JSONB NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS events_goal_idx ON events(goal_id, sequence)""",
            """CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
                goal_id TEXT NOT NULL, task_id TEXT NOT NULL, payload JSONB NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS approvals_goal_idx
                ON approvals(goal_id, approval_id)""",
            """CREATE INDEX IF NOT EXISTS approvals_status_idx
                ON approvals ((payload->>'status'))""",
            """CREATE TABLE IF NOT EXISTS trace_spans (
                sequence BIGSERIAL PRIMARY KEY, span_id TEXT UNIQUE NOT NULL,
                goal_id TEXT NOT NULL, payload JSONB NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS trace_spans_goal_idx
                ON trace_spans(goal_id, sequence)""",
            """CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY, payload JSONB NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS performance (
                agent_id TEXT NOT NULL, task_type TEXT NOT NULL, payload JSONB NOT NULL,
                PRIMARY KEY(agent_id, task_type))""",
            """CREATE TABLE IF NOT EXISTS agent_genomes (
                agent_id TEXT PRIMARY KEY, payload JSONB NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS experience_records (
                experience_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
                task_type TEXT NOT NULL, goal_id TEXT NOT NULL, payload JSONB NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS experience_records_agent_idx
                ON experience_records(agent_id, task_type)""",
            """CREATE INDEX IF NOT EXISTS experience_records_goal_idx
                ON experience_records(goal_id)""",
            """CREATE TABLE IF NOT EXISTS knowledge (
                knowledge_id TEXT PRIMARY KEY, payload JSONB NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS deployments (
                task_type TEXT PRIMARY KEY, payload JSONB NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS scheduler_workers (
                worker_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS task_claim_fences (
                goal_id TEXT NOT NULL, task_id TEXT NOT NULL, last_token BIGINT NOT NULL,
                PRIMARY KEY(goal_id, task_id))""",
            """CREATE TABLE IF NOT EXISTS task_claims (
                claim_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, task_id TEXT NOT NULL,
                worker_id TEXT NOT NULL, session_id TEXT NOT NULL,
                fencing_token BIGINT NOT NULL, status TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL,
                UNIQUE(goal_id, task_id, fencing_token))""",
            """CREATE UNIQUE INDEX IF NOT EXISTS task_claims_one_active_idx
                ON task_claims(goal_id, task_id) WHERE status = 'active'""",
            """CREATE INDEX IF NOT EXISTS task_claims_expiry_idx
                ON task_claims(status, expires_at)""",
            """CREATE TABLE IF NOT EXISTS outbox_messages (
                message_id TEXT PRIMARY KEY, topic TEXT NOT NULL,
                idempotency_key TEXT NOT NULL, status TEXT NOT NULL,
                available_at TIMESTAMPTZ NOT NULL,
                claim_expires_at TIMESTAMPTZ,
                delivery_token BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                payload JSONB NOT NULL, UNIQUE(topic, idempotency_key))""",
            """CREATE INDEX IF NOT EXISTS outbox_delivery_idx
                ON outbox_messages(status, available_at, claim_expires_at, created_at)""",
        )
        with self.connection.transaction():
            for statement in statements:
                self.connection.execute(statement)

    def close(self) -> None:
        self.connection.close()

    def drop_schema(self) -> None:
        self.connection.execute(
            self._sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                self._sql.Identifier(self.schema)
            )
        )

    def _enqueue_outbox_locked(self, message: OutboxMessage) -> OutboxMessage:
        row = self.connection.execute(
            """INSERT INTO outbox_messages(
                   message_id, topic, idempotency_key, status, available_at,
                   claim_expires_at, delivery_token, created_at, payload)
               VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, %s)
               ON CONFLICT(topic, idempotency_key) DO NOTHING
               RETURNING payload""",
            (
                message.message_id,
                message.topic,
                message.idempotency_key,
                message.status.value,
                message.available_at,
                message.delivery_token,
                message.created_at,
                self._j(message),
            ),
        ).fetchone()
        if row is not None:
            return message
        row = self.connection.execute(
            """SELECT payload FROM outbox_messages
               WHERE topic=%s AND idempotency_key=%s FOR UPDATE""",
            (message.topic, message.idempotency_key),
        ).fetchone()
        existing = _outbox(row["payload"])
        if (
            existing.payload_digest != message.payload_digest
            or existing.max_attempts != message.max_attempts
        ):
            raise ValueError(
                "outbox idempotency payload does not match existing message"
            )
        return existing

    def enqueue_outbox(self, message: OutboxMessage) -> OutboxMessage:
        with self.connection.transaction():
            return self._enqueue_outbox_locked(message)

    def claim_outbox(
        self,
        worker_id: str,
        *,
        topic: str | None = None,
        now: str,
        lease_seconds: int,
    ) -> OutboxMessage | None:
        if topic is not None and not str(topic).strip():
            raise ValueError("outbox topic must not be empty")
        topic_filter = "topic=%s AND " if topic is not None else ""
        parameters = (
            (str(topic).strip(), now, now)
            if topic is not None
            else (now, now)
        )
        with self.connection.transaction():
            while True:
                row = self.connection.execute(
                    f"""SELECT payload FROM outbox_messages
                        WHERE {topic_filter}(
                            (status='pending' AND available_at<=%s)
                            OR (status='delivering' AND claim_expires_at<=%s)
                        )
                        ORDER BY created_at, message_id
                        FOR UPDATE SKIP LOCKED LIMIT 1""",
                    parameters,
                ).fetchone()
                if row is None:
                    return None
                current = _outbox(row["payload"])
                if (
                    current.status == OutboxStatus.DELIVERING
                    and current.attempt_count >= current.max_attempts
                ):
                    exhausted = current.expire(now=now)
                    self.connection.execute(
                        """UPDATE outbox_messages
                           SET status=%s, claim_expires_at=NULL, payload=%s
                           WHERE message_id=%s AND status='delivering'
                             AND delivery_token=%s""",
                        (
                            exhausted.status.value,
                            self._j(exhausted),
                            exhausted.message_id,
                            current.delivery_token,
                        ),
                    )
                    continue
                break
            claimed = current.claim(
                worker_id, now=now, lease_seconds=lease_seconds
            )
            updated = self.connection.execute(
                """UPDATE outbox_messages
                   SET status=%s, available_at=%s, claim_expires_at=%s,
                       delivery_token=%s, payload=%s
                   WHERE message_id=%s AND status=%s AND delivery_token=%s
                   RETURNING message_id""",
                (
                    claimed.status.value,
                    claimed.available_at,
                    claimed.claim_expires_at,
                    claimed.delivery_token,
                    self._j(claimed),
                    claimed.message_id,
                    current.status.value,
                    current.delivery_token,
                ),
            ).fetchone()
            if updated is None:
                raise ValueError("outbox delivery ownership is stale")
            return claimed

    def renew_outbox(
        self,
        message_id: str,
        worker_id: str,
        delivery_token: int,
        *,
        now: str,
        lease_seconds: int,
    ) -> OutboxMessage:
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT payload FROM outbox_messages "
                "WHERE message_id=%s FOR UPDATE",
                (message_id,),
            ).fetchone()
            if row is None:
                raise ValueError("outbox message not found")
            renewed = _outbox(row["payload"]).renew(
                worker_id,
                delivery_token,
                now=now,
                lease_seconds=lease_seconds,
            )
            row = self.connection.execute(
                """UPDATE outbox_messages SET claim_expires_at=%s, payload=%s
                   WHERE message_id=%s AND status='delivering'
                     AND delivery_token=%s
                     AND payload->>'claimed_by'=%s
                   RETURNING message_id""",
                (
                    renewed.claim_expires_at,
                    self._j(renewed),
                    message_id,
                    delivery_token,
                    worker_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("outbox delivery ownership is stale")
            return renewed

    def complete_outbox(
        self,
        message_id: str,
        worker_id: str,
        delivery_token: int,
        *,
        now: str,
        error: str = "",
        retry_seconds: int = 0,
    ) -> OutboxMessage:
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT payload FROM outbox_messages WHERE message_id=%s FOR UPDATE",
                (message_id,),
            ).fetchone()
            if row is None:
                raise ValueError("outbox message not found")
            current = _outbox(row["payload"])
            updated = (
                current.fail(
                    worker_id,
                    delivery_token,
                    now=now,
                    error=error,
                    retry_seconds=retry_seconds,
                )
                if error
                else current.deliver(worker_id, delivery_token, now=now)
            )
            row = self.connection.execute(
                """UPDATE outbox_messages
                   SET status=%s, available_at=%s, claim_expires_at=%s,
                       delivery_token=%s, payload=%s
                   WHERE message_id=%s AND status='delivering'
                     AND delivery_token=%s
                   RETURNING message_id""",
                (
                    updated.status.value,
                    updated.available_at,
                    updated.claim_expires_at or None,
                    updated.delivery_token,
                    self._j(updated),
                    message_id,
                    delivery_token,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("outbox delivery ownership is stale")
            return updated

    def list_outbox(
        self,
        *,
        status: OutboxStatus | None = None,
        limit: int | None = None,
    ) -> list[OutboxMessage]:
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("outbox list limit must be between 1 and 10000")
        query = "SELECT payload FROM outbox_messages"
        parameters: list[Any] = []
        if status is not None:
            query += " WHERE status=%s"
            parameters.append(OutboxStatus(status).value)
        query += " ORDER BY created_at, message_id"
        if limit is not None:
            query += " LIMIT %s"
            parameters.append(limit)
        rows = self.connection.execute(query, parameters).fetchall()
        return [_outbox(row["payload"]) for row in rows]

    def purge_outbox(self, *, before: str, limit: int) -> int:
        cutoff = parse_utc(before).isoformat()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("outbox purge limit must be between 1 and 10000")
        with self.connection.transaction():
            cursor = self.connection.execute(
                """DELETE FROM outbox_messages WHERE message_id IN (
                       SELECT message_id FROM outbox_messages
                       WHERE status IN ('delivered', 'failed')
                         AND (payload->>'updated_at')::timestamptz<%s
                       ORDER BY created_at, message_id LIMIT %s
                   )""",
                (cutoff, limit),
            )
        return cursor.rowcount

    def save_goal(self, goal: Goal) -> None:
        self.connection.execute(
            """INSERT INTO goals(goal_id, status, created_at, payload)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT(goal_id) DO UPDATE SET status=excluded.status,
               created_at=excluded.created_at, payload=excluded.payload""",
            (goal.goal_id, goal.status.value, goal.created_at, self._j(goal)),
        )

    def get_goal(self, goal_id: str) -> Goal | None:
        row = self.connection.execute(
            "SELECT payload FROM goals WHERE goal_id=%s", (goal_id,)
        ).fetchone()
        return None if row is None else _goal(row["payload"])

    def _reject_active_claim(self, goal_id: str, task_id: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM task_claims WHERE goal_id=%s AND task_id=%s AND status='active'",
            (goal_id, task_id),
        ).fetchone()
        if row is not None:
            raise StaleClaim("active scheduler claim requires a fenced outcome mutation")

    def save_tasks(self, tasks: Iterable[Task]) -> None:
        with self.connection.transaction():
            for task in tasks:
                self._reject_active_claim(task.goal_id, task.task_id)
                self.connection.execute(
                    """INSERT INTO tasks(
                           goal_id, task_id, position, task_type, status,
                           assigned_agent_id, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT(goal_id, task_id) DO UPDATE SET
                           position=excluded.position, task_type=excluded.task_type,
                           status=excluded.status,
                           assigned_agent_id=excluded.assigned_agent_id,
                           payload=excluded.payload""",
                    (
                        task.goal_id, task.task_id, task.position, task.task_type,
                        task.status.value, task.assigned_agent_id, self._j(task),
                    ),
                )

    def save_task(self, task: Task) -> None:
        self.save_tasks((task,))

    def list_tasks(self, goal_id: str) -> list[Task]:
        rows = self.connection.execute(
            "SELECT payload FROM tasks WHERE goal_id=%s ORDER BY position, task_id",
            (goal_id,),
        ).fetchall()
        return [_task(row["payload"]) for row in rows]

    def append_event(self, event: Event) -> Event:
        row = self.connection.execute(
            """INSERT INTO events(event_id, goal_id, event_type, payload)
               VALUES (%s, %s, %s, %s) RETURNING sequence""",
            (event.event_id, event.goal_id, event.event_type, self._j(event)),
        ).fetchone()
        return replace(event, sequence=int(row["sequence"]))

    def list_events(self, goal_id: str) -> list[Event]:
        rows = self.connection.execute(
            "SELECT sequence, payload FROM events WHERE goal_id=%s ORDER BY sequence",
            (goal_id,),
        ).fetchall()
        return [
            Event(**(_payload(row["payload"]) | {"sequence": int(row["sequence"])}))
            for row in rows
        ]

    def list_artifacts(self, goal_id: str, task_id: str | None = None) -> list[Artifact]:
        query = "SELECT payload FROM artifacts WHERE goal_id=%s"
        params: list[Any] = [goal_id]
        if task_id is not None:
            query += " AND task_id=%s"
            params.append(task_id)
        query += " ORDER BY artifact_id"
        rows = self.connection.execute(query, params).fetchall()
        return [Artifact(**_payload(row["payload"])) for row in rows]

    def list_reviews(self, goal_id: str, task_id: str | None = None) -> list[Review]:
        query = "SELECT payload FROM reviews WHERE goal_id=%s"
        params: list[Any] = [goal_id]
        if task_id is not None:
            query += " AND task_id=%s"
            params.append(task_id)
        query += " ORDER BY attempt_no, review_id"
        rows = self.connection.execute(query, params).fetchall()
        return [_review(row["payload"]) for row in rows]

    def list_attempts(self, goal_id: str, task_id: str | None = None) -> list[Attempt]:
        query = "SELECT payload FROM attempts WHERE goal_id=%s"
        params: list[Any] = [goal_id]
        if task_id is not None:
            query += " AND task_id=%s"
            params.append(task_id)
        query += " ORDER BY attempt_no, attempt_id"
        rows = self.connection.execute(query, params).fetchall()
        return [Attempt(**_payload(row["payload"])) for row in rows]

    def save_agent(self, agent: AgentProfile) -> None:
        self.connection.execute(
            """INSERT INTO agents(agent_id, payload) VALUES (%s, %s)
               ON CONFLICT(agent_id) DO UPDATE SET payload=excluded.payload""",
            (agent.agent_id, self._j(agent)),
        )

    def list_agents(self) -> list[AgentProfile]:
        rows = self.connection.execute(
            "SELECT payload FROM agents ORDER BY agent_id"
        ).fetchall()
        result = []
        for row in rows:
            data = _payload(row["payload"])
            data["task_types"] = tuple(data["task_types"])
            result.append(AgentProfile(**data))
        return result

    def save_performance(self, record: PerformanceRecord) -> None:
        self.connection.execute(
            """INSERT INTO performance(agent_id, task_type, payload) VALUES (%s, %s, %s)
               ON CONFLICT(agent_id, task_type) DO UPDATE SET payload=excluded.payload""",
            (record.agent_id, record.task_type, self._j(record)),
        )

    def get_performance(self, agent_id: str, task_type: str) -> PerformanceRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM performance WHERE agent_id=%s AND task_type=%s",
            (agent_id, task_type),
        ).fetchone()
        if row is None:
            return None
        data = _payload(row["payload"])
        data["recent_results"] = tuple(data["recent_results"])
        return PerformanceRecord(**data)

    def list_performance(self) -> list[PerformanceRecord]:
        rows = self.connection.execute(
            "SELECT payload FROM performance ORDER BY agent_id, task_type"
        ).fetchall()
        result = []
        for row in rows:
            data = _payload(row["payload"])
            data["recent_results"] = tuple(data["recent_results"])
            result.append(PerformanceRecord(**data))
        return result

    def save_agent_genome(self, genome: AgentGenome) -> None:
        self.connection.execute(
            """INSERT INTO agent_genomes(agent_id, payload) VALUES (%s, %s)
               ON CONFLICT(agent_id) DO UPDATE SET payload=excluded.payload""",
            (genome.agent_id, self._j(genome)),
        )

    def get_agent_genome(self, agent_id: str) -> AgentGenome | None:
        row = self.connection.execute(
            "SELECT payload FROM agent_genomes WHERE agent_id=%s", (agent_id,)
        ).fetchone()
        return None if row is None else _genome(row["payload"])

    def list_agent_genomes(self) -> list[AgentGenome]:
        rows = self.connection.execute(
            "SELECT payload FROM agent_genomes ORDER BY agent_id"
        ).fetchall()
        return [_genome(row["payload"]) for row in rows]

    def save_experience(
        self, experience: ExperienceRecord, *, overwrite: bool = False
    ) -> None:
        if overwrite:
            self.connection.execute(
                """INSERT INTO experience_records(
                       experience_id, agent_id, task_type, goal_id, payload
                   ) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT(experience_id) DO UPDATE SET
                       agent_id=excluded.agent_id,
                       task_type=excluded.task_type,
                       goal_id=excluded.goal_id,
                       payload=excluded.payload""",
                (
                    experience.experience_id,
                    experience.agent_id,
                    experience.task_type,
                    experience.goal_id,
                    self._j(experience),
                ),
            )
        else:
            self.connection.execute(
                """INSERT INTO experience_records(
                       experience_id, agent_id, task_type, goal_id, payload
                   ) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT(experience_id) DO NOTHING""",
                (
                    experience.experience_id,
                    experience.agent_id,
                    experience.task_type,
                    experience.goal_id,
                    self._j(experience),
                ),
            )

    def list_experience(
        self,
        *,
        agent_id: str | None = None,
        task_type: str | None = None,
        goal_id: str | None = None,
        limit: int | None = None,
        min_strength: float | None = None,
    ) -> list[ExperienceRecord]:
        if limit is not None and (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10_000
        ):
            raise ValueError("experience list limit must be between 1 and 10000")
        if min_strength is not None and not 0 <= min_strength <= 1:
            raise ValueError("experience min_strength must be between 0 and 1")
        query = "SELECT payload FROM experience_records"
        clauses = []
        params: list[Any] = []
        for field, value in (
            ("agent_id", agent_id),
            ("task_type", task_type),
            ("goal_id", goal_id),
        ):
            if value is not None:
                clauses.append(f"{field}=%s")
                params.append(str(value))
        if min_strength is not None:
            clauses.append(
                "COALESCE(CAST(payload->>'strength' AS double precision), 1.0) >= %s"
            )
            params.append(min_strength)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY experience_id"
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [_experience(row["payload"]) for row in rows]

    def save_knowledge(self, item: KnowledgeItem) -> None:
        self.connection.execute(
            """INSERT INTO knowledge(knowledge_id, payload) VALUES (%s, %s)
               ON CONFLICT(knowledge_id) DO UPDATE SET payload=excluded.payload""",
            (item.knowledge_id, self._j(item)),
        )

    def list_knowledge(self) -> list[KnowledgeItem]:
        rows = self.connection.execute(
            "SELECT payload FROM knowledge ORDER BY knowledge_id"
        ).fetchall()
        result = []
        for row in rows:
            data = _payload(row["payload"])
            data["tags"] = tuple(data["tags"])
            result.append(KnowledgeItem(**data))
        return result

    def save_deployment(self, deployment: DeploymentRecord) -> None:
        self.connection.execute(
            """INSERT INTO deployments(task_type, payload) VALUES (%s, %s)
               ON CONFLICT(task_type) DO UPDATE SET payload=excluded.payload""",
            (deployment.task_type, self._j(deployment)),
        )

    def get_deployment(self, task_type: str) -> DeploymentRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM deployments WHERE task_type=%s", (task_type,)
        ).fetchone()
        return None if row is None else DeploymentRecord(**_payload(row["payload"]))

    def list_deployments(self) -> list[DeploymentRecord]:
        rows = self.connection.execute(
            "SELECT payload FROM deployments ORDER BY task_type"
        ).fetchall()
        return [DeploymentRecord(**_payload(row["payload"])) for row in rows]

    def register_worker(self, session: WorkerSession) -> WorkerSession:
        self.connection.execute(
            """INSERT INTO scheduler_workers(worker_id, session_id, expires_at, payload)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT(worker_id) DO UPDATE SET session_id=excluded.session_id,
               expires_at=excluded.expires_at, payload=excluded.payload""",
            (session.worker_id, session.session_id, session.expires_at, self._j(session)),
        )
        return session

    def _require_worker_locked(self, worker_id: str, session_id: str, now: str) -> WorkerSession:
        row = self.connection.execute(
            "SELECT payload FROM scheduler_workers WHERE worker_id=%s FOR UPDATE",
            (worker_id,),
        ).fetchone()
        if row is None:
            raise WorkerSessionRejected("worker session is not registered")
        session = _worker(row["payload"])
        if session.session_id != session_id:
            raise WorkerSessionRejected("worker session has been superseded")
        if session.is_expired(now):
            raise WorkerSessionRejected("worker session has expired")
        return session

    def heartbeat_worker(
        self, worker_id: str, session_id: str, *, now: str, ttl_seconds: int
    ) -> WorkerSession:
        with self.connection.transaction():
            current = self._require_worker_locked(worker_id, session_id, now)
            updated = current.heartbeat(now=now, ttl_seconds=ttl_seconds)
            row = self.connection.execute(
                """UPDATE scheduler_workers SET expires_at=%s, payload=%s
                   WHERE worker_id=%s AND session_id=%s RETURNING worker_id""",
                (updated.expires_at, self._j(updated), worker_id, session_id),
            ).fetchone()
            if row is None:
                raise WorkerSessionRejected("worker session has been superseded")
        return updated

    def get_worker(self, worker_id: str) -> WorkerSession | None:
        row = self.connection.execute(
            "SELECT payload FROM scheduler_workers WHERE worker_id=%s", (worker_id,)
        ).fetchone()
        return None if row is None else _worker(row["payload"])

    def list_workers(self) -> list[WorkerSession]:
        rows = self.connection.execute(
            "SELECT payload FROM scheduler_workers ORDER BY worker_id"
        ).fetchall()
        return [_worker(row["payload"]) for row in rows]

    def _create_claim_locked(
        self, session: WorkerSession, task: Task, agent_id: str, *, now: str,
        lease_seconds: int
    ) -> tuple[TaskClaim, Task]:
        row = self.connection.execute(
            """INSERT INTO task_claim_fences(goal_id, task_id, last_token)
               VALUES (%s, %s, 1)
               ON CONFLICT(goal_id, task_id) DO UPDATE
               SET last_token=task_claim_fences.last_token + 1
               RETURNING last_token""",
            (task.goal_id, task.task_id),
        ).fetchone()
        claim = TaskClaim.create(
            task.goal_id, task.task_id, session, agent_id, int(row["last_token"]),
            now=now, lease_seconds=lease_seconds,
        )
        running = replace(task, status=TaskStatus.RUNNING, assigned_agent_id=agent_id)
        self.connection.execute(
            """UPDATE tasks SET status=%s, assigned_agent_id=%s, payload=%s
               WHERE goal_id=%s AND task_id=%s""",
            (
                running.status.value, running.assigned_agent_id, self._j(running),
                running.goal_id, running.task_id,
            ),
        )
        self.connection.execute(
            """INSERT INTO task_claims(
                   claim_id, goal_id, task_id, worker_id, session_id,
                   fencing_token, status, expires_at, payload)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                claim.claim_id, claim.goal_id, claim.task_id, claim.worker_id,
                claim.session_id, claim.fencing_token, claim.status.value,
                claim.expires_at, self._j(claim),
            ),
        )
        return claim, running

    def claim_task(
        self, goal_id: str, task_id: str, worker_id: str, session_id: str,
        agent_id: str, *, now: str, lease_seconds: int
    ) -> TaskClaim | None:
        with self.connection.transaction():
            session = self._require_worker_locked(worker_id, session_id, now)
            goal_row = self.connection.execute(
                "SELECT status FROM goals WHERE goal_id=%s FOR SHARE", (goal_id,)
            ).fetchone()
            if goal_row is None or goal_row["status"] != GoalStatus.RUNNING.value:
                return None
            row = self.connection.execute(
                "SELECT payload FROM tasks WHERE goal_id=%s AND task_id=%s FOR UPDATE",
                (goal_id, task_id),
            ).fetchone()
            if row is None:
                return None
            task = _task(row["payload"])
            if task.status != TaskStatus.PENDING:
                return None
            for dependency_id in task.dependencies:
                dependency = self.connection.execute(
                    "SELECT status FROM tasks WHERE goal_id=%s AND task_id=%s",
                    (goal_id, dependency_id),
                ).fetchone()
                if dependency is None or dependency["status"] != TaskStatus.SUCCEEDED.value:
                    return None
            active = self.connection.execute(
                """SELECT 1 FROM task_claims
                   WHERE goal_id=%s AND task_id=%s AND status='active'""",
                (goal_id, task_id),
            ).fetchone()
            if active is not None:
                return None
            claim, _ = self._create_claim_locked(
                session, task, agent_id, now=now, lease_seconds=lease_seconds
            )
            return claim

    def claim_next_task(
        self, worker_id: str, session_id: str, assignments: Mapping[str, str],
        *, now: str, lease_seconds: int
    ) -> ClaimedTask | None:
        normalized = {str(key).strip(): str(value).strip() for key, value in assignments.items()}
        if not normalized or any(not key or not value for key, value in normalized.items()):
            raise ValueError("assignments must map task types to agent identifiers")
        with self.connection.transaction():
            session = self._require_worker_locked(worker_id, session_id, now)
            if not set(normalized).issubset(session.capabilities):
                raise ValueError("assignments must be within worker session capabilities")
            row = self.connection.execute(
                """SELECT t.payload
                   FROM tasks AS t
                   JOIN goals AS g ON g.goal_id=t.goal_id
                   WHERE g.status='running' AND t.status='pending'
                     AND t.task_type = ANY(%s)
                     AND NOT EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements_text(t.payload->'dependencies') AS dep(task_id)
                       LEFT JOIN tasks AS required
                         ON required.goal_id=t.goal_id AND required.task_id=dep.task_id
                       WHERE required.status IS DISTINCT FROM 'succeeded'
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM task_claims AS active
                       WHERE active.goal_id=t.goal_id AND active.task_id=t.task_id
                         AND active.status='active'
                     )
                   ORDER BY t.goal_id, t.position, t.task_id
                   FOR UPDATE OF t SKIP LOCKED
                   LIMIT 1""",
                (list(normalized),),
            ).fetchone()
            if row is None:
                return None
            task = _task(row["payload"])
            claim, running = self._create_claim_locked(
                session, task, normalized[task.task_type],
                now=now, lease_seconds=lease_seconds,
            )
            return ClaimedTask(claim, running)

    def _require_claim_locked(
        self, claim_id: str, worker_id: str, session_id: str,
        fencing_token: int, now: str
    ) -> TaskClaim:
        row = self.connection.execute(
            "SELECT payload FROM task_claims WHERE claim_id=%s FOR UPDATE", (claim_id,)
        ).fetchone()
        if row is None:
            raise StaleClaim("claim identity no longer owns task")
        claim = _claim(row["payload"])
        if (
            claim.status != ClaimStatus.ACTIVE
            or claim.worker_id != worker_id
            or claim.session_id != session_id
            or claim.fencing_token != fencing_token
        ):
            raise StaleClaim("claim identity no longer owns task")
        if claim.is_expired(now):
            raise StaleClaim("claim lease has expired")
        return claim

    def renew_claim(
        self, claim_id: str, worker_id: str, session_id: str, fencing_token: int,
        *, now: str, lease_seconds: int
    ) -> TaskClaim:
        with self.connection.transaction():
            self._require_worker_locked(worker_id, session_id, now)
            current = self._require_claim_locked(
                claim_id, worker_id, session_id, fencing_token, now
            )
            renewed = current.renew(now=now, lease_seconds=lease_seconds)
            row = self.connection.execute(
                """UPDATE task_claims SET expires_at=%s, payload=%s
                   WHERE claim_id=%s AND status='active' AND fencing_token=%s
                   RETURNING claim_id""",
                (renewed.expires_at, self._j(renewed), claim_id, fencing_token),
            ).fetchone()
            if row is None:
                raise StaleClaim("claim identity no longer owns task")
            return renewed

    def release_claim(
        self, claim_id: str, worker_id: str, session_id: str, fencing_token: int,
        *, now: str, reason: str = ""
    ) -> TaskClaim:
        with self.connection.transaction():
            self._require_worker_locked(worker_id, session_id, now)
            current = self._require_claim_locked(
                claim_id, worker_id, session_id, fencing_token, now
            )
            released = current.finish(ClaimStatus.RELEASED, now=now, reason=reason)
            self.connection.execute(
                "UPDATE task_claims SET status=%s, payload=%s WHERE claim_id=%s",
                (released.status.value, self._j(released), released.claim_id),
            )
            row = self.connection.execute(
                "SELECT payload FROM tasks WHERE goal_id=%s AND task_id=%s FOR UPDATE",
                (released.goal_id, released.task_id),
            ).fetchone()
            if row is not None:
                task = _task(row["payload"])
                if task.status == TaskStatus.RUNNING:
                    pending = replace(task, status=TaskStatus.PENDING, assigned_agent_id=None)
                    self.connection.execute(
                        """UPDATE tasks SET status=%s, assigned_agent_id=NULL, payload=%s
                           WHERE goal_id=%s AND task_id=%s""",
                        (pending.status.value, self._j(pending), pending.goal_id, pending.task_id),
                    )
            return released

    def pause_claim_for_approval(
        self,
        claim: TaskClaim,
        approval: ApprovalRequest,
        *,
        now: str,
    ) -> TaskClaim:
        """Atomically persist an approval and surrender the claimed task."""
        with self.connection.transaction():
            self._require_worker_locked(claim.worker_id, claim.session_id, now)
            current = self._require_claim_locked(
                claim.claim_id,
                claim.worker_id,
                claim.session_id,
                claim.fencing_token,
                now,
            )
            if (
                approval.goal_id != current.goal_id
                or approval.task_id != current.task_id
                or approval.status != ApprovalStatus.PENDING
            ):
                raise ValueError("approval identity does not match active claim")

            approval_row = self.connection.execute(
                "SELECT payload FROM approvals WHERE approval_id=%s FOR UPDATE",
                (approval.approval_id,),
            ).fetchone()
            if approval_row is not None:
                if _approval(approval_row["payload"]) != approval:
                    raise ValueError("approval identity cannot change")
            else:
                self.connection.execute(
                    """INSERT INTO approvals(
                           approval_id, fingerprint, goal_id, task_id, payload
                       ) VALUES (%s, %s, %s, %s, %s)""",
                    (
                        approval.approval_id,
                        approval.fingerprint,
                        approval.goal_id,
                        approval.task_id,
                        self._j(approval),
                    ),
                )

            task_row = self.connection.execute(
                "SELECT payload FROM tasks WHERE goal_id=%s AND task_id=%s FOR UPDATE",
                (current.goal_id, current.task_id),
            ).fetchone()
            goal_row = self.connection.execute(
                "SELECT payload FROM goals WHERE goal_id=%s FOR UPDATE",
                (current.goal_id,),
            ).fetchone()
            if task_row is None or goal_row is None:
                raise StaleClaim("claimed task or goal no longer exists")
            task = _task(task_row["payload"])
            goal = _goal(goal_row["payload"])
            if (
                task.status != TaskStatus.RUNNING
                or task.assigned_agent_id != current.agent_id
                or goal.status != GoalStatus.RUNNING
            ):
                raise StaleClaim("claim identity no longer owns running work")

            reason = f"approval required for tool {approval.tool_name}"
            released = current.finish(ClaimStatus.RELEASED, now=now, reason=reason)
            pending = replace(task, status=TaskStatus.PENDING, assigned_agent_id=None)
            paused = transition_goal(goal, GoalStatus.PAUSED, reason)
            event = Event.create(
                current.goal_id,
                "approval.requested",
                {
                    "approval_id": approval.approval_id,
                    "task_id": current.task_id,
                    "tool_name": approval.tool_name,
                    "claim_id": current.claim_id,
                    "fencing_token": current.fencing_token,
                },
            )
            self.connection.execute(
                "UPDATE task_claims SET status=%s, payload=%s WHERE claim_id=%s",
                (released.status.value, self._j(released), released.claim_id),
            )
            self.connection.execute(
                """UPDATE tasks SET status=%s, assigned_agent_id=NULL, payload=%s
                   WHERE goal_id=%s AND task_id=%s""",
                (pending.status.value, self._j(pending), pending.goal_id, pending.task_id),
            )
            self.connection.execute(
                "UPDATE goals SET status=%s, payload=%s WHERE goal_id=%s",
                (paused.status.value, self._j(paused), paused.goal_id),
            )
            self.connection.execute(
                """INSERT INTO events(event_id, goal_id, event_type, payload)
                   VALUES (%s, %s, %s, %s)""",
                (event.event_id, event.goal_id, event.event_type, self._j(event)),
            )
            return released

    def get_claim(self, claim_id: str) -> TaskClaim | None:
        row = self.connection.execute(
            "SELECT payload FROM task_claims WHERE claim_id=%s", (claim_id,)
        ).fetchone()
        return None if row is None else _claim(row["payload"])

    def list_claims(self, goal_id: str | None = None) -> list[TaskClaim]:
        query = "SELECT payload FROM task_claims"
        params: tuple[Any, ...] = ()
        if goal_id is not None:
            query += " WHERE goal_id=%s"
            params = (goal_id,)
        query += " ORDER BY goal_id, task_id, fencing_token"
        rows = self.connection.execute(query, params).fetchall()
        return [_claim(row["payload"]) for row in rows]

    def reap_expired_claims(self, *, now: str) -> list[TaskClaim]:
        expired = []
        with self.connection.transaction():
            rows = self.connection.execute(
                """SELECT payload FROM task_claims
                   WHERE status='active' AND expires_at <= %s
                   ORDER BY goal_id, task_id, fencing_token FOR UPDATE SKIP LOCKED""",
                (now,),
            ).fetchall()
            for row in rows:
                claim = _claim(row["payload"])
                ended = claim.finish(ClaimStatus.EXPIRED, now=now, reason="lease expired")
                self.connection.execute(
                    "UPDATE task_claims SET status=%s, payload=%s WHERE claim_id=%s",
                    (ended.status.value, self._j(ended), ended.claim_id),
                )
                task_row = self.connection.execute(
                    "SELECT payload FROM tasks WHERE goal_id=%s AND task_id=%s FOR UPDATE",
                    (claim.goal_id, claim.task_id),
                ).fetchone()
                if task_row is not None:
                    task = _task(task_row["payload"])
                    if task.status == TaskStatus.RUNNING:
                        pending = replace(task, status=TaskStatus.PENDING, assigned_agent_id=None)
                        self.connection.execute(
                            """UPDATE tasks SET status=%s, assigned_agent_id=NULL, payload=%s
                               WHERE goal_id=%s AND task_id=%s""",
                            (pending.status.value, self._j(pending), pending.goal_id, pending.task_id),
                        )
                event = Event.create(
                    claim.goal_id,
                    "task.claim_expired",
                    {
                        "claim_id": claim.claim_id, "task_id": claim.task_id,
                        "worker_id": claim.worker_id,
                        "fencing_token": claim.fencing_token,
                        "recovery": "pending", "reason": "lease expired",
                    },
                )
                self.connection.execute(
                    """INSERT INTO events(event_id, goal_id, event_type, payload)
                       VALUES (%s, %s, %s, %s)""",
                    (event.event_id, event.goal_id, event.event_type, self._j(event)),
                )
                expired.append(ended)
        return expired

    def commit_claim_outcome(
        self, claim: TaskClaim, task: Task, artifact: Artifact | None,
        attempt: Attempt, review: Review, performance: PerformanceRecord,
        events: Sequence[Event], outbox_messages: Sequence[OutboxMessage] = (),
        *, now: str
    ) -> TaskClaim:
        with self.connection.transaction():
            self._require_worker_locked(claim.worker_id, claim.session_id, now)
            current = self._require_claim_locked(
                claim.claim_id, claim.worker_id, claim.session_id,
                claim.fencing_token, now,
            )
            self._validate_outcome(current, task, artifact, attempt, review, performance, events)
            row = self.connection.execute(
                "SELECT payload FROM tasks WHERE goal_id=%s AND task_id=%s FOR UPDATE",
                (current.goal_id, current.task_id),
            ).fetchone()
            if row is None:
                raise StaleClaim("claimed task no longer exists")
            durable = _task(row["payload"])
            if durable.status != TaskStatus.RUNNING or durable.assigned_agent_id != current.agent_id:
                raise StaleClaim("claim identity no longer owns task")
            if artifact is not None:
                self.connection.execute(
                    """INSERT INTO artifacts(artifact_id, goal_id, task_id, payload)
                       VALUES (%s, %s, %s, %s)""",
                    (artifact.artifact_id, artifact.goal_id, artifact.task_id, self._j(artifact)),
                )
            self.connection.execute(
                """INSERT INTO attempts(attempt_id, goal_id, task_id, attempt_no, payload)
                   VALUES (%s, %s, %s, %s, %s)""",
                (attempt.attempt_id, attempt.goal_id, attempt.task_id,
                 attempt.attempt_no, self._j(attempt)),
            )
            self.connection.execute(
                """INSERT INTO reviews(review_id, goal_id, task_id, attempt_no, payload)
                   VALUES (%s, %s, %s, %s, %s)""",
                (review.review_id, review.goal_id, review.task_id,
                 review.attempt_no, self._j(review)),
            )
            self.connection.execute(
                """INSERT INTO performance(agent_id, task_type, payload) VALUES (%s, %s, %s)
                   ON CONFLICT(agent_id, task_type) DO UPDATE SET payload=excluded.payload""",
                (performance.agent_id, performance.task_type, self._j(performance)),
            )
            for event in events:
                self.connection.execute(
                    """INSERT INTO events(event_id, goal_id, event_type, payload)
                       VALUES (%s, %s, %s, %s)""",
                    (event.event_id, event.goal_id, event.event_type, self._j(event)),
                )
            for message in outbox_messages:
                self._enqueue_outbox_locked(message)
            self.connection.execute(
                """UPDATE tasks SET status=%s, assigned_agent_id=%s, payload=%s
                   WHERE goal_id=%s AND task_id=%s""",
                (task.status.value, task.assigned_agent_id, self._j(task), task.goal_id, task.task_id),
            )
            self._reconcile_goal_locked(task)
            committed = current.finish(ClaimStatus.COMMITTED, now=now)
            row = self.connection.execute(
                """UPDATE task_claims SET status=%s, payload=%s
                   WHERE claim_id=%s AND status='active' AND fencing_token=%s
                   RETURNING claim_id""",
                (committed.status.value, self._j(committed), committed.claim_id,
                 committed.fencing_token),
            ).fetchone()
            if row is None:
                raise StaleClaim("claim identity no longer owns task")
            return committed

    @staticmethod
    def _validate_outcome(
        current: TaskClaim, task: Task, artifact: Artifact | None,
        attempt: Attempt, review: Review, performance: PerformanceRecord,
        events: Sequence[Event]
    ) -> None:
        if (
            task.goal_id != current.goal_id or task.task_id != current.task_id
            or task.status not in {
                TaskStatus.PENDING, TaskStatus.SUCCEEDED,
                TaskStatus.FAILED, TaskStatus.BLOCKED,
            }
            or attempt.goal_id != current.goal_id or attempt.task_id != current.task_id
            or attempt.agent_id != current.agent_id
            or review.goal_id != current.goal_id or review.task_id != current.task_id
            or review.review_id != attempt.review_id
            or review.attempt_no != attempt.attempt_no
            or performance.agent_id != current.agent_id
            or performance.task_type != task.task_type
        ):
            raise ValueError("outcome identity does not match active claim")
        if task.status == TaskStatus.SUCCEEDED and (
            artifact is None or review.verdict != Verdict.PASS
        ):
            raise ValueError("succeeded task requires a passing review and artifact")
        if artifact is None:
            if attempt.artifact_id is not None or task.artifact_id is not None:
                raise ValueError("outcome artifact identity is inconsistent")
        elif (
            artifact.goal_id != current.goal_id
            or artifact.task_id != current.task_id
            or artifact.agent_id != current.agent_id
            or attempt.artifact_id != artifact.artifact_id
            or (task.status == TaskStatus.SUCCEEDED and task.artifact_id != artifact.artifact_id)
            or (task.status != TaskStatus.SUCCEEDED and task.artifact_id not in {None, artifact.artifact_id})
        ):
            raise ValueError("outcome artifact identity does not match active claim")
        if any(event.goal_id != current.goal_id for event in events):
            raise ValueError("outcome event belongs to another goal")

    def _reconcile_goal_locked(self, task: Task) -> None:
        row = self.connection.execute(
            "SELECT payload FROM goals WHERE goal_id=%s FOR UPDATE", (task.goal_id,)
        ).fetchone()
        if row is None:
            raise StaleClaim("claimed goal no longer exists")
        goal = _goal(row["payload"])
        if goal.status != GoalStatus.RUNNING:
            raise StaleClaim("claimed goal is no longer running")
        target = None
        reason = ""
        if task.status == TaskStatus.FAILED:
            target = GoalStatus.FAILED
            reason = f"task {task.task_id} failed"
        elif task.status == TaskStatus.BLOCKED:
            target = GoalStatus.BLOCKED
            reason = f"task {task.task_id} blocked"
        elif task.status == TaskStatus.SUCCEEDED:
            row = self.connection.execute(
                "SELECT bool_and(status='succeeded') AS complete, count(*) AS count "
                "FROM tasks WHERE goal_id=%s",
                (task.goal_id,),
            ).fetchone()
            if row["count"] and row["complete"]:
                target = GoalStatus.SUCCEEDED
        if target is None:
            return
        updated = transition_goal(goal, target, reason)
        self.connection.execute(
            "UPDATE goals SET status=%s, payload=%s WHERE goal_id=%s",
            (updated.status.value, self._j(updated), updated.goal_id),
        )
        event = Event.create(
            updated.goal_id,
            f"goal.{target.value}",
            {"task_id": task.task_id, **({"reason": reason} if reason else {})},
        )
        self.connection.execute(
            """INSERT INTO events(event_id, goal_id, event_type, payload)
               VALUES (%s, %s, %s, %s)""",
            (event.event_id, event.goal_id, event.event_type, self._j(event)),
        )

    def save_approval(self, approval: ApprovalRequest) -> None:
        with self.connection.transaction():
            row = self.connection.execute(
                "SELECT payload FROM approvals WHERE approval_id=%s FOR UPDATE",
                (approval.approval_id,),
            ).fetchone()
            if row is not None:
                current = _approval(row["payload"])
                if current == approval:
                    return
                if (
                    current.status != ApprovalStatus.PENDING
                    or approval.status
                    not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
                    or replace(
                        approval,
                        status=current.status,
                        decided_at=current.decided_at,
                        decided_by=current.decided_by,
                    )
                    != current
                ):
                    raise ValueError("approval identity cannot change")
                self.connection.execute(
                    "UPDATE approvals SET payload=%s WHERE approval_id=%s",
                    (self._j(approval), approval.approval_id),
                )
                return
            self.connection.execute(
                """INSERT INTO approvals(
                       approval_id, fingerprint, goal_id, task_id, payload
                   ) VALUES (%s, %s, %s, %s, %s)""",
                (
                    approval.approval_id,
                    approval.fingerprint,
                    approval.goal_id,
                    approval.task_id,
                    self._j(approval),
                ),
            )

    def save_approval_resolution(
        self,
        approval: ApprovalRequest,
        events: Iterable[Event],
        goal: Goal | None = None,
    ) -> None:
        with self.connection.transaction():
            approval_row = self.connection.execute(
                "SELECT payload FROM approvals WHERE approval_id=%s FOR UPDATE",
                (approval.approval_id,),
            ).fetchone()
            if approval_row is None:
                raise KeyError(f"approval not found: {approval.approval_id}")
            current = _approval(approval_row["payload"])
            expected_pending = replace(
                approval,
                status=ApprovalStatus.PENDING,
                decided_at="",
                decided_by="",
            )
            if current.status != ApprovalStatus.PENDING:
                raise ValueError("approval request is already resolved")
            if current != expected_pending:
                raise ValueError("approval identity cannot change")
            if goal is not None:
                goal_row = self.connection.execute(
                    "SELECT payload FROM goals WHERE goal_id=%s FOR UPDATE",
                    (goal.goal_id,),
                ).fetchone()
                if goal_row is None:
                    raise KeyError(f"goal not found: {goal.goal_id}")
                current_goal = _goal(goal_row["payload"])
                if (
                    current_goal.goal_id != approval.goal_id
                    or current_goal.status != GoalStatus.PAUSED
                    or goal.status not in {GoalStatus.RUNNING, GoalStatus.FAILED}
                ):
                    raise ValueError("approval goal is no longer paused")
            self.connection.execute(
                "UPDATE approvals SET payload=%s WHERE approval_id=%s",
                (self._j(approval), approval.approval_id),
            )
            if goal is not None:
                self.connection.execute(
                    "UPDATE goals SET status=%s, payload=%s WHERE goal_id=%s",
                    (goal.status.value, self._j(goal), goal.goal_id),
                )
            for event in events:
                self.connection.execute(
                    """INSERT INTO events(event_id, goal_id, event_type, payload)
                       VALUES (%s, %s, %s, %s)""",
                    (event.event_id, event.goal_id, event.event_type, self._j(event)),
                )

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        row = self.connection.execute(
            "SELECT payload FROM approvals WHERE approval_id=%s", (approval_id,)
        ).fetchone()
        return None if row is None else _approval(row["payload"])

    def get_approval_by_fingerprint(self, fingerprint: str) -> ApprovalRequest | None:
        row = self.connection.execute(
            "SELECT payload FROM approvals WHERE fingerprint=%s", (fingerprint,)
        ).fetchone()
        return None if row is None else _approval(row["payload"])

    def list_approvals(self, goal_id: str | None = None) -> list[ApprovalRequest]:
        query = "SELECT payload FROM approvals"
        params: tuple[Any, ...] = ()
        if goal_id is not None:
            query += " WHERE goal_id=%s"
            params = (goal_id,)
        query += " ORDER BY approval_id"
        rows = self.connection.execute(query, params).fetchall()
        return [_approval(row["payload"]) for row in rows]

    def save_span(self, span: TraceSpan) -> None:
        self.connection.execute(
            """INSERT INTO trace_spans(span_id, goal_id, payload)
               VALUES (%s, %s, %s)""",
            (span.span_id, span.goal_id, self._j(span)),
        )

    def list_spans(self, goal_id: str) -> list[TraceSpan]:
        rows = self.connection.execute(
            "SELECT payload FROM trace_spans WHERE goal_id=%s ORDER BY sequence",
            (goal_id,),
        ).fetchall()
        return [_span(row["payload"]) for row in rows]

    # PostgreSQL v0.9 deliberately exposes no partial A2A governance,
    # evaluation, publication, or maintenance persistence.

    def list_workspace_snapshots(self, goal_id: str):
        return []

    def list_verification_results(self, goal_id: str):
        return []

    def get_publication(self, goal_id: str):
        return None
