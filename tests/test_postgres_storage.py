import importlib.util
import os
import unittest
from dataclasses import replace
from uuid import uuid4

from seed_society.domain import (
    Artifact,
    Attempt,
    Event,
    Goal,
    GoalStatus,
    OutboxMessage,
    OutboxStatus,
    PerformanceRecord,
    Review,
    Task,
    TaskStatus,
    Verdict,
)
from seed_society.postgres_storage import PostgreSQLRepository
from seed_society.scheduler import ClaimStatus, WorkerSession
from tests.scheduler_conformance import (
    ApprovalPauseContract,
    ClaimNextTaskContract,
    OwnershipConformanceContract,
    OutcomeReconciliationContract,
)


POSTGRES_URL = os.environ.get("SCHEDULER_POSTGRES_URL", "")


class PostgreSQLDependencyTests(unittest.TestCase):
    @unittest.skipIf(importlib.util.find_spec("psycopg") is not None, "psycopg installed")
    def test_default_install_reports_the_postgres_extra(self):
        with self.assertRaisesRegex(RuntimeError, r"postgres.*extra"):
            PostgreSQLRepository("postgresql://unused")


@unittest.skipUnless(POSTGRES_URL, "SCHEDULER_POSTGRES_URL is not configured")
class PostgreSQLContractBase:
    def setUp(self):
        self.schema = f"test_{uuid4().hex}"
        self.first = PostgreSQLRepository(POSTGRES_URL, schema=self.schema)
        self.second = PostgreSQLRepository(POSTGRES_URL, schema=self.schema)

    def tearDown(self):
        self.second.close()
        self.first.drop_schema()
        self.first.close()


class PostgreSQLClaimNextTaskTests(
    PostgreSQLContractBase, ClaimNextTaskContract, unittest.TestCase
):
    pass


class PostgreSQLTimeTests(PostgreSQLContractBase, unittest.TestCase):
    def test_scheduler_now_uses_postgresql_clock(self):
        from datetime import datetime, timezone

        from seed_society.scheduler import parse_utc

        difference = abs(
            (
                parse_utc(self.first.scheduler_now())
                - datetime.now(timezone.utc)
            ).total_seconds()
        )
        self.assertLess(difference, 5)

    def test_operational_counts_are_database_aggregates(self):
        now = self.first.scheduler_now()
        self.first.enqueue_outbox(
            OutboxMessage.create(
                "webhook",
                "postgres-metrics-key",
                {"event": "x"},
                now=now,
            )
        )

        counts = self.first.operational_counts(now)

        self.assertEqual(counts["workers_total"], 0)
        self.assertEqual(counts["claims_active"], 0)
        self.assertEqual(counts["approvals_pending"], 0)
        self.assertEqual(counts["outbox_pending"], 1)


