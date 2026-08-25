"""Deterministic bridge between the society seed store and dsh-mneme.

Two directions, both dry-run by default (sila: mutations need an explicit
operator flag, and every applied mutation writes an audit event):

- Push (society -> mneme): promoted semantic knowledge (tag
  `source:consolidation`) and optionally high-strength PASS experience
  lessons are upserted into the mneme SQLite store
  (`~/.dsh/memory/memory.db`, node:sqlite WAL file) so DSH sessions get
  them injected and searchable. Row ids are content-addressed, so re-sync
  is idempotent; mneme-owned lifecycle flags (forgotten/archived) are
  never resurrected.
- Import (mneme -> society): mneme entries that were NOT produced by this
  bridge (e.g. autoDream summaries, human-curated decisions) are imported
  into the society knowledge seed store with provenance tags.

The bridge writes only the `memories` table using the exact schema mneme
creates (idempotent DDL). WAL + busy_timeout make concurrent access with a
running DSH safe. Vector embeddings are left to mneme's own reindex path.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .domain import Event, ExperienceRecord, KnowledgeItem
from .storage import SQLiteRepository

MNEME_SOURCE_PREFIX = "agent-society-loop:"

# Injection-gate equivalence: mneme's default importance threshold is 3, and
# importance = ceil(strength * 5), so strength 0.5 is exactly the boundary at
# which a seed stops being injected (遗忘=停止现行). Experience rows at or
# above it are pushed; below it they are either never pushed or decayed down.
EXPERIENCE_PUSH_THRESHOLD = 0.5

_MNEME_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id          TEXT PRIMARY KEY,
  type        TEXT NOT NULL,
  title       TEXT NOT NULL,
  content     TEXT NOT NULL,
  tags        TEXT NOT NULL DEFAULT '[]',
  importance  INTEGER NOT NULL DEFAULT 3,
  forgotten   INTEGER NOT NULL DEFAULT 0,
  archived    INTEGER NOT NULL DEFAULT 0,
  source      TEXT,
  embedding   TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);
"""

_VALID_TYPES = {"preference", "project", "decision", "history", "summary"}


@dataclass(frozen=True, slots=True)
class MnemePushReport:
    target_db: str
    planned: int
    pushed: int
    refreshed: int
    decay_refreshed: int
    skipped_archived: int
    rows: tuple[dict[str, Any], ...]
    applied: bool


@dataclass(frozen=True, slots=True)
class MnemeImportReport:
    source_db: str
    candidates: int
    imported: int
    skipped_existing: int
    rows: tuple[dict[str, Any], ...]
    applied: bool


def resolve_mneme_dir(mneme_dir: str | None) -> Path:
    raw = str(mneme_dir or "").strip() or "~/.dsh/memory"
    if raw.startswith("~"):
        raw = str(Path.home() / raw[1:].lstrip("/\\"))
    return Path(raw)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_id(title: str, content: str) -> str:
    digest = hashlib.sha256(f"{title}\n{content}".encode("utf-8")).hexdigest()
    return f"asl-{digest[:16]}"


def _connect(db_path: Path, *, create: bool) -> sqlite3.Connection:
    if not create and not db_path.exists():
        raise FileNotFoundError(
            f"mneme store not found: {db_path} "
            "(dsh-mneme has not booted yet or memoryDir differs)"
        )
    if create:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=5.0)
    connection.execute("PRAGMA busy_timeout = 5000")
    if create:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(_MNEME_SCHEMA)
        connection.commit()
    return connection


def _existing_flags(connection: sqlite3.Connection, row_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT forgotten, archived FROM memories WHERE id=?", (row_id,)
    ).fetchone()
    if row is None:
        return {"exists": False, "forgotten": 0, "archived": 0}
    return {"exists": True, "forgotten": int(row[0]), "archived": int(row[1])}


