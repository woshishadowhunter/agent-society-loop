import unittest

from seed_society.deterministic import (
    CriteriaReviewer,
    QuantumMugPlanner,
    TemplateWorker,
    build_demo_engine,
)
from seed_society.domain import Goal, GoalStatus, Task, Verdict
from seed_society.storage import SQLiteRepository


class DeterministicScenarioTests(unittest.TestCase):
    def test_planner_creates_four_dependency_ordered_specialist_tasks(self):
        goal = Goal.create("Quantum mug", "Prepare launch materials", goal_id="demo")

        tasks = QuantumMugPlanner().plan(goal, {})

        self.assertEqual(
            [task.task_type for task in tasks],
            ["market_analysis", "visual_concept", "launch_copy", "integration"],
        )
        self.assertEqual(tasks[-1].dependencies, ("market", "visual", "copy"))

    def test_reviewer_reports_each_unmet_structured_criterion(self):
        task = Task.create(
            "g1",
            "market",
            "market_analysis",
            "Analyze",
            acceptance_criteria={
                "required_terms": ["competitor"],
                "required_sections": ["SWOT"],
                "min_sources": 3,
                "min_length": 80,
            },
        )

        review = CriteriaReviewer().review(task, "short draft", 1)

        self.assertEqual(review.verdict, Verdict.FAIL)
        self.assertEqual(len(review.defects), 4)
        self.assertLess(review.score, 70)

    def test_worker_uses_review_feedback_to_repair_market_report(self):
        goal = Goal.create("Quantum mug", "Prepare launch materials", goal_id="g1")
        task = QuantumMugPlanner().plan(goal, {})[0]
        worker = TemplateWorker("market-analyst")
        reviewer = CriteriaReviewer()
        first = worker.execute(task, {"review_feedback": [], "dependency_artifacts": {}})
        failed = reviewer.review(task, first, 1)

        repaired = worker.execute(
            task,
            {
                "review_feedback": [{"summary": failed.summary, "defects": []}],
                "dependency_artifacts": {},
            },
        )
        passed = reviewer.review(task, repaired, 2)

        self.assertEqual(failed.verdict, Verdict.FAIL)
        self.assertEqual(passed.verdict, Verdict.PASS)

    def test_demo_engine_reaches_success_with_a_visible_retry(self):
        repository = SQLiteRepository(":memory:")
        engine = build_demo_engine(repository)
        goal = engine.create_goal(
            "Quantum Coffee Mug Launch",
            "Create market, visual, copy, and integrated launch materials",
            goal_id="quantum-demo",
        )

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.SUCCEEDED)
        self.assertGreaterEqual(report.retries, 1)
        self.assertEqual(report.tasks_succeeded, 4)
        self.assertEqual(len(repository.list_agents()), 4)
        repository.close()


if __name__ == "__main__":
    unittest.main()

