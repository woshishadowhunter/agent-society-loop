import unittest
from dataclasses import replace

from seed_society.domain import (
    AgentProfile,
    ApprovalStatus,
    Artifact,
    Defect,
    Goal,
    GoalStatus,
    Review,
    RunBudget,
    Task,
    TaskStatus,
    ToolRisk,
    Verdict,
    transition_goal,
)
from seed_society.engine import LoopEngine
from seed_society.memory import MemoryManager
from seed_society.selection import PerformanceWeightedSelector
from seed_society.storage import SQLiteRepository
from seed_society.tools import (
    DefaultToolPolicy,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
)


class TwoTaskPlanner:
    def plan(self, goal, context):
        return [
            Task.create(goal.goal_id, "research", "research", "Find evidence", position=1),
            Task.create(
                goal.goal_id,
                "copy",
                "copywriting",
                "Write launch copy",
                dependencies=("research",),
                position=2,
            ),
        ]


class DuplicatePlanner:
    def plan(self, goal, context):
        return [
            Task.create(goal.goal_id, "same", "research", "First"),
            Task.create(goal.goal_id, "same", "research", "Second"),
        ]


class RecordingWorker:
    agent_id = "worker-a"

    def __init__(self):
        self.calls = []

    def execute(self, task, context):
        self.calls.append((task.task_id, context))
        if task.task_id == "research":
            return "Evidence from three sources"
        if context["review_feedback"]:
            return "Clear customer benefit supported by research"
        return "Generic launch draft"


class FeedbackReviewer:
    def review(self, task, artifact, attempt_no):
        if task.task_id == "copy" and "benefit" not in artifact:
            return Review.create(
                task.goal_id,
                task.task_id,
                attempt_no,
                Verdict.FAIL,
                55,
                [Defect("headline", "benefit missing", "add a customer benefit")],
                "Add a customer benefit",
            )
        return Review.create(
            task.goal_id, task.task_id, attempt_no, Verdict.PASS, 90, [], "Accepted"
        )


class AlwaysFailReviewer:
    def review(self, task, artifact, attempt_no):
        return Review.create(
            task.goal_id, task.task_id, attempt_no, Verdict.FAIL, 20, [], "Rejected"
        )


class SingleTaskPlanner:
    def __init__(self, max_attempts=2):
        self.max_attempts = max_attempts

    def plan(self, goal, context):
        return [
            Task.create(
                goal.goal_id,
                "only",
                "research",
                "Do work",
                max_attempts=self.max_attempts,
            )
        ]


class WriteTool:
    name = "write_candidate"
    description = "Write a candidate artifact"
    risk = ToolRisk.WRITE
    input_schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
        "additionalProperties": False,
    }

    def __init__(self):
        self.calls = 0

    def invoke(self, arguments):
        self.calls += 1
        return arguments["content"]


class ApprovalWorker:
    agent_id = "worker-a"

    def __init__(self, executor):
        self.executor = executor

    def execute(self, task, context):
        result = self.executor.execute(
            "write_candidate",
            {"content": "approved artifact"},
            ToolContext(task.goal_id, task.task_id, self.agent_id),
        )
        return result.output


class LoopEngineTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.memory = MemoryManager(self.repository)
        self.worker = RecordingWorker()
        self.repository.save_agent(
            AgentProfile("worker-a", "worker", "deterministic", ("*",))
        )

    def tearDown(self):
        self.repository.close()

    def engine(self, planner, reviewer=None, budget=None):
        return LoopEngine(
            planner=planner,
            workers={"worker-a": self.worker},
            reviewer=reviewer or FeedbackReviewer(),
            repository=self.repository,
            memory=self.memory,
            selector=PerformanceWeightedSelector(),
            budget=budget or RunBudget(),
        )

    def approval_engine(self):
        tool = WriteTool()
        worker = ApprovalWorker(
            ToolExecutor(
                ToolRegistry([tool]),
                DefaultToolPolicy(),
                self.repository,
            )
        )
        engine = LoopEngine(
            planner=SingleTaskPlanner(),
            workers={"worker-a": worker},
            reviewer=FeedbackReviewer(),
            repository=self.repository,
            memory=self.memory,
            selector=PerformanceWeightedSelector(),
        )
        return engine, tool

    def test_pending_tool_approval_pauses_without_attempt_then_resumes(self):
        engine, tool = self.approval_engine()
        goal = engine.create_goal("Maintain", "Prepare candidate", goal_id="approval")

        paused = engine.run(goal.goal_id)

        self.assertEqual(paused.status, GoalStatus.PAUSED)
        self.assertEqual(paused.attempts, 0)
        self.assertEqual(paused.actions, 0)
        self.assertEqual(tool.calls, 0)
        span_names = {span.name for span in self.repository.list_spans(goal.goal_id)}
        self.assertIn("goal.created", span_names)
        self.assertIn("goal.planning", span_names)
        self.assertIn("approval.requested", span_names)
        approval = self.repository.list_approvals(goal.goal_id)[0]

        still_paused = engine.resume(goal.goal_id)
        self.assertEqual(still_paused.status, GoalStatus.PAUSED)
        self.assertEqual(len(self.repository.list_approvals(goal.goal_id)), 1)

        resolved = engine.resolve_approval(
            approval.approval_id, approved=True, decided_by="operator"
        )
        self.assertEqual(
            self.repository.get_goal(goal.goal_id).status, GoalStatus.RUNNING
        )
        completed = engine.resume(goal.goal_id)

        self.assertEqual(resolved.status, ApprovalStatus.APPROVED)
        self.assertEqual(completed.status, GoalStatus.SUCCEEDED)
        self.assertEqual(completed.attempts, 1)
        self.assertEqual(completed.actions, 1)
        self.assertEqual(tool.calls, 1)
        span_names = {span.name for span in self.repository.list_spans(goal.goal_id)}
        self.assertIn("approval.approved", span_names)
        self.assertIn("goal.succeeded", span_names)

    def test_rejected_tool_approval_fails_without_attempt(self):
        engine, tool = self.approval_engine()
        goal = engine.create_goal("Maintain", "Prepare candidate", goal_id="rejected")
        engine.run(goal.goal_id)
        approval = self.repository.list_approvals(goal.goal_id)[0]

        resolved = engine.resolve_approval(
            approval.approval_id, approved=False, decided_by="operator"
        )
        report = engine.resume(goal.goal_id)

        self.assertEqual(resolved.status, ApprovalStatus.REJECTED)
        self.assertEqual(report.status, GoalStatus.FAILED)
        self.assertEqual(report.attempts, 0)
        self.assertEqual(report.actions, 0)
        self.assertIn("rejected", report.reason)
        self.assertEqual(tool.calls, 0)

    def test_runs_outer_and_inner_loops_to_success(self):
        engine = self.engine(TwoTaskPlanner())
        goal = engine.create_goal("Launch", "Produce launch materials", goal_id="g1")

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.SUCCEEDED)
        self.assertEqual([call[0] for call in self.worker.calls], ["research", "copy", "copy"])
        self.assertEqual(report.attempts, 3)
        self.assertEqual(report.retries, 1)
        self.assertEqual(report.tasks_succeeded, 2)
        self.assertEqual(len(self.repository.list_attempts("g1")), 3)
        copy_context = self.worker.calls[-1][1]
        self.assertEqual(copy_context["dependency_artifacts"]["research"], "Evidence from three sources")
        self.assertEqual(copy_context["review_feedback"][-1]["summary"], "Add a customer benefit")
        events = [event.event_type for event in self.repository.list_events("g1")]
        self.assertEqual(events.count("task.attempt_completed"), 3)
        self.assertIn("task.review_failed", events)
        self.assertEqual(events[-1], "goal.succeeded")
        performance = self.repository.get_performance("worker-a", "copywriting")
        self.assertEqual((performance.attempts, performance.passes), (2, 1))

    def test_plan_persists_work_without_executing_and_is_idempotent(self):
        engine = self.engine(TwoTaskPlanner())
        goal = engine.create_goal("Queued", "Execute elsewhere", goal_id="queued")

        first = engine.plan(goal.goal_id)
        second = engine.plan(goal.goal_id)

        self.assertEqual(first.status, GoalStatus.RUNNING)
        self.assertEqual(second.status, GoalStatus.RUNNING)
        self.assertEqual(first.tasks_total, 2)
        self.assertEqual(first.attempts, 0)
        self.assertEqual(self.worker.calls, [])
        self.assertEqual(
            [task.status for task in self.repository.list_tasks("queued")],
            [TaskStatus.PENDING, TaskStatus.PENDING],
        )
        events = [event.event_type for event in self.repository.list_events("queued")]
        self.assertEqual(events.count("goal.planned"), 1)
        self.assertEqual(events.count("goal.running"), 1)

    def test_invalid_plan_fails_goal_before_work_starts(self):
        engine = self.engine(DuplicatePlanner())
        goal = engine.create_goal("Launch", "Produce launch materials", goal_id="g2")

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.FAILED)
        self.assertIn("duplicate", report.reason)
        self.assertEqual(self.worker.calls, [])

    def test_create_goal_rejects_duplicate_identifier_without_overwrite(self):
        engine = self.engine(SingleTaskPlanner())
        engine.create_goal("Original", "Keep this goal", goal_id="same-goal")

        with self.assertRaisesRegex(ValueError, "already exists"):
            engine.create_goal("Replacement", "Must not overwrite", goal_id="same-goal")

        self.assertEqual(self.repository.get_goal("same-goal").title, "Original")

    def test_attempt_exhaustion_fails_goal(self):
        engine = self.engine(SingleTaskPlanner(max_attempts=2), AlwaysFailReviewer())
        goal = engine.create_goal("Fail", "Exercise retry limit", goal_id="g3")

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.FAILED)
        self.assertEqual(report.attempts, 2)
        self.assertIn("exhausted", report.reason)

    def test_action_budget_blocks_unbounded_retry(self):
        engine = self.engine(
            SingleTaskPlanner(max_attempts=3),
            AlwaysFailReviewer(),
            RunBudget(max_actions=1),
        )
        goal = engine.create_goal("Bounded", "Stop after budget", goal_id="g4")

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.BLOCKED)
        self.assertEqual(report.attempts, 1)
        self.assertIn("action budget", report.reason)

    def test_resume_skips_completed_tasks(self):
        goal = Goal.create("Resume", "Continue unfinished work", goal_id="g5")
        goal = transition_goal(goal, GoalStatus.PLANNING)
        goal = transition_goal(goal, GoalStatus.RUNNING)
        done = replace(
            Task.create("g5", "research", "research", "Research", position=1),
            status=TaskStatus.SUCCEEDED,
            artifact_id="artifact-existing",
        )
        pending = Task.create(
            "g5", "copy", "copywriting", "Copy", dependencies=("research",), position=2
        )
        self.repository.save_goal(goal)
        self.repository.save_tasks([done, pending])
        self.repository.save_artifact(
            Artifact("artifact-existing", "g5", "research", "worker-a", "Existing evidence")
        )
        engine = self.engine(TwoTaskPlanner())

        report = engine.resume("g5")

        self.assertEqual(report.status, GoalStatus.SUCCEEDED)
        self.assertEqual([call[0] for call in self.worker.calls], ["copy", "copy"])


if __name__ == "__main__":
    unittest.main()