def _refresh_decayed_experience(
    connection: sqlite3.Connection, repository: SQLiteRepository
) -> int:
    """Decay-to-importance linkage (遗忘=停止现行).

    Every mneme row we own with an experience provenance carries the seed's
    strength mapped onto mneme's 1-5 importance. When consolidation decays a
    seed below the injection threshold, this pass lowers the row's importance
    so mneme's autoInject stops surfacing it — forgetting becomes real on the
    DSH side. Runs regardless of the push filter so decayed rows that fell
    under the push threshold still converge.
    """
    rows = connection.execute(
        "SELECT id, source, importance FROM memories WHERE source LIKE ?",
        (f"{MNEME_SOURCE_PREFIX}experience-%",),
    ).fetchall()
    if not rows:
        return 0
    by_id = {record.experience_id: record for record in repository.list_experience()}
    now = _now_iso()
    refreshed = 0
    for row_id, source, importance in rows:
        experience_id = str(source)[len(MNEME_SOURCE_PREFIX):]
        record = by_id.get(experience_id)
        if record is None:
            continue
        new_importance = max(1, min(5, math.ceil(record.strength * 5)))
        if int(importance) != new_importance:
            connection.execute(
                "UPDATE memories SET importance=?, updated_at=? WHERE id=?",
                (new_importance, now, row_id),
            )
            refreshed += 1
    return refreshed


