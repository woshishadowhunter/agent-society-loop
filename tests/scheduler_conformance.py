"""Backend-neutral scheduler conformance contracts."""

from __future__ import annotations

from dataclasses import replace

from agent_society_loop.domain import (
    Artifact,
    Attempt,
    Event,
    Goal,
    GoalStatus,
    PerformanceRecord,
    Review,
    Task,
    TaskStatus,
    Verdict,
)
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


class OutcomeReconciliationContract:
    """Terminal goal state must share the fenced outcome transaction."""

    first: object

    def seed_outcome_graph(self, other_status: TaskStatus = TaskStatus.PENDING):
        goal = replace(
            Goal.create("Goal", "Reconcile", goal_id="reconcile"),
            status=GoalStatus.RUNNING,
        )
        claimed_task = Task.create("reconcile", "claimed", "analysis", "Claimed")
        other_task = replace(
            Task.create("reconcile", "other", "analysis", "Other", position=1),
            status=other_status,
        )
        self.first.save_goal(goal)
        self.first.save_tasks((claimed_task, other_task))
        self.first.register_worker(
            WorkerSession.create(
                "worker-r",
                "session-r",
                ("analysis",),
                now=AT,
                ttl_seconds=60,
            )
        )
        claim = self.first.claim_task(
            "reconcile",
            "claimed",
            "worker-r",
            "session-r",
            "agent-r",
            now=AT,
            lease_seconds=30,
        )
        return claim, claimed_task

    def commit_reconciled_outcome(self, task_status: TaskStatus, other_status: TaskStatus):
        claim, original = self.seed_outcome_graph(other_status)
        passing = task_status == TaskStatus.SUCCEEDED
        artifact = (
            Artifact.create("reconcile", "claimed", "agent-r", "accepted")
            if passing
            else None
        )
        review = Review.create(
            "reconcile",
            "claimed",
            1,
            Verdict.PASS if passing else Verdict.FAIL,
            90 if passing else 0,
            (),
            "accepted" if passing else "not accepted",
        )
        attempt = Attempt.create(
            "reconcile",
            "claimed",
            "agent-r",
            1,
            10,
            artifact.artifact_id if artifact else None,
            review.review_id,
        )
        performance = PerformanceRecord(
            "agent-r", "analysis", 1, int(passing), 90 if passing else 0, 10
        )
        task = replace(
            original,
            status=task_status,
            assigned_agent_id="agent-r",
            artifact_id=artifact.artifact_id if artifact else None,
        )
        self.first.commit_claim_outcome(
            claim,
            task,
            artifact,
            attempt,
            review,
            performance,
            (Event.create("reconcile", "task.attempt_completed", {}),),
            now="2026-07-16T00:00:01+00:00",
        )
        return self.first.get_goal("reconcile"), self.first.list_events("reconcile")

    def test_last_successful_outcome_completes_goal_atomically(self) -> None:
        goal, events = self.commit_reconciled_outcome(
            TaskStatus.SUCCEEDED, TaskStatus.SUCCEEDED
        )

        self.assertEqual(goal.status, GoalStatus.SUCCEEDED)
        self.assertEqual(
            [event.event_type for event in events].count("goal.succeeded"), 1
        )

    def test_failed_outcome_fails_goal_atomically(self) -> None:
        goal, events = self.commit_reconciled_outcome(
            TaskStatus.FAILED, TaskStatus.PENDING
        )

        self.assertEqual(goal.status, GoalStatus.FAILED)
        self.assertIn("claimed", goal.failure_reason)
        self.assertEqual([event.event_type for event in events].count("goal.failed"), 1)

    def test_blocked_outcome_blocks_goal_atomically(self) -> None:
        goal, events = self.commit_reconciled_outcome(
            TaskStatus.BLOCKED, TaskStatus.PENDING
        )

        self.assertEqual(goal.status, GoalStatus.BLOCKED)
        self.assertIn("claimed", goal.failure_reason)
        self.assertEqual([event.event_type for event in events].count("goal.blocked"), 1)

    def test_nonterminal_outcome_keeps_goal_running(self) -> None:
        goal, events = self.commit_reconciled_outcome(
            TaskStatus.SUCCEEDED, TaskStatus.PENDING
        )

        self.assertEqual(goal.status, GoalStatus.RUNNING)
        self.assertFalse(
            {"goal.succeeded", "goal.failed", "goal.blocked"}
            & {event.event_type for event in events}
        )
