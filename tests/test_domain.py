import unittest
from dataclasses import replace

from agent_society_loop.domain import (
    Goal,
    GoalStatus,
    Review,
    RunBudget,
    Task,
    Verdict,
    transition_goal,
    validate_task_graph,
)


class DomainValidationTests(unittest.TestCase):
    def test_review_score_must_be_between_zero_and_one_hundred(self):
        with self.assertRaisesRegex(ValueError, "score"):
            Review.create("g1", "t1", 1, Verdict.FAIL, 101, [], "invalid")

    def test_budget_requires_positive_action_limit(self):
        with self.assertRaisesRegex(ValueError, "max_actions"):
            RunBudget(max_actions=0)

    def test_goal_transition_rejects_skipping_planning(self):
        goal = Goal.create("Ship a release", "Build and publish it")

        with self.assertRaisesRegex(ValueError, "created.*running"):
            transition_goal(goal, GoalStatus.RUNNING)

    def test_terminal_goal_cannot_transition(self):
        goal = replace(
            Goal.create("Ship a release", "Build and publish it"),
            status=GoalStatus.SUCCEEDED,
        )

        with self.assertRaisesRegex(ValueError, "terminal"):
            transition_goal(goal, GoalStatus.RUNNING)


class TaskGraphTests(unittest.TestCase):
    def task(self, task_id, dependencies=()):
        return Task.create(
            goal_id="g1",
            task_id=task_id,
            task_type="analysis",
            description=task_id,
            dependencies=dependencies,
        )

    def test_rejects_duplicate_task_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_task_graph([self.task("a"), self.task("a")])

    def test_rejects_missing_dependency(self):
        with self.assertRaisesRegex(ValueError, "missing dependency"):
            validate_task_graph([self.task("a", ("missing",))])

    def test_rejects_dependency_cycles(self):
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_task_graph([self.task("a", ("b",)), self.task("b", ("a",))])

    def test_accepts_ordered_acyclic_tasks(self):
        tasks = [self.task("a"), self.task("b", ("a",))]

        self.assertEqual(validate_task_graph(tasks), tasks)


if __name__ == "__main__":
    unittest.main()
