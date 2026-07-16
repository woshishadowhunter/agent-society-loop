import tempfile
import unittest
from pathlib import Path
import sqlite3

from agent_society_loop.domain import (
    AgentProfile,
    ApprovalRequest,
    Artifact,
    Attempt,
    Event,
    Goal,
    PerformanceRecord,
    Review,
    Task,
    SpanStatus,
    TraceSpan,
    Verdict,
)
from agent_society_loop.storage import SQLiteRepository


class SQLiteRepositoryTests(unittest.TestCase):
    def test_approval_and_span_survive_database_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "society.db"
            repository = SQLiteRepository(path)
            approval = ApprovalRequest.create(
                "goal", "task", "write_file", {"path": "a"}, "write"
            )
            span = TraceSpan.start(
                "goal", "task", "agent", "tool", "write_file"
            ).finish(SpanStatus.OK)

            repository.save_approval(approval)
            repository.save_span(span)
            repository.close()

            reopened = SQLiteRepository(path)
            self.assertEqual(reopened.get_approval(approval.approval_id), approval)
            self.assertEqual(reopened.list_approvals("goal"), [approval])
            self.assertEqual(reopened.list_spans("goal"), [span])
            reopened.close()

    def test_state_survives_database_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "society.db"
            goal = Goal.create("Launch", "Create launch materials", goal_id="g1")
            task = Task.create("g1", "t1", "market_analysis", "Analyze market")
            artifact = Artifact.create("g1", "t1", "analyst-a", "three sources")
            agent = AgentProfile("analyst-a", "worker", "model-a", ("market_analysis",))
            performance = PerformanceRecord(
                "analyst-a", "market_analysis", attempts=2, passes=1,
                avg_score=75.0, avg_duration_ms=120.0,
                recent_results=({"passed": True, "score": 90.0},),
            )

            repository = SQLiteRepository(path)
            repository.save_goal(goal)
            repository.save_tasks([task])
            repository.save_artifact(artifact)
            repository.append_event(Event.create("g1", "goal.created", {"title": "Launch"}))
            repository.save_agent(agent)
            repository.save_performance(performance)
            repository.close()

            reopened = SQLiteRepository(path)
            self.assertEqual(reopened.get_goal("g1"), goal)
            self.assertEqual(reopened.list_tasks("g1"), [task])
            self.assertEqual(reopened.list_artifacts("g1", "t1"), [artifact])
            self.assertEqual(reopened.list_events("g1")[0].event_type, "goal.created")
            self.assertEqual(reopened.list_agents(), [agent])
            self.assertEqual(
                reopened.get_performance("analyst-a", "market_analysis"), performance
            )
            reopened.close()

    def test_missing_goal_returns_none(self):
        repository = SQLiteRepository(":memory:")
        self.assertIsNone(repository.get_goal("missing"))
        repository.close()

    def test_same_task_id_is_isolated_between_goals(self):
        repository = SQLiteRepository(":memory:")
        first = Goal.create("First", "First goal", goal_id="first")
        second = Goal.create("Second", "Second goal", goal_id="second")
        repository.save_goal(first)
        repository.save_goal(second)
        repository.save_task(Task.create("first", "research", "analysis", "First research"))
        repository.save_task(Task.create("second", "research", "analysis", "Second research"))

        self.assertEqual(repository.list_tasks("first")[0].description, "First research")
        self.assertEqual(repository.list_tasks("second")[0].description, "Second research")
        repository.close()

    def test_attempt_review_performance_and_event_commit_atomically(self):
        repository = SQLiteRepository(":memory:")
        goal = Goal.create("Atomic", "Record one outcome", goal_id="atomic")
        repository.save_goal(goal)
        review = Review.create("atomic", "task", 1, Verdict.PASS, 88, [], "Accepted")
        attempt = Attempt.create(
            "atomic", "task", "worker", 1, 42.0, "artifact-1", review.review_id
        )
        performance = PerformanceRecord("worker", "analysis", 1, 1, 88.0, 42.0)
        event = Event.create("atomic", "task.attempt_completed", {"task_id": "task"})

        repository.save_attempt_outcome(attempt, review, performance, event)

        self.assertEqual(repository.list_attempts("atomic"), [attempt])
        self.assertEqual(repository.list_reviews("atomic"), [review])
        self.assertEqual(repository.get_performance("worker", "analysis"), performance)
        self.assertEqual(repository.list_events("atomic")[-1].event_type, "task.attempt_completed")
        repository.close()

    def test_attempt_outcome_rolls_back_all_writes_when_one_insert_fails(self):
        repository = SQLiteRepository(":memory:")
        goal = Goal.create("Atomic", "Reject a partial write", goal_id="rollback")
        repository.save_goal(goal)
        review = Review.create("rollback", "task", 1, Verdict.FAIL, 10, [], "Existing")
        repository.save_review(review)
        attempt = Attempt.create(
            "rollback", "task", "worker", 1, 12.0, None, review.review_id
        )
        performance = PerformanceRecord("worker", "analysis", 1, 0, 10.0, 12.0)
        event = Event.create("rollback", "task.attempt_completed", {"task_id": "task"})

        with self.assertRaises(sqlite3.IntegrityError):
            repository.save_attempt_outcome(attempt, review, performance, event)

        self.assertEqual(repository.list_attempts("rollback"), [])
        self.assertIsNone(repository.get_performance("worker", "analysis"))
        self.assertEqual(repository.list_events("rollback"), [])
        repository.close()


if __name__ == "__main__":
    unittest.main()
