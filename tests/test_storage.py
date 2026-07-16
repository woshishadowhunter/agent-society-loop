import tempfile
import unittest
from pathlib import Path

from agent_society_loop.domain import (
    AgentProfile,
    Artifact,
    Event,
    Goal,
    PerformanceRecord,
    Task,
)
from agent_society_loop.storage import SQLiteRepository


class SQLiteRepositoryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

