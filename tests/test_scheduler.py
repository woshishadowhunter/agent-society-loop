import tempfile
import unittest
import sqlite3
from dataclasses import replace
from pathlib import Path

from agent_society_loop.domain import (
    Artifact,
    Attempt,
    DelegationRecord,
    DelegationStatus,
    Event,
    Goal,
    GoalStatus,
    PerformanceRecord,
    Review,
    Task,
    TaskStatus,
    Verdict,
)
from agent_society_loop.deterministic import build_demo_engine
from agent_society_loop.scheduler import (
    ClaimStatus,
    TaskClaim,
    StaleClaim,
    WorkerSession,
    WorkerSessionRejected,
    parse_utc,
    validate_duration,
)
from agent_society_loop.storage import SQLiteRepository
from tests.scheduler_conformance import (
    ClaimNextTaskContract,
    OutcomeReconciliationContract,
)


AT = "2026-07-16T00:00:00+00:00"
PLUS_5 = "2026-07-16T00:00:05+00:00"
PLUS_15 = "2026-07-16T00:00:15+00:00"


class SchedulerDomainTests(unittest.TestCase):
    def test_worker_session_and_claim_validate_identity_time_and_transitions(self):
        session = WorkerSession.create(
            "worker-a", "session-a", ("writing", "analysis", "analysis"),
            now=AT, ttl_seconds=30,
        )
        claim = TaskClaim.create(
            "g", "t", session, "agent-a", 1, now=AT, lease_seconds=10
        )

        self.assertEqual(session.capabilities, ("analysis", "writing"))
        self.assertEqual(claim.status, ClaimStatus.ACTIVE)
        renewed = claim.renew(now=PLUS_5, lease_seconds=10)
        self.assertEqual(renewed.expires_at, PLUS_15)
        committed = renewed.finish(ClaimStatus.COMMITTED, now=PLUS_5)
        self.assertEqual(committed.status, ClaimStatus.COMMITTED)
        self.assertEqual(committed.finished_at, PLUS_5)

        with self.assertRaisesRegex(ValueError, "terminal"):
            claim.finish(ClaimStatus.ACTIVE, now=PLUS_5)
        with self.assertRaisesRegex(ValueError, "active"):
            committed.renew(now=PLUS_5, lease_seconds=10)

    def test_scheduler_values_reject_invalid_identifiers_durations_and_times(self):
        with self.assertRaisesRegex(ValueError, "worker_id"):
            WorkerSession.create("bad worker", "session-a", (), now=AT, ttl_seconds=30)
        with self.assertRaisesRegex(ValueError, "capabilities"):
            WorkerSession.create("worker-a", "session-a", ("bad value",), now=AT, ttl_seconds=30)
        with self.assertRaisesRegex(ValueError, "duration"):
            validate_duration(0)
        with self.assertRaisesRegex(ValueError, "duration"):
            validate_duration(86_401)
        with self.assertRaisesRegex(ValueError, "UTC"):
            parse_utc("2026-07-16T00:00:00")

        session = WorkerSession.create(
            "worker-a", "session-a", (), now=AT, ttl_seconds=30
        )
        with self.assertRaisesRegex(ValueError, "fencing_token"):
            TaskClaim.create(
                "g", "t", session, "agent-a", 0, now=AT, lease_seconds=10
            )


class SchedulerStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "scheduler.db"
        self.first = SQLiteRepository(self.path)
        self.second = SQLiteRepository(self.path)

    def tearDown(self):
        self.second.close()
        self.first.close()
        self.directory.cleanup()

    def test_new_worker_session_supersedes_old_and_survives_reopen(self):
        first_session = WorkerSession.create(
            "worker-a", "session-1", ("analysis",), now=AT, ttl_seconds=30
        )
        second_session = WorkerSession.create(
            "worker-a", "session-2", ("analysis",), now=PLUS_5, ttl_seconds=30
        )

        self.first.register_worker(first_session)
        self.second.register_worker(second_session)

        with self.assertRaisesRegex(WorkerSessionRejected, "superseded"):
            self.first.heartbeat_worker(
                "worker-a", "session-1", now="2026-07-16T00:00:06+00:00", ttl_seconds=30
            )
        self.assertEqual(self.first.get_worker("worker-a"), second_session)
        self.assertEqual(self.first.list_workers(), [second_session])

        self.second.close()
        self.second = SQLiteRepository(self.path)
        self.assertEqual(self.second.get_worker("worker-a"), second_session)

    def test_heartbeat_extends_exact_live_session_but_not_expired_session(self):
        session = WorkerSession.create(
            "worker-a", "session-a", (), now=AT, ttl_seconds=10
        )
        self.first.register_worker(session)

        heartbeat = self.second.heartbeat_worker(
            "worker-a", "session-a", now=PLUS_5, ttl_seconds=10
        )

        self.assertEqual(heartbeat.last_heartbeat_at, PLUS_5)
        self.assertEqual(heartbeat.expires_at, PLUS_15)
        with self.assertRaisesRegex(WorkerSessionRejected, "expired"):
            self.first.heartbeat_worker(
                "worker-a", "session-a", now=PLUS_15, ttl_seconds=10
            )

    def test_v07_database_opens_with_additive_scheduler_schema(self):
        self.second.close()
        self.first.close()
        legacy_path = Path(self.directory.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE legacy_evidence (identity TEXT PRIMARY KEY);
            INSERT INTO legacy_evidence(identity) VALUES ('v0.7');
            """
        )
        connection.commit()
        connection.close()

        self.first = SQLiteRepository(legacy_path)
        self.second = SQLiteRepository(legacy_path)

        tables = {
            row[0]
            for row in self.first.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertTrue(
            {"scheduler_workers", "task_claim_fences", "task_claims"}.issubset(tables)
        )
        self.assertEqual(
            self.first.connection.execute(
                "SELECT identity FROM legacy_evidence"
            ).fetchone()[0],
            "v0.7",
        )


class SchedulerClaimTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "claims.db"
        self.first = SQLiteRepository(self.path)
        self.second = SQLiteRepository(self.path)
        goal = replace(
            Goal.create("Schedule", "Claim work", goal_id="g"),
            status=GoalStatus.RUNNING,
        )
        self.first.save_goal(goal)
        self.first.save_task(Task.create("g", "t", "analysis", "Analyze"))
        self.first.register_worker(
            WorkerSession.create(
                "worker-a", "session-a", ("analysis",), now=AT, ttl_seconds=60
            )
        )
        self.first.register_worker(
            WorkerSession.create(
                "worker-b", "session-b", ("analysis",), now=AT, ttl_seconds=60
            )
        )

    def tearDown(self):
        self.second.close()
        self.first.close()
        self.directory.cleanup()

    def test_two_connections_claim_once_renew_and_release_with_increasing_token(self):
        first_claim = self.first.claim_task(
            "g", "t", "worker-a", "session-a", "agent-a",
            now=AT, lease_seconds=10,
        )

        self.assertIsNotNone(first_claim)
        self.assertEqual(first_claim.fencing_token, 1)
        self.assertIsNone(
            self.second.claim_task(
                "g", "t", "worker-b", "session-b", "agent-a",
                now=PLUS_5, lease_seconds=10,
            )
        )
        renewed = self.first.renew_claim(
            first_claim.claim_id,
            "worker-a",
            "session-a",
            first_claim.fencing_token,
            now=PLUS_5,
            lease_seconds=10,
        )
        self.assertEqual(renewed.expires_at, PLUS_15)

        released = self.first.release_claim(
            renewed.claim_id,
            "worker-a",
            "session-a",
            renewed.fencing_token,
            now="2026-07-16T00:00:06+00:00",
            reason="operator drain",
        )
        self.assertEqual(released.status, ClaimStatus.RELEASED)
        self.assertEqual(self.first.list_tasks("g")[0].status, TaskStatus.PENDING)

        replacement = self.second.claim_task(
            "g", "t", "worker-b", "session-b", "agent-a",
            now="2026-07-16T00:00:07+00:00", lease_seconds=10,
        )
        self.assertEqual(replacement.fencing_token, 2)
        self.assertEqual(
            [item.status for item in self.first.list_claims("g")],
            [ClaimStatus.RELEASED, ClaimStatus.ACTIVE],
        )

    def test_claim_revalidates_goal_task_dependencies_and_worker_session(self):
        blocked_goal = replace(self.first.get_goal("g"), status=GoalStatus.PAUSED)
        self.first.save_goal(blocked_goal)
        self.assertIsNone(
            self.first.claim_task(
                "g", "t", "worker-a", "session-a", "agent-a",
                now=AT, lease_seconds=10,
            )
        )
        self.first.save_goal(replace(blocked_goal, status=GoalStatus.RUNNING))
        dependency = Task.create("g", "dep", "analysis", "Dependency", position=0)
        dependent = Task.create(
            "g", "child", "analysis", "Child", dependencies=("dep",), position=1
        )
        self.first.save_tasks([dependency, dependent])

        self.assertIsNone(
            self.first.claim_task(
                "g", "child", "worker-a", "session-a", "agent-a",
                now=AT, lease_seconds=10,
            )
        )
        self.first.save_task(replace(dependency, status=TaskStatus.SUCCEEDED))
        self.assertIsNotNone(
            self.first.claim_task(
                "g", "child", "worker-a", "session-a", "agent-a",
                now=AT, lease_seconds=10,
            )
        )
        with self.assertRaisesRegex(WorkerSessionRejected, "superseded"):
            self.first.claim_task(
                "g", "t", "worker-a", "wrong-session", "agent-a",
                now=AT, lease_seconds=10,
            )

    def test_renewal_requires_exact_live_claim_identity(self):
        claim = self.first.claim_task(
            "g", "t", "worker-a", "session-a", "agent-a",
            now=AT, lease_seconds=10,
        )

        with self.assertRaisesRegex(StaleClaim, "identity"):
            self.second.renew_claim(
                claim.claim_id, "worker-a", "session-a", 99,
                now=PLUS_5, lease_seconds=10,
            )
        with self.assertRaisesRegex(StaleClaim, "expired"):
            self.first.renew_claim(
                claim.claim_id, "worker-a", "session-a", claim.fencing_token,
                now="2026-07-16T00:00:10+00:00", lease_seconds=10,
            )


class SQLiteClaimNextTaskTests(ClaimNextTaskContract, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "claim-next.db"
        self.first = SQLiteRepository(self.path)
        self.second = SQLiteRepository(self.path)

    def tearDown(self):
        self.second.close()
        self.first.close()
        self.directory.cleanup()


class SQLiteOutcomeReconciliationTests(
    OutcomeReconciliationContract, unittest.TestCase
):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.first = self.repository

    def tearDown(self):
        self.repository.close()


class SchedulerOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "outcomes.db"
        self.first = SQLiteRepository(self.path)
        self.second = SQLiteRepository(self.path)
        goal = replace(
            Goal.create("Outcome", "Fence outcomes", goal_id="g"),
            status=GoalStatus.RUNNING,
        )
        self.first.save_goal(goal)
        self.first.save_task(Task.create("g", "t", "analysis", "Analyze"))
        for worker_id, session_id in (
            ("worker-a", "session-a"),
            ("worker-b", "session-b"),
        ):
            self.first.register_worker(
                WorkerSession.create(
                    worker_id, session_id, ("analysis",), now=AT, ttl_seconds=60
                )
            )

    def tearDown(self):
        self.second.close()
        self.first.close()
        self.directory.cleanup()

    def _make_outcome(self, claim, agent_id):
        artifact = Artifact.create("g", "t", agent_id, "accepted artifact")
        review = Review.create("g", "t", 1, Verdict.PASS, 92, [], "Accepted")
        attempt = Attempt.create(
            "g", "t", agent_id, 1, 12.0, artifact.artifact_id, review.review_id
        )
        performance = PerformanceRecord(agent_id, "analysis", 1, 1, 92.0, 12.0)
        event = Event.create("g", "task.attempt_completed", {"task_id": "t"})
        task = replace(
            self.first.list_tasks("g")[0],
            status=TaskStatus.SUCCEEDED,
            artifact_id=artifact.artifact_id,
        )
        return task, artifact, attempt, review, performance, event

    def test_expired_worker_cannot_commit_any_partial_outcome(self):
        old = self.first.claim_task(
            "g", "t", "worker-a", "session-a", "agent-a",
            now=AT, lease_seconds=10,
        )
        expired = self.first.reap_expired_claims(
            now="2026-07-16T00:00:10+00:00"
        )
        self.assertEqual([item.claim_id for item in expired], [old.claim_id])
        replacement = self.second.claim_task(
            "g", "t", "worker-b", "session-b", "agent-b",
            now="2026-07-16T00:00:10+00:00", lease_seconds=10,
        )
        self.assertGreater(replacement.fencing_token, old.fencing_token)
        outcome = self._make_outcome(old, "agent-a")

        with self.assertRaisesRegex(StaleClaim, "identity"):
            self.first.commit_claim_outcome(
                old, *outcome[:-1], [outcome[-1]],
                now="2026-07-16T00:00:11+00:00",
            )

        self.assertEqual(self.first.list_artifacts("g", "t"), [])
        self.assertEqual(self.first.list_attempts("g", "t"), [])
        self.assertEqual(self.first.list_reviews("g", "t"), [])
        self.assertIsNone(self.first.get_performance("agent-a", "analysis"))
        self.assertEqual(
            [event.event_type for event in self.first.list_events("g")],
            ["task.claim_expired"],
        )

    def test_current_claim_commits_complete_outcome_atomically_and_survives_reopen(self):
        claim = self.first.claim_task(
            "g", "t", "worker-a", "session-a", "agent-a",
            now=AT, lease_seconds=10,
        )
        outcome = self._make_outcome(claim, "agent-a")

        committed = self.second.commit_claim_outcome(
            claim, *outcome[:-1], [outcome[-1]], now=PLUS_5
        )

        self.assertEqual(committed.status, ClaimStatus.COMMITTED)
        self.assertEqual(self.first.list_tasks("g")[0].status, TaskStatus.SUCCEEDED)
        self.assertEqual(len(self.first.list_artifacts("g", "t")), 1)
        self.assertEqual(len(self.first.list_attempts("g", "t")), 1)
        self.assertEqual(len(self.first.list_reviews("g", "t")), 1)
        self.assertEqual(
            self.first.get_performance("agent-a", "analysis").passes, 1
        )

        self.second.close()
        self.second = SQLiteRepository(self.path)
        self.assertEqual(
            self.second.get_claim(claim.claim_id).status, ClaimStatus.COMMITTED
        )

    def test_failed_review_keeps_audit_artifact_but_returns_task_to_pending(self):
        claim = self.first.claim_task(
            "g", "t", "worker-a", "session-a", "agent-a",
            now=AT, lease_seconds=10,
        )
        artifact = Artifact.create("g", "t", "agent-a", "draft artifact")
        review = Review.create("g", "t", 1, Verdict.FAIL, 35, [], "Revise")
        attempt = Attempt.create(
            "g", "t", "agent-a", 1, 9.0, artifact.artifact_id, review.review_id
        )
        performance = PerformanceRecord("agent-a", "analysis", 1, 0, 35.0, 9.0)
        pending = replace(
            self.first.list_tasks("g")[0],
            status=TaskStatus.PENDING,
            artifact_id=None,
            assigned_agent_id=None,
        )

        committed = self.first.commit_claim_outcome(
            claim,
            pending,
            artifact,
            attempt,
            review,
            performance,
            [Event.create("g", "task.review_failed", {"task_id": "t"})],
            now=PLUS_5,
        )

        self.assertEqual(committed.status, ClaimStatus.COMMITTED)
        self.assertEqual(self.first.list_tasks("g")[0].status, TaskStatus.PENDING)
        self.assertEqual(len(self.first.list_artifacts("g", "t")), 1)

    def test_outcome_sql_failure_rolls_back_claim_and_every_new_record(self):
        claim = self.first.claim_task(
            "g", "t", "worker-a", "session-a", "agent-a",
            now=AT, lease_seconds=10,
        )
        outcome = self._make_outcome(claim, "agent-a")
        self.first.append_event(outcome[-1])

        with self.assertRaises(sqlite3.IntegrityError):
            self.first.commit_claim_outcome(
                claim, *outcome[:-1], [outcome[-1]], now=PLUS_5
            )

        self.assertEqual(
            self.first.get_claim(claim.claim_id).status, ClaimStatus.ACTIVE
        )
        self.assertEqual(self.first.list_tasks("g")[0].status, TaskStatus.RUNNING)
        self.assertEqual(self.first.list_artifacts("g", "t"), [])
        self.assertEqual(self.first.list_attempts("g", "t"), [])
        self.assertEqual(self.first.list_reviews("g", "t"), [])

    def test_terminal_goal_event_failure_rolls_back_the_entire_fenced_commit(self):
        claim = self.first.claim_task(
            "g", "t", "worker-a", "session-a", "agent-a",
            now=AT, lease_seconds=10,
        )
        outcome = self._make_outcome(claim, "agent-a")
        self.first.connection.execute(
            """
            CREATE TRIGGER reject_terminal_goal_event
            BEFORE INSERT ON events
            WHEN json_extract(NEW.payload, '$.event_type') = 'goal.succeeded'
            BEGIN
                SELECT RAISE(ABORT, 'terminal event failure');
            END
            """
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "terminal event failure"):
            self.first.commit_claim_outcome(
                claim, *outcome[:-1], [outcome[-1]], now=PLUS_5
            )

        self.assertEqual(self.first.get_goal("g").status, GoalStatus.RUNNING)
        self.assertEqual(self.first.get_claim(claim.claim_id).status, ClaimStatus.ACTIVE)
        self.assertEqual(self.first.list_tasks("g")[0].status, TaskStatus.RUNNING)
        self.assertEqual(self.first.list_artifacts("g", "t"), [])
        self.assertEqual(self.first.list_attempts("g", "t"), [])
        self.assertEqual(self.first.list_reviews("g", "t"), [])
        self.assertEqual(self.first.get_performance("agent-a", "analysis"), None)
        self.assertEqual(self.first.list_events("g"), [])

    def test_succeeded_task_requires_a_passing_review_and_artifact(self):
        claim = self.first.claim_task(
            "g", "t", "worker-a", "session-a", "agent-a",
            now=AT, lease_seconds=10,
        )
        artifact = Artifact.create("g", "t", "agent-a", "rejected artifact")
        review = Review.create("g", "t", 1, Verdict.FAIL, 20, [], "Rejected")
        attempt = Attempt.create(
            "g", "t", "agent-a", 1, 4.0, artifact.artifact_id, review.review_id
        )
        succeeded = replace(
            self.first.list_tasks("g")[0],
            status=TaskStatus.SUCCEEDED,
            artifact_id=artifact.artifact_id,
        )

        with self.assertRaisesRegex(ValueError, "succeeded task"):
            self.first.commit_claim_outcome(
                claim,
                succeeded,
                artifact,
                attempt,
                review,
                PerformanceRecord("agent-a", "analysis", 1, 0, 20.0, 4.0),
                [],
                now=PLUS_5,
            )

        self.assertEqual(self.first.list_artifacts("g", "t"), [])
        self.assertEqual(self.first.get_claim(claim.claim_id).status, ClaimStatus.ACTIVE)

    def test_risky_remote_expiry_blocks_but_accepted_remote_work_is_resumable(self):
        risky = self.first.claim_task(
            "g", "t", "worker-a", "session-a", "agent-a",
            now=AT, lease_seconds=10,
        )
        self.first.save_delegation(
            DelegationRecord(
                "delegation-risky", "g", "t", 1, "agent-a", "model-a",
                "a" * 64, "message-risky", "b" * 64,
                status=DelegationStatus.UNKNOWN, error_category="send_timeout",
            )
        )

        self.first.reap_expired_claims(now="2026-07-16T00:00:10+00:00")

        self.assertEqual(
            self.first.list_tasks("g")[0].status, TaskStatus.BLOCKED
        )
        self.assertEqual(
            self.first.get_claim(risky.claim_id).reason,
            "unsafe remote delegation state: unknown",
        )

        accepted_task = Task.create("g", "accepted", "analysis", "Resume polling")
        self.first.save_task(accepted_task)
        accepted = self.first.claim_task(
            "g", "accepted", "worker-b", "session-b", "agent-b",
            now=AT, lease_seconds=10,
        )
        self.first.save_delegation(
            DelegationRecord(
                "delegation-accepted", "g", "accepted", 1, "agent-b", "model-b",
                "c" * 64, "message-accepted", "d" * 64,
                status=DelegationStatus.ACCEPTED, remote_task_id="remote-1",
            )
        )

        self.first.reap_expired_claims(now="2026-07-16T00:00:10+00:00")

        tasks = {task.task_id: task for task in self.first.list_tasks("g")}
        self.assertEqual(tasks["accepted"].status, TaskStatus.PENDING)
        self.assertEqual(
            self.first.get_claim(accepted.claim_id).status, ClaimStatus.EXPIRED
        )


class SchedulerEngineBoundaryTests(unittest.TestCase):
    def test_legacy_engine_refuses_to_recover_scheduler_owned_task(self):
        repository = SQLiteRepository(":memory:")
        engine = build_demo_engine(repository)
        goal = replace(
            Goal.create("Owned", "Do not bypass lease", goal_id="owned"),
            status=GoalStatus.RUNNING,
        )
        repository.save_goal(goal)
        repository.save_task(
            Task.create("owned", "market", "market_analysis", "Analyze")
        )
        repository.register_worker(
            WorkerSession.create(
                "process-a", "session-a", ("market_analysis",),
                now=AT, ttl_seconds=60,
            )
        )
        claim = repository.claim_task(
            "owned", "market", "process-a", "session-a", "market-analyst",
            now=AT, lease_seconds=30,
        )

        with self.assertRaisesRegex(RuntimeError, "active scheduler claim"):
            engine.resume("owned")

        self.assertEqual(repository.get_claim(claim.claim_id).status, ClaimStatus.ACTIVE)
        self.assertEqual(repository.list_tasks("owned")[0].status, TaskStatus.RUNNING)
        self.assertEqual(repository.get_goal("owned").status, GoalStatus.RUNNING)
        repository.close()

    def test_legacy_task_outcome_writes_cannot_bypass_active_claim(self):
        repository = SQLiteRepository(":memory:")
        goal = replace(
            Goal.create("Fence", "Reject legacy writes", goal_id="fence"),
            status=GoalStatus.RUNNING,
        )
        task = Task.create("fence", "task", "analysis", "Analyze")
        repository.save_goal(goal)
        repository.save_task(task)
        repository.register_worker(
            WorkerSession.create(
                "process-a", "session-a", ("analysis",), now=AT, ttl_seconds=60
            )
        )
        repository.claim_task(
            "fence", "task", "process-a", "session-a", "agent-a",
            now=AT, lease_seconds=30,
        )
        artifact = Artifact.create("fence", "task", "agent-a", "bypass")
        review = Review.create("fence", "task", 1, Verdict.FAIL, 0, [], "bypass")
        attempt = Attempt.create(
            "fence", "task", "agent-a", 1, 1.0, None, review.review_id
        )
        performance = PerformanceRecord("agent-a", "analysis", 1, 0, 0.0, 1.0)

        for mutation in (
            lambda: repository.save_task(replace(task, status=TaskStatus.PENDING)),
            lambda: repository.save_artifact(artifact),
            lambda: repository.save_review(review),
            lambda: repository.save_attempt_outcome(
                attempt,
                review,
                performance,
                Event.create("fence", "task.attempt_completed", {}),
            ),
        ):
            with self.assertRaisesRegex(StaleClaim, "fenced"):
                mutation()

        self.assertEqual(repository.list_artifacts("fence", "task"), [])
        self.assertEqual(repository.list_reviews("fence", "task"), [])
        self.assertEqual(repository.list_attempts("fence", "task"), [])
        repository.close()


if __name__ == "__main__":
    unittest.main()
