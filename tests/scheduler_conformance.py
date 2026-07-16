"""Backend-neutral scheduler conformance contracts."""

from __future__ import annotations

from dataclasses import replace

from agent_society_loop.domain import Goal, GoalStatus, Task, TaskStatus
from agent_society_loop.scheduler import ClaimStatus, WorkerSession


AT = "2026-07-16T00:00:00+00:00"


class ClaimNextTaskContract:
    """Assertions shared by every transactional scheduler repository."""

    first: object
    second: object

    def seed_claim_next_graph(self) -> None:
        later_goal = replace(
            Goal.create("Later", "Second goal", goal_id="goal-b"),
            status=GoalStatus.RUNNING,
        )
        first_goal = replace(
            Goal.create("First", "First goal", goal_id="goal-a"),
            status=GoalStatus.RUNNING,
        )
        self.first.save_goal(later_goal)
        self.first.save_goal(first_goal)
        self.first.save_tasks(
            [
                Task.create(
                    "goal-a", "dependent", "analysis", "Wait", dependencies=("root",),
                    position=0,
                ),
                Task.create("goal-a", "root", "analysis", "Start", position=1),
                Task.create("goal-a", "write", "writing", "Write", position=2),
                Task.create("goal-b", "other", "analysis", "Other", position=0),
            ]
        )
        for worker_id, session_id, capabilities in (
            ("worker-a", "session-a", ("analysis", "writing")),
            ("worker-b", "session-b", ("analysis", "translation")),
        ):
            self.first.register_worker(
                WorkerSession.create(
                    worker_id,
                    session_id,
                    capabilities,
                    now=AT,
                    ttl_seconds=60,
                )
            )

    def test_claim_next_uses_deterministic_ready_order_and_assignment(self) -> None:
        self.seed_claim_next_graph()

        claimed = self.first.claim_next_task(
            "worker-a",
            "session-a",
            {"analysis": "agent-analysis", "writing": "agent-writing"},
            now=AT,
            lease_seconds=30,
        )

        self.assertIsNotNone(claimed)
        self.assertEqual((claimed.task.goal_id, claimed.task.task_id), ("goal-a", "root"))
        self.assertEqual(claimed.task.status, TaskStatus.RUNNING)
        self.assertEqual(claimed.task.assigned_agent_id, "agent-analysis")
        self.assertEqual(claimed.claim.agent_id, "agent-analysis")
        self.assertEqual(claimed.claim.fencing_token, 1)

    def test_claim_next_skips_incompatible_and_dependency_blocked_tasks(self) -> None:
        self.seed_claim_next_graph()

        claimed = self.first.claim_next_task(
            "worker-a",
            "session-a",
            {"writing": "agent-writing"},
            now=AT,
            lease_seconds=30,
        )

        self.assertEqual(claimed.task.task_id, "write")
        self.assertIsNone(
            self.second.claim_next_task(
                "worker-b",
                "session-b",
                {"translation": "agent-translation"},
                now=AT,
                lease_seconds=30,
            )
        )

    def test_claim_next_competing_connections_do_not_share_ownership(self) -> None:
        self.seed_claim_next_graph()

        first = self.first.claim_next_task(
            "worker-a",
            "session-a",
            {"analysis": "agent-a"},
            now=AT,
            lease_seconds=30,
        )
        second = self.second.claim_next_task(
            "worker-b",
            "session-b",
            {"analysis": "agent-b"},
            now=AT,
            lease_seconds=30,
        )

        self.assertNotEqual(
            (first.task.goal_id, first.task.task_id),
            (second.task.goal_id, second.task.task_id),
        )
        active = [
            claim for claim in self.first.list_claims() if claim.status == ClaimStatus.ACTIVE
        ]
        self.assertEqual(len(active), 2)
        self.assertEqual(len({(claim.goal_id, claim.task_id) for claim in active}), 2)

    def test_claim_next_rejects_assignment_outside_session_capabilities(self) -> None:
        self.seed_claim_next_graph()

        with self.assertRaisesRegex(ValueError, "capabilities"):
            self.first.claim_next_task(
                "worker-b",
                "session-b",
                {"writing": "agent-writing"},
                now=AT,
                lease_seconds=30,
            )
