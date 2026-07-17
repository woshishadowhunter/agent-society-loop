import unittest
from dataclasses import replace

from agent_society_loop.domain import (
    ApprovalRequest,
    Goal,
    GoalStatus,
    OutboxMessage,
    Task,
)
from agent_society_loop.operations import collect_health, collect_metrics
from agent_society_loop.scheduler import WorkerSession
from agent_society_loop.storage import SQLiteRepository


AT = "2026-07-17T00:00:00+00:00"
AFTER_EXPIRY = "2026-07-17T00:00:20+00:00"


class AggregateOnlyRepository:
    def scheduler_now(self):
        return AFTER_EXPIRY

    def operational_counts(self, now):
        if now != AFTER_EXPIRY:
            raise AssertionError("database time must be reused for the snapshot")
        return {
            "workers_total": 3,
            "workers_expired": 1,
            "claims_active": 2,
            "claims_expired_active": 1,
            "approvals_pending": 4,
            "outbox_pending": 5,
            "outbox_delivering": 1,
            "outbox_delivered": 8,
            "outbox_failed": 2,
        }

    def __getattr__(self, name):
        if name.startswith("list_"):
            raise AssertionError(f"operational snapshots must not call {name}")
        raise AttributeError(name)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.repository.scheduler_now = lambda: AFTER_EXPIRY
        goal = replace(
            Goal.create("Operations", "Inspect runtime", goal_id="ops"),
            status=GoalStatus.RUNNING,
        )
        self.repository.save_goal(goal)
        self.repository.save_task(
            Task.create("ops", "task", "analysis", "Analyze")
        )
        self.repository.register_worker(
            WorkerSession.create(
                "worker-expired",
                "session-expired",
                ("analysis",),
                now=AT,
                ttl_seconds=10,
            )
        )
        self.repository.claim_task(
            "ops",
            "task",
            "worker-expired",
            "session-expired",
            "agent-a",
            now=AT,
            lease_seconds=10,
        )
        self.repository.save_approval(
            ApprovalRequest.create(
                "ops", "task", "write_file", {"path": "x"}, "approval required"
            )
        )
        terminal = self.repository.enqueue_outbox(
            OutboxMessage.create(
                "webhook",
                "failed",
                {"event": "failed"},
                now=AT,
                max_attempts=1,
            )
        )
        claimed = self.repository.claim_outbox(
            "dispatcher-a", now=AT, lease_seconds=10
        )
        self.repository.complete_outbox(
            claimed.message_id,
            "dispatcher-a",
            claimed.delivery_token,
            now="2026-07-17T00:00:05+00:00",
            error="unavailable",
        )
        self.repository.enqueue_outbox(
            OutboxMessage.create(
                "webhook",
                "pending",
                {"event": "pending"},
                now="2026-07-17T00:00:06+00:00",
            )
        )

    def tearDown(self):
        self.repository.close()

    def test_health_reports_database_and_degraded_operational_state(self):
        health = collect_health(self.repository)

        self.assertEqual(health["status"], "degraded")
        self.assertTrue(health["ready"])
        self.assertEqual(health["database_time"], AFTER_EXPIRY)
        self.assertEqual(health["workers"], {"total": 1, "expired": 1})
        self.assertEqual(health["claims"], {"active": 1, "expired_active": 1})
        self.assertEqual(health["approvals"]["pending"], 1)
        self.assertEqual(health["outbox"]["failed"], 1)

    def test_worker_health_requires_a_live_registered_session(self):
        expired = collect_health(
            self.repository,
            worker_id="worker-expired",
        )
        missing = collect_health(
            self.repository,
            worker_id="worker-missing",
        )

        self.assertFalse(expired["ready"])
        self.assertEqual(expired["status"], "unavailable")
        self.assertEqual(expired["worker"]["state"], "expired")
        self.assertFalse(missing["ready"])
        self.assertEqual(missing["worker"]["state"], "missing")

    def test_metrics_are_bounded_numeric_counters(self):
        metrics = collect_metrics(self.repository)

        self.assertEqual(metrics["workers_total"], 1)
        self.assertEqual(metrics["workers_expired"], 1)
        self.assertEqual(metrics["claims_active"], 1)
        self.assertEqual(metrics["claims_expired_active"], 1)
        self.assertEqual(metrics["approvals_pending"], 1)
        self.assertEqual(metrics["outbox_pending"], 1)
        self.assertEqual(metrics["outbox_failed"], 1)
        self.assertTrue(all(isinstance(value, int) for value in metrics.values()))

    def test_snapshots_use_database_aggregates_without_loading_payloads(self):
        repository = AggregateOnlyRepository()

        metrics = collect_metrics(repository)
        health = collect_health(repository)

        self.assertEqual(metrics["outbox_delivered"], 8)
        self.assertEqual(health["database_time"], AFTER_EXPIRY)
        self.assertEqual(health["workers"], {"total": 3, "expired": 1})
        self.assertEqual(health["outbox"]["failed"], 2)


if __name__ == "__main__":
    unittest.main()
