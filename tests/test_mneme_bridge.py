"""Tests for the deterministic dsh-mneme bridge."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_society_loop.domain import Goal, KnowledgeItem
from agent_society_loop.mneme_bridge import (
    MNEME_SOURCE_PREFIX,
    import_seeds,
    push_seeds,
    resolve_mneme_dir,
)
from agent_society_loop.storage import SQLiteRepository


class MnemePushTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.temp = tempfile.TemporaryDirectory()
        self.mneme_dir = Path(self.temp.name)

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def _seed_promoted_knowledge(self):
        self.repository.save_knowledge(
            KnowledgeItem.create(
                "analysis 成功模式",
                "Successful pattern: evidence first",
                ("analysis", "pattern:success", "source:consolidation"),
            )
        )
        self.repository.save_knowledge(
            KnowledgeItem.create(
                "普通知识",
                "no promotion tag",
                ("analysis",),
            )
        )

    def test_resolve_mneme_dir_expands_home(self):
        resolved = resolve_mneme_dir("~/custom/dir")
        self.assertEqual(resolved, Path.home() / "custom" / "dir")

    def test_dry_run_does_not_create_store(self):
        self._seed_promoted_knowledge()
        report = push_seeds(self.repository, mneme_dir=str(self.mneme_dir))
        self.assertFalse(report.applied)
        self.assertEqual(report.planned, 1)
        self.assertFalse((self.mneme_dir / "memory.db").exists())

    def test_push_writes_idempotent_rows(self):
        self._seed_promoted_knowledge()
        first = push_seeds(self.repository, mneme_dir=str(self.mneme_dir), apply=True)
        self.assertTrue(first.applied)
        self.assertEqual(first.pushed, 1)
        self.assertEqual(first.refreshed, 0)

        second = push_seeds(self.repository, mneme_dir=str(self.mneme_dir), apply=True)
        self.assertEqual(second.pushed, 0)
        self.assertEqual(second.refreshed, 1)

        connection = sqlite3.connect(str(self.mneme_dir / "memory.db"))
        try:
            rows = connection.execute(
                "SELECT type, source, importance, forgotten, archived FROM memories"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            row_type, source, importance, forgotten, archived = rows[0]
            self.assertEqual(row_type, "project")
            self.assertTrue(source.startswith(MNEME_SOURCE_PREFIX))
            self.assertEqual(importance, 4)
            self.assertEqual(forgotten, 0)
            self.assertEqual(archived, 0)
        finally:
            connection.close()

    def test_push_never_resurrects_archived_rows(self):
        self._seed_promoted_knowledge()
        push_seeds(self.repository, mneme_dir=str(self.mneme_dir), apply=True)
        connection = sqlite3.connect(str(self.mneme_dir / "memory.db"))
        try:
            connection.execute("UPDATE memories SET archived=1")
            connection.commit()
        finally:
            connection.close()
        report = push_seeds(self.repository, mneme_dir=str(self.mneme_dir), apply=True)
        self.assertEqual(report.skipped_archived, 1)
        self.assertEqual(report.pushed, 0)

    def test_apply_writes_audit_event(self):
        self._seed_promoted_knowledge()
        goal = Goal.create("Bridge", "Bridge test", goal_id="bridge-audit")
        self.repository.save_goal(goal)
        push_seeds(self.repository, mneme_dir=str(self.mneme_dir), apply=True)
        events = self.repository.list_events("mneme-sync")
        self.assertTrue(
            any(event.event_type == "memory.mneme_pushed" for event in events)
        )

    def test_include_experience_pushes_strong_lessons(self):
        from agent_society_loop.domain import ExperienceRecord, utc_now

        self.repository.save_experience(
            ExperienceRecord(
                experience_id="experience-strong",
                goal_id="g1",
                task_id="t1",
                task_type="analysis",
                agent_id="agent-a",
                attempt_no=1,
                verdict="PASS",
                score=95.0,
                lessons=("Successful pattern: verify then write",),
                tags=("pattern:success",),
                created_at=utc_now(),
                strength=0.9,
                salience=0.9,
                valence=0.95,
            )
        )
        report = push_seeds(
            self.repository,
            mneme_dir=str(self.mneme_dir),
            include_experience=True,
            apply=True,
        )
        self.assertEqual(report.pushed, 1)
        connection = sqlite3.connect(str(self.mneme_dir / "memory.db"))
        try:
            importance = connection.execute(
                "SELECT importance FROM memories"
            ).fetchone()[0]
            self.assertEqual(importance, 5)  # ceil(0.9 * 5) = 5
        finally:
            connection.close()

    def test_decay_linkage_lowers_importance_below_injection_threshold(self):
        """遗忘=停止现行: a decayed seed drops importance so mneme stops
        injecting it — even without --include-experience on the refresh pass."""
        from agent_society_loop.domain import ExperienceRecord, utc_now

        self.repository.save_experience(
            ExperienceRecord(
                experience_id="experience-decay",
                goal_id="g1",
                task_id="t1",
                task_type="analysis",
                agent_id="agent-a",
                attempt_no=1,
                verdict="PASS",
                score=95.0,
                lessons=("Successful pattern: evidence first",),
                tags=("pattern:success",),
                created_at=utc_now(),
                strength=0.9,
                salience=0.9,
                valence=0.95,
            )
        )
        push_seeds(
            self.repository,
            mneme_dir=str(self.mneme_dir),
            include_experience=True,
            apply=True,
        )

        # Consolidation decays the seed far below the push threshold (0.8).
        self.repository.save_experience(
            ExperienceRecord(
                experience_id="experience-decay",
                goal_id="g1",
                task_id="t1",
                task_type="analysis",
                agent_id="agent-a",
                attempt_no=1,
                verdict="PASS",
                score=95.0,
                lessons=("Successful pattern: evidence first",),
                tags=("pattern:success",),
                created_at=utc_now(),
                strength=0.3,
                salience=0.9,
                valence=0.95,
            ),
            overwrite=True,
        )

        # Refresh WITHOUT --include-experience: the decayed row must still
        # converge downward (it is no longer a push candidate).
        report = push_seeds(self.repository, mneme_dir=str(self.mneme_dir), apply=True)
        self.assertEqual(report.decay_refreshed, 1)

        connection = sqlite3.connect(str(self.mneme_dir / "memory.db"))
        try:
            importance = connection.execute(
                "SELECT importance FROM memories"
            ).fetchone()[0]
            self.assertEqual(importance, 2)  # ceil(0.3 * 5) = 2 < injection threshold 3
        finally:
            connection.close()


class MnemeImportTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.temp = tempfile.TemporaryDirectory()
        self.mneme_dir = Path(self.temp.name)

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def _seed_mneme_rows(self):
        from agent_society_loop.mneme_bridge import _connect

        connection = _connect(self.mneme_dir / "memory.db", create=True)
        try:
            rows = [
                (
                    "dream-1",
                    "summary",
                    "记忆库总览",
                    "用户偏好深度写作与可审计工程。",
                    json.dumps(["summary"]),
                    5,
                    "dream",
                    "2026-08-14T00:00:00.000Z",
                ),
                (
                    "asl-echo",
                    "project",
                    "echo",
                    "should be excluded",
                    json.dumps(["agent-society"]),
                    4,
                    f"{MNEME_SOURCE_PREFIX}knowledge-1",
                    "2026-08-14T00:00:00.000Z",
                ),
            ]
            connection.executemany(
                """INSERT INTO memories(
                       id, type, title, content, tags, importance, source,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        row_id,
                        row_type,
                        title,
                        content,
                        tags,
                        importance,
                        source,
                        updated,
                        updated,
                    )
                    for (
                        row_id,
                        row_type,
                        title,
                        content,
                        tags,
                        importance,
                        source,
                        updated,
                    ) in rows
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def test_import_excludes_bridge_echo_and_deduplicates(self):
        self._seed_mneme_rows()
        report = import_seeds(self.repository, mneme_dir=str(self.mneme_dir))
        self.assertEqual(report.candidates, 1)
        self.assertEqual(report.imported, 0)

        applied = import_seeds(
            self.repository, mneme_dir=str(self.mneme_dir), apply=True
        )
        self.assertEqual(applied.imported, 1)
        knowledge = self.repository.list_knowledge()
        self.assertEqual(len(knowledge), 1)
        self.assertIn("mneme", knowledge[0].tags)
        self.assertIn("summary", knowledge[0].tags)

        again = import_seeds(
            self.repository, mneme_dir=str(self.mneme_dir), apply=True
        )
        self.assertEqual(again.imported, 0)  # dedupe by token overlap
        self.assertEqual(again.skipped_existing, 1)

    def test_import_type_filter(self):
        self._seed_mneme_rows()
        report = import_seeds(
            self.repository, mneme_dir=str(self.mneme_dir), memory_type="project"
        )
        self.assertEqual(report.candidates, 0)  # project rows are bridge echo


if __name__ == "__main__":
    unittest.main()
