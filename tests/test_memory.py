import unittest

from seed_society.domain import Artifact, Goal, Review, Task, Verdict
from seed_society.memory import MemoryManager
from seed_society.storage import SQLiteRepository


class MemoryManagerTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.memory = MemoryManager(self.repository)

    def tearDown(self):
        self.repository.close()

    def test_context_contains_dependency_artifact_and_latest_review_feedback(self):
        goal = Goal.create("Launch", "Launch a product", goal_id="g1")
        first = Task.create("g1", "research", "research", "Research")
        second = Task.create(
            "g1", "copy", "copywriting", "Write copy", dependencies=("research",)
        )
        self.repository.save_goal(goal)
        self.repository.save_tasks([first, second])
        self.repository.save_artifact(
            Artifact.create("g1", "research", "researcher", "Market evidence")
        )
        self.repository.save_review(
            Review.create("g1", "copy", 1, Verdict.FAIL, 55, [], "Add a clear benefit")
        )

        context = self.memory.build_context(goal, second)

        self.assertEqual(context["dependency_artifacts"]["research"], "Market evidence")
        self.assertEqual(context["review_feedback"][-1]["summary"], "Add a clear benefit")

    def test_knowledge_search_ranks_tag_and_term_overlap(self):
        self.memory.add_knowledge("Brand voice", "Use plain, warm language", ("copy",))
        best_id = self.memory.add_knowledge(
            "Market report", "Coffee mug market growth and competitor data", ("market",)
        )

        results = self.memory.search_knowledge("market competitor", ("market",))

        self.assertEqual(results[0].knowledge_id, best_id)

    def test_record_outcome_updates_task_specific_social_memory(self):
        self.memory.record_outcome("analyst", "analysis", True, 90, 100)
        self.memory.record_outcome("analyst", "analysis", False, 60, 200)

        performance = self.repository.get_performance("analyst", "analysis")

        self.assertEqual(performance.attempts, 2)
        self.assertEqual(performance.passes, 1)
        self.assertEqual(performance.avg_score, 75.0)
        self.assertEqual(performance.avg_duration_ms, 150.0)
        self.assertEqual(len(performance.recent_results), 2)


if __name__ == "__main__":
    unittest.main()