class PostgreSQLOutboxTests(PostgreSQLContractBase, unittest.TestCase):
    def test_claim_is_isolated_by_topic(self):
        at = "2026-07-17T00:00:00+00:00"
        later = "2026-07-17T00:00:05+00:00"
        self.first.enqueue_outbox(
            OutboxMessage.create("audit", "postgres-audit", {"event": "a"}, now=at)
        )
        expected = self.first.enqueue_outbox(
            OutboxMessage.create(
                "webhook", "postgres-webhook", {"event": "w"}, now=later
            )
        )

        claimed = self.second.claim_outbox(
            "dispatcher-a",
            topic="webhook",
            now=later,
            lease_seconds=10,
        )

        self.assertEqual(claimed.message_id, expected.message_id)

    def test_outbox_is_idempotent_exclusive_and_reclaimable(self):
        at = "2026-07-17T00:00:00+00:00"
        later = "2026-07-17T00:00:05+00:00"
        expired = "2026-07-17T00:00:11+00:00"
        message = OutboxMessage.create(
            "webhook", "postgres-key", {"event": "x"}, now=at
        )

        self.assertEqual(self.first.enqueue_outbox(message), message)
        duplicate = OutboxMessage.create(
            "webhook", "postgres-key", {"event": "x"}, now=later
        )
        self.assertEqual(self.second.enqueue_outbox(duplicate), message)
        first_claim = self.first.claim_outbox(
            "dispatcher-a", now=at, lease_seconds=10
        )
        renewed = self.first.renew_outbox(
            first_claim.message_id,
            "dispatcher-a",
            first_claim.delivery_token,
            now=later,
            lease_seconds=10,
        )
        self.assertIsNone(
            self.second.claim_outbox(
                "dispatcher-b", now=expired, lease_seconds=10
            )
        )
        reclaimed = self.second.claim_outbox(
            "dispatcher-b",
            now="2026-07-17T00:00:16+00:00",
            lease_seconds=10,
        )
        delivered = self.second.complete_outbox(
            reclaimed.message_id,
            "dispatcher-b",
            reclaimed.delivery_token,
            now="2026-07-17T00:00:16+00:00",
        )

        self.assertEqual(
            renewed.claim_expires_at,
            "2026-07-17T00:00:15+00:00",
        )
        self.assertEqual(reclaimed.delivery_token, 2)
        self.assertEqual(delivered.status, OutboxStatus.DELIVERED)
        self.assertEqual(self.first.list_outbox(), [delivered])
        self.assertEqual(
            self.first.list_outbox(status=OutboxStatus.DELIVERED, limit=1),
            [delivered],
        )
        self.assertEqual(
            self.first.purge_outbox(
                before="2026-07-17T00:00:17+00:00",
                limit=10,
            ),
            1,
        )
        self.assertEqual(self.first.list_outbox(), [])

    def test_expired_outbox_at_attempt_limit_becomes_terminal(self):
        at = "2026-07-17T00:00:00+00:00"
        expired = "2026-07-17T00:00:11+00:00"
        self.first.enqueue_outbox(
            OutboxMessage.create(
                "webhook",
                "postgres-crash-key",
                {"event": "x"},
                now=at,
                max_attempts=1,
            )
        )
        self.first.claim_outbox("dispatcher-a", now=at, lease_seconds=10)

        self.assertIsNone(
            self.second.claim_outbox(
                "dispatcher-b", now=expired, lease_seconds=10
            )
        )
        self.assertEqual(
            self.second.list_outbox()[0].status,
            OutboxStatus.FAILED,
        )

    def test_outbox_conflict_rolls_back_the_complete_claim_outcome(self):
        at = "2026-07-17T00:00:00+00:00"
        goal = replace(
            Goal.create("Atomic", "Rollback outbox conflict", goal_id="atomic"),
            status=GoalStatus.RUNNING,
        )
        task = Task.create("atomic", "task", "analysis", "Analyze")
        self.first.save_goal(goal)
        self.first.save_task(task)
        self.first.register_worker(
            WorkerSession.create(
                "worker-a",
                "session-a",
                ("analysis",),
                now=at,
                ttl_seconds=60,
            )
        )
        claim = self.first.claim_task(
            "atomic",
            "task",
            "worker-a",
            "session-a",
            "agent-a",
            now=at,
            lease_seconds=30,
        )
        running = self.first.list_tasks("atomic")[0]
        artifact = Artifact.create("atomic", "task", "agent-a", "accepted")
        review = Review.create(
            "atomic", "task", 1, Verdict.PASS, 95, (), "Accepted"
        )
        attempt = Attempt.create(
            "atomic",
            "task",
            "agent-a",
            1,
            10,
            artifact.artifact_id,
            review.review_id,
        )
        performance = PerformanceRecord("agent-a", "analysis", 1, 1, 95, 10)
        succeeded = replace(
            running,
            status=TaskStatus.SUCCEEDED,
            assigned_agent_id="agent-a",
            artifact_id=artifact.artifact_id,
        )
        existing = OutboxMessage.create(
            "webhook", "atomic-key", {"event": "original"}, now=at
        )
        self.first.enqueue_outbox(existing)
        conflicting = OutboxMessage.create(
            "webhook", "atomic-key", {"event": "changed"}, now=at
        )

        with self.assertRaisesRegex(ValueError, "idempotency payload"):
            self.first.commit_claim_outcome(
                claim,
                succeeded,
                artifact,
                attempt,
                review,
                performance,
                (Event.create("atomic", "task.attempt_completed", {}),),
                (conflicting,),
                now="2026-07-17T00:00:01+00:00",
            )

        self.assertEqual(self.first.get_goal("atomic").status, GoalStatus.RUNNING)
        self.assertEqual(self.first.list_tasks("atomic")[0].status, TaskStatus.RUNNING)
        self.assertEqual(self.first.get_claim(claim.claim_id).status, ClaimStatus.ACTIVE)
        self.assertEqual(self.first.list_artifacts("atomic"), [])
        self.assertEqual(self.first.list_attempts("atomic"), [])
        self.assertEqual(self.first.list_reviews("atomic"), [])
        self.assertEqual(self.first.list_outbox(), [existing])


class PostgreSQLApprovalPauseTests(
    PostgreSQLContractBase, ApprovalPauseContract, unittest.TestCase
):
    pass


class PostgreSQLOutcomeReconciliationTests(
    PostgreSQLContractBase, OutcomeReconciliationContract, unittest.TestCase
):
    pass


class PostgreSQLOwnershipConformanceTests(
    PostgreSQLContractBase, OwnershipConformanceContract, unittest.TestCase
):
    pass


if __name__ == "__main__":
    unittest.main()
