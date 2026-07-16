import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent_society_loop.domain import Goal, GoalStatus, Task, TaskStatus
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


if __name__ == "__main__":
    unittest.main()
