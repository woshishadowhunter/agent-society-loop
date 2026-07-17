import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event as ThreadEvent, enumerate as enumerate_threads
from pathlib import Path

from agent_society_loop.domain import (
    ApprovalRequest,
    Goal,
    GoalStatus,
    OutboxMessage,
    Review,
    Task,
    TaskStatus,
    Verdict,
)
from agent_society_loop.storage import SQLiteRepository
from agent_society_loop.ports import WorkerBlocked, WorkerExecution
from agent_society_loop.scheduler import WorkerSession
from agent_society_loop.tools import ApprovalRequired
from agent_society_loop.worker_service import (
    WorkerRunStatus,
    WorkerService,
    WorkerServiceConfig,
)


AT = "2026-07-16T00:00:00+00:00"


class StaticWorker:
    def __init__(self, agent_id="agent-a", output="accepted artifact"):
        self.agent_id = agent_id
        self.output = output
        self.calls = []

    def execute(self, task, context):
        self.calls.append((task, context))
        return self.output


class PassingReviewer:
    def review(self, task, artifact, attempt_no):
        return Review.create(
            task.goal_id,
            task.task_id,
            attempt_no,
            Verdict.PASS,
            95,
            (),
            "Accepted",
        )


class FailingReviewer:
    def review(self, task, artifact, attempt_no):
        return Review.create(
            task.goal_id,
            task.task_id,
            attempt_no,
            Verdict.FAIL,
            30,
            (),
            "Revise",
        )


class BlockedWorker(StaticWorker):
    def execute(self, task, context):
        raise WorkerBlocked(
            "remote outcome is ambiguous",
            {"delegation_id": "delegation-a", "status": "unknown"},
        )


class ApprovalWorker(StaticWorker):
    def execute(self, task, context):
        raise ApprovalRequired(
            ApprovalRequest.create(
                task.goal_id,
                task.task_id,
                "write_file",
                {"path": "candidate.txt"},
                "write tool requires approval",
            )
        )


class SlowWorker(StaticWorker):
    def execute(self, task, context):
        time.sleep(0.06)
        return super().execute(task, context)


class SupersedingWorker(StaticWorker):
    def __init__(self, path):
        super().__init__()
        self.path = path

    def execute(self, task, context):
        repository = SQLiteRepository(self.path)
        try:
            repository.register_worker(
                WorkerSession.create(
                    "worker-a",
                    "replacement-session",
                    ("analysis",),
                    now="2026-07-16T00:00:01+00:00",
                    ttl_seconds=60,
                )
            )
        finally:
            repository.close()
        return "locally successful but stale"


class StopAfterWorker(StaticWorker):
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event

    def execute(self, task, context):
        self.stop_event.set()
        return super().execute(task, context)


class OutboxWorker(StaticWorker):
    def __init__(self, message):
        super().__init__()
        self.message = message

    def execute(self, task, context):
        return WorkerExecution(self.output, (self.message,))


class WorkerServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "worker.db"
        self.repository = SQLiteRepository(self.path)
        goal = replace(
            Goal.create("Queued", "Run in worker", goal_id="queued"),
            status=GoalStatus.RUNNING,
        )
        self.repository.save_goal(goal)
        self.repository.save_task(
            Task.create("queued", "task", "analysis", "Analyze", max_attempts=2)
        )
        self.worker = StaticWorker()

    def tearDown(self):
        self.repository.close()
        self.directory.cleanup()

    def build_service(
        self, *, worker=None, reviewer=None, config=None, repository_factory=None,
        clock=None, stop_event=None
    ):
        worker = worker or self.worker
        return WorkerService(
            repository=self.repository,
            repository_factory=repository_factory or (lambda: SQLiteRepository(self.path)),
            workers={"agent-a": worker},
            assignments={"analysis": "agent-a"},
            reviewer=reviewer or PassingReviewer(),
            config=config
            or WorkerServiceConfig(
                worker_id="worker-a", session_id="session-a",
                heartbeat_ttl_seconds=60, lease_seconds=30,
                renew_interval_seconds=10, poll_interval_seconds=0.01,
            ),
            clock=clock or (lambda: AT),
            stop_event=stop_event,
        )

    def test_run_once_claims_reviews_and_commits_a_complete_outcome(self):
        result = self.build_service().run_once()

        self.assertEqual(result.status, WorkerRunStatus.COMMITTED)
        self.assertEqual((result.goal_id, result.task_id), ("queued", "task"))
        self.assertEqual(result.task_status, TaskStatus.SUCCEEDED)
        self.assertEqual(self.repository.get_goal("queued").status, GoalStatus.SUCCEEDED)
        self.assertEqual(self.repository.list_tasks("queued")[0].status, TaskStatus.SUCCEEDED)
        self.assertEqual(len(self.repository.list_artifacts("queued", "task")), 1)
        self.assertEqual(len(self.repository.list_reviews("queued", "task")), 1)
        self.assertEqual(len(self.repository.list_attempts("queued", "task")), 1)
        self.assertEqual(len(self.worker.calls), 1)

    def test_default_clock_uses_repository_authoritative_time(self):
        calls = []

        def scheduler_now():
            calls.append(True)
            return AT

        self.repository.scheduler_now = scheduler_now
        service = WorkerService(
            repository=self.repository,
            repository_factory=lambda: SQLiteRepository(self.path),
            workers={"agent-a": self.worker},
            assignments={"analysis": "agent-a"},
            reviewer=PassingReviewer(),
            config=WorkerServiceConfig(
                worker_id="worker-a",
                session_id="session-a",
                heartbeat_ttl_seconds=60,
                lease_seconds=30,
                renew_interval_seconds=10,
                poll_interval_seconds=0.01,
            ),
        )

        result = service.run_once()

        self.assertEqual(result.status, WorkerRunStatus.COMMITTED)
        self.assertGreaterEqual(len(calls), 3)

    def test_worker_outbox_intents_commit_atomically_with_outcome(self):
        message = OutboxMessage.create(
            "webhook", "queued:task:notify", {"event": "task.completed"}, now=AT
        )

        result = self.build_service(worker=OutboxWorker(message)).run_once()

        self.assertEqual(result.status, WorkerRunStatus.COMMITTED)
        self.assertEqual(self.repository.list_outbox(), [message])
        self.assertEqual(len(self.repository.list_artifacts("queued", "task")), 1)

    def test_failed_review_does_not_commit_worker_outbox_intents(self):
        message = OutboxMessage.create(
            "webhook", "queued:task:notify", {"event": "task.completed"}, now=AT
        )

        result = self.build_service(
            worker=OutboxWorker(message), reviewer=FailingReviewer()
        ).run_once()

        self.assertEqual(result.task_status, TaskStatus.PENDING)
        self.assertEqual(self.repository.list_outbox(), [])

    def test_outbox_conflict_rolls_back_complete_claim_outcome(self):
        existing = OutboxMessage.create(
            "webhook", "queued:task:notify", {"event": "existing"}, now=AT
        )
        self.repository.enqueue_outbox(existing)
        changed = OutboxMessage.create(
            "webhook", "queued:task:notify", {"event": "changed"}, now=AT
        )

        with self.assertRaisesRegex(ValueError, "idempotency payload"):
            self.build_service(worker=OutboxWorker(changed)).run_once()

        self.assertEqual(self.repository.list_outbox(), [existing])
        self.assertEqual(self.repository.list_artifacts("queued", "task"), [])
        self.assertEqual(self.repository.list_reviews("queued", "task"), [])
        self.assertEqual(self.repository.list_attempts("queued", "task"), [])

    def test_failed_review_commits_audit_records_and_schedules_retry(self):
        result = self.build_service(reviewer=FailingReviewer()).run_once()

        self.assertEqual(result.status, WorkerRunStatus.COMMITTED)
        self.assertEqual(result.task_status, TaskStatus.PENDING)
        self.assertEqual(self.repository.get_goal("queued").status, GoalStatus.RUNNING)
        self.assertEqual(self.repository.list_tasks("queued")[0].status, TaskStatus.PENDING)
        self.assertEqual(len(self.repository.list_artifacts("queued", "task")), 1)
        self.assertEqual(len(self.repository.list_reviews("queued", "task")), 1)
        self.assertEqual(len(self.repository.list_attempts("queued", "task")), 1)

    def test_failed_review_at_attempt_limit_fails_task_and_goal(self):
        task = self.repository.list_tasks("queued")[0]
        self.repository.save_task(replace(task, max_attempts=1))

        result = self.build_service(reviewer=FailingReviewer()).run_once()

        self.assertEqual(result.task_status, TaskStatus.FAILED)
        self.assertEqual(self.repository.get_goal("queued").status, GoalStatus.FAILED)
        self.assertEqual(self.repository.list_tasks("queued")[0].status, TaskStatus.FAILED)

    def test_worker_blocked_commits_fail_closed_task_and_goal(self):
        result = self.build_service(worker=BlockedWorker()).run_once()

        self.assertEqual(result.status, WorkerRunStatus.COMMITTED)
        self.assertEqual(result.task_status, TaskStatus.BLOCKED)
        self.assertEqual(self.repository.get_goal("queued").status, GoalStatus.BLOCKED)
        self.assertEqual(self.repository.list_artifacts("queued", "task"), [])
        review = self.repository.list_reviews("queued", "task")[0]
        self.assertEqual(review.verdict, Verdict.FAIL)
        events = self.repository.list_events("queued")
        completed = next(
            event for event in events if event.event_type == "task.attempt_completed"
        )
        self.assertEqual(completed.payload["delegation_id"], "delegation-a")
        self.assertEqual(completed.payload["status"], "unknown")

    def test_no_ready_task_returns_idle_without_creating_claim(self):
        task = self.repository.list_tasks("queued")[0]
        self.repository.save_task(replace(task, status=TaskStatus.SUCCEEDED))

        result = self.build_service().run_once()

        self.assertEqual(result.status, WorkerRunStatus.IDLE)
        self.assertEqual(self.repository.list_claims("queued"), [])
        self.assertEqual(self.worker.calls, [])

    def test_slow_execution_renews_lease_on_a_separate_repository_connection(self):
        opened = []
        started = time.monotonic()

        def factory():
            opened.append(time.monotonic())
            return SQLiteRepository(self.path)

        def clock():
            current = datetime(2026, 7, 16, tzinfo=timezone.utc) + timedelta(
                seconds=time.monotonic() - started
            )
            return current.isoformat()

        result = self.build_service(
            worker=SlowWorker(),
            repository_factory=factory,
            clock=clock,
            config=WorkerServiceConfig(
                worker_id="worker-a", session_id="session-a",
                heartbeat_ttl_seconds=3, lease_seconds=2,
                renew_interval_seconds=0.01, poll_interval_seconds=0.01,
            ),
        ).run_once()

        self.assertEqual(result.status, WorkerRunStatus.COMMITTED)
        self.assertGreaterEqual(len(opened), 1)
        claim = self.repository.list_claims("queued")[0]
        self.assertGreater(claim.renewed_at, claim.acquired_at)

    def test_superseded_session_discards_locally_successful_output(self):
        result = self.build_service(worker=SupersedingWorker(self.path)).run_once()

        self.assertEqual(result.status, WorkerRunStatus.LOST)
        self.assertIn("superseded", result.reason)
        self.assertEqual(self.repository.list_artifacts("queued", "task"), [])
        self.assertEqual(self.repository.list_reviews("queued", "task"), [])
        self.assertEqual(self.repository.list_attempts("queued", "task"), [])
        self.assertEqual(self.repository.list_tasks("queued")[0].status, TaskStatus.RUNNING)

    def test_approval_pauses_without_consuming_an_attempt(self):
        result = self.build_service(worker=ApprovalWorker()).run_once()

        self.assertEqual(result.status, WorkerRunStatus.PAUSED)
        self.assertEqual(result.task_status, TaskStatus.PENDING)
        self.assertEqual(self.repository.get_goal("queued").status, GoalStatus.PAUSED)
        self.assertEqual(self.repository.list_tasks("queued")[0].status, TaskStatus.PENDING)
        self.assertEqual(self.repository.list_claims("queued")[0].status.value, "released")
        self.assertEqual(len(self.repository.list_approvals("queued")), 1)
        self.assertEqual(self.repository.list_artifacts("queued", "task"), [])
        self.assertEqual(self.repository.list_reviews("queued", "task"), [])
        self.assertEqual(self.repository.list_attempts("queued", "task"), [])

    def test_outcome_construction_error_stops_the_lease_thread(self):
        service = self.build_service()
        original_memory = service.memory

        class ExplodingMemory:
            def build_context(self, goal, task):
                return original_memory.build_context(goal, task)

            def calculate_outcome(self, *args, **kwargs):
                raise RuntimeError("outcome construction failed")

        service.memory = ExplodingMemory()

        with self.assertRaisesRegex(RuntimeError, "outcome construction failed"):
            service.run_once()

        self.assertFalse(
            any(
                thread.is_alive() and thread.name.startswith("lease-")
                for thread in enumerate_threads()
            )
        )

    def test_run_respects_max_tasks_without_claiming_more_work(self):
        self.repository.save_task(
            Task.create("queued", "task-2", "analysis", "Second", position=1)
        )

        result = self.build_service().run(max_tasks=1)

        self.assertEqual(result.status, WorkerRunStatus.COMMITTED)
        statuses = [task.status for task in self.repository.list_tasks("queued")]
        self.assertEqual(statuses.count(TaskStatus.SUCCEEDED), 1)
        self.assertEqual(statuses.count(TaskStatus.PENDING), 1)
        self.assertEqual(len(self.repository.list_attempts("queued")), 1)

    def test_stop_during_execution_drains_current_claim_before_exit(self):
        self.repository.save_task(
            Task.create("queued", "task-2", "analysis", "Second", position=1)
        )
        stop_event = ThreadEvent()

        result = self.build_service(
            worker=StopAfterWorker(stop_event), stop_event=stop_event
        ).run()

        self.assertEqual(result.status, WorkerRunStatus.STOPPED)
        statuses = [task.status for task in self.repository.list_tasks("queued")]
        self.assertEqual(statuses.count(TaskStatus.SUCCEEDED), 1)
        self.assertEqual(statuses.count(TaskStatus.PENDING), 1)
        self.assertEqual(len(self.repository.list_attempts("queued")), 1)


if __name__ == "__main__":
    unittest.main()