def push_seeds(
    repository: SQLiteRepository,
    *,
    mneme_dir: str | None = None,
    memory_type: str = "project",
    limit: int = 10,
    include_experience: bool = False,
    apply: bool = False,
) -> MnemePushReport:
    """Upsert society seeds into the mneme store (idempotent, bounded)."""
    if memory_type not in _VALID_TYPES:
        raise ValueError(f"mneme memory type must be one of {sorted(_VALID_TYPES)}")
    if not 1 <= limit <= 100:
        raise ValueError("mneme push limit must be between 1 and 100")
    db_path = resolve_mneme_dir(mneme_dir) / "memory.db"

    rows: list[dict[str, Any]] = []
    for item in repository.list_knowledge():
        if "source:consolidation" in item.tags:
            rows.append(
                {
                    "title": item.title,
                    "content": item.content,
                    "importance": 4,
                    "tags": sorted({*item.tags, "agent-society"}),
                    "source": f"{MNEME_SOURCE_PREFIX}{item.knowledge_id}",
                }
            )
    if include_experience:
        seen = {(row["title"], row["content"]) for row in rows}
        experiences = sorted(
            (
                record
                for record in repository.list_experience()
                if record.verdict.casefold() == "pass"
                and record.strength >= EXPERIENCE_PUSH_THRESHOLD
            ),
            key=lambda record: (-record.strength, record.experience_id),
        )
        for record in experiences:
            entry = {
                "title": f"{record.task_type} 成功模式",
                "content": record.lessons[0],
                "importance": max(1, min(5, math.ceil(record.strength * 5))),
                "tags": sorted({record.task_type, "pattern:success", "agent-society"}),
                "source": f"{MNEME_SOURCE_PREFIX}{record.experience_id}",
            }
            key = (entry["title"], entry["content"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(entry)
    rows = rows[:limit]

    # Dry-run must not create or mutate anything on disk (sila). When the
    # store does not exist yet, report every row as new without touching it.
    connection = None
    if apply or db_path.exists():
        connection = _connect(db_path, create=apply)
    try:
        now = _now_iso()
        if connection is None:
            planned = [
                {**entry, "exists": False, "forgotten": 0, "archived": 0}
                for entry in rows
            ]
            return MnemePushReport(
                target_db=str(db_path),
                planned=len(planned),
                pushed=0,
                refreshed=0,
                decay_refreshed=0,
                skipped_archived=0,
                rows=tuple(planned),
                applied=apply,
            )
        planned = []
        for entry in rows:
            flags = _existing_flags(connection, _row_id(entry["title"], entry["content"]))
            planned.append({**entry, **flags})
        pushed = refreshed = decay_refreshed = skipped = 0
        if apply:
            for entry in rows:
                row_id = _row_id(entry["title"], entry["content"])
                flags = _existing_flags(connection, row_id)
                if flags["archived"] or flags["forgotten"]:
                    skipped += 1
                    continue
                tags_json = json.dumps(entry["tags"], ensure_ascii=False)
                connection.execute(
                    """INSERT INTO memories(
                           id, type, title, content, tags, importance,
                           forgotten, archived, source, embedding, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, NULL, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           title=excluded.title,
                           content=excluded.content,
                           tags=excluded.tags,
                           importance=excluded.importance,
                           source=excluded.source,
                           updated_at=excluded.updated_at""",
                    (
                        row_id,
                        memory_type,
                        entry["title"],
                        entry["content"],
                        tags_json,
                        int(entry["importance"]),
                        entry["source"],
                        now,
                        now,
                    ),
                )
                if flags["exists"]:
                    refreshed += 1
                else:
                    pushed += 1
            # 遗忘=停止现行: lower importance of our decayed experience rows
            # so mneme's autoInject stops surfacing forgotten seeds.
            decay_refreshed = _refresh_decayed_experience(connection, repository)
            connection.commit()
        report = MnemePushReport(
            target_db=str(db_path),
            planned=len(planned),
            pushed=pushed,
            refreshed=refreshed,
            decay_refreshed=decay_refreshed,
            skipped_archived=skipped,
            rows=tuple(planned),
            applied=apply,
        )
        if apply and planned:
            repository.append_event(
                Event.create(
                    "mneme-sync",
                    "memory.mneme_pushed",
                    {
                        "target_db": str(db_path),
                        "pushed": pushed,
                        "refreshed": refreshed,
                        "decay_refreshed": decay_refreshed,
                        "skipped_archived": skipped,
                    },
                )
            )
        return report
    finally:
        if connection is not None:
            connection.close()


def import_seeds(
    repository: SQLiteRepository,
    *,
    mneme_dir: str | None = None,
    memory_type: str | None = None,
    limit: int = 5,
    apply: bool = False,
) -> MnemeImportReport:
    """Import non-society mneme entries back into the knowledge seed store."""
    if memory_type is not None and memory_type not in _VALID_TYPES:
        raise ValueError(f"mneme memory type must be one of {sorted(_VALID_TYPES)}")
    if not 1 <= limit <= 100:
        raise ValueError("mneme import limit must be between 1 and 100")
    db_path = resolve_mneme_dir(mneme_dir) / "memory.db"
    connection = _connect(db_path, create=False)
    try:
        query = (
            "SELECT id, type, title, content, tags, importance, source, updated_at "
            "FROM memories WHERE forgotten=0 AND archived=0 "
            "AND (source IS NULL OR source NOT LIKE ?) "
        )
        parameters: list[Any] = [f"{MNEME_SOURCE_PREFIX}%"]
        if memory_type is not None:
            query += " AND type=?"
            parameters.append(memory_type)
        query += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        parameters.append(limit)
        rows = [
            {
                "mneme_id": str(row[0]),
                "type": str(row[1]),
                "title": str(row[2]),
                "content": str(row[3]),
                "tags": json.loads(row[4]) if row[4] else [],
                "importance": int(row[5]),
                "source": str(row[6]) if row[6] else "",
                "updated_at": str(row[7]),
            }
            for row in connection.execute(query, parameters).fetchall()
        ]
    finally:
        connection.close()

    existing = repository.list_knowledge()
    candidates = []
    for row in rows:
        if any(
            _overlap(f"{item.title} {item.content}", f"{row['title']} {row['content']}")
            >= 0.5
            for item in existing
        ):
            continue
        candidates.append(row)
    imported = 0
    if apply:
        for row in candidates:
            tags = sorted(
                {"mneme", row["type"], *(str(tag) for tag in row["tags"] if str(tag).strip())}
            )
            repository.save_knowledge(
                KnowledgeItem.create(
                    row["title"],
                    row["content"],
                    tuple(tags),
                )
            )
            imported += 1
        if candidates:
            repository.append_event(
                Event.create(
                    "mneme-import",
                    "memory.mneme_imported",
                    {"source_db": str(db_path), "imported": imported},
                )
            )
    return MnemeImportReport(
        source_db=str(db_path),
        candidates=len(candidates),
        imported=imported,
        skipped_existing=len(rows) - len(candidates),
        rows=tuple(candidates),
        applied=apply,
    )


def _tokens(text: str) -> set[str]:
    import re

    return set(re.findall(r"[\w-]+", str(text).casefold()))


def _overlap(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
