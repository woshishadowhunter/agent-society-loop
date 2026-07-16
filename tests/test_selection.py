import unittest

from agent_society_loop.domain import AgentProfile, PerformanceRecord, Task
from agent_society_loop.selection import PerformanceWeightedSelector


class PerformanceWeightedSelectorTests(unittest.TestCase):
    def setUp(self):
        self.selector = PerformanceWeightedSelector()
        self.task = Task.create("g1", "t1", "analysis", "Analyze a market")

    def agent(self, agent_id, task_types=("analysis",), role="worker", enabled=True):
        return AgentProfile(agent_id, role, f"model-{agent_id}", task_types, enabled)

    def test_raises_when_no_agent_is_eligible(self):
        candidates = [self.agent("disabled", enabled=False), self.agent("writer", ("copy",))]

        with self.assertRaisesRegex(LookupError, "eligible"):
            self.selector.select(self.task, candidates, [])

    def test_cold_start_is_neutral_and_ties_are_deterministic(self):
        decision = self.selector.select(
            self.task, [self.agent("z-agent"), self.agent("a-agent")], []
        )

        self.assertEqual(decision.agent_id, "a-agent")
        self.assertEqual(decision.components["success_rate"], 0.5)
        self.assertEqual(decision.components["review_score"], 0.5)
        self.assertEqual(decision.components["confidence"], 0.0)
        self.assertEqual([item["agent_id"] for item in decision.considered], ["a-agent", "z-agent"])

    def test_prefers_stronger_task_specific_history(self):
        candidates = [self.agent("steady"), self.agent("weak")]
        records = [
            PerformanceRecord("steady", "analysis", 10, 9, 92, 100),
            PerformanceRecord("weak", "analysis", 10, 4, 55, 100),
        ]

        decision = self.selector.select(self.task, candidates, records)

        self.assertEqual(decision.agent_id, "steady")
        self.assertGreater(decision.total_score, 0.8)

    def test_ignores_history_from_another_task_type(self):
        records = [PerformanceRecord("z-agent", "copy", 10, 10, 100, 10)]

        decision = self.selector.select(
            self.task, [self.agent("z-agent"), self.agent("a-agent")], records
        )

        self.assertEqual(decision.agent_id, "a-agent")


if __name__ == "__main__":
    unittest.main()
