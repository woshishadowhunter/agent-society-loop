import unittest

from seed_society.domain import (
    AgentProfile,
    Artifact,
    EvaluationRun,
    EvaluationStatus,
    GoalStatus,
    PerformanceRecord,
    Review,
    Task,
    Verdict,
)
from seed_society.engine import LoopEngine
from seed_society.memory import MemoryManager
from seed_society.selection import PerformanceWeightedSelector
from seed_society.storage import SQLiteRepository


def evaluation_run(*, recommended=True, champion="champion", challenger="challenger"):
    return EvaluationRun(
        run_id=f"run-{champion}-{challenger}-{int(recommended)}",
        task_type="analysis",
        benchmark_digest="digest",
        champion_agent_id=champion,
        champion_model_id="model-a",
        challenger_agent_id=challenger,
        challenger_model_id="model-b",
        case_count=5,
        metrics={"mean_score_delta": 4.0},
        recommended=recommended,
        failed_gates=() if recommended else ("mean_score",),
    )


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.repository.save_agent(
            AgentProfile("champion", "worker", "model-a", ("analysis",))
        )
        self.repository.save_agent(
            AgentProfile("challenger", "worker", "model-b", ("analysis",))
        )

    def tearDown(self):
        self.repository.close()

    def test_recommendation_does_not_change_deployment(self):
        run = evaluation_run()
        self.repository.save_evaluation_run(run)

        self.assertTrue(run.recommended)
        self.assertIsNone(self.repository.get_deployment("analysis"))

    def test_explicit_promotion_atomically_updates_run_and_deployment(self):
        run = evaluation_run()
        self.repository.save_evaluation_run(run)

        deployment = self.repository.promote_evaluation(run.run_id, "operator")

        promoted = self.repository.get_evaluation_run(run.run_id)
        self.assertEqual(promoted.status, EvaluationStatus.PROMOTED)
        self.assertEqual(promoted.promoted_by, "operator")
        self.assertEqual(deployment.champion_agent_id, "challenger")
        self.assertEqual(deployment.champion_model_id, "model-b")
        self.assertEqual(self.repository.get_deployment("analysis"), deployment)

    def test_nonrecommended_or_changed_candidate_cannot_be_promoted(self):
        rejected = evaluation_run(recommended=False)
        self.repository.save_evaluation_run(rejected)
        with self.assertRaisesRegex(ValueError, "not recommended"):
            self.repository.promote_evaluation(rejected.run_id, "operator")

        accepted = evaluation_run()
        self.repository.save_evaluation_run(accepted)
        self.repository.save_agent(
            AgentProfile("challenger", "worker", "model-c", ("analysis",))
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            self.repository.promote_evaluation(accepted.run_id, "operator")

    def test_promotion_rejects_stale_champion_against_active_deployment(self):
        first = evaluation_run()
        self.repository.save_evaluation_run(first)
        self.repository.promote_evaluation(first.run_id, "operator")
        self.repository.save_agent(
            AgentProfile("next", "worker", "model-b", ("analysis",))
        )
        stale = EvaluationRun(
            run_id="stale-run",
            task_type="analysis",
            benchmark_digest="other",
            champion_agent_id="champion",
            champion_model_id="model-a",
            challenger_agent_id="next",
            challenger_model_id="model-b",
            case_count=5,
            metrics={},
            recommended=True,
            failed_gates=(),
        )
        self.repository.save_evaluation_run(stale)

        with self.assertRaisesRegex(ValueError, "stale champion"):
            self.repository.promote_evaluation(stale.run_id, "operator")


class OneTaskPlanner:
    def plan(self, goal, context):
        return [Task.create(goal.goal_id, "task", "analysis", "Analyze")]


class RecordingWorker:
    def __init__(self, agent_id):
        self.agent_id = agent_id
        self.calls = 0

    def execute(self, task, context):
        self.calls += 1
        return f"result from {self.agent_id}"


class PassingReviewer:
    def review(self, task, artifact, attempt_no):
        return Review.create(
            task.goal_id, task.task_id, attempt_no, Verdict.PASS, 90, [], "accepted"
        )


class DeploymentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.champion = RecordingWorker("champion")
        self.challenger = RecordingWorker("challenger")
        self.repository.save_agent(
            AgentProfile("champion", "worker", "model-a", ("analysis",))
        )
        self.repository.save_agent(
            AgentProfile("challenger", "worker", "model-b", ("analysis",))
        )
        self.repository.save_performance(
            PerformanceRecord("champion", "analysis", 10, 2, 40, 100)
        )
        self.repository.save_performance(
            PerformanceRecord("challenger", "analysis", 10, 10, 99, 50)
        )

    def tearDown(self):
        self.repository.close()

    def engine(self):
        return LoopEngine(
            planner=OneTaskPlanner(),
            workers={"champion": self.champion, "challenger": self.challenger},
            reviewer=PassingReviewer(),
            repository=self.repository,
            memory=MemoryManager(self.repository),
            selector=PerformanceWeightedSelector(),
        )

    def deploy_champion(self):
        run = EvaluationRun(
            run_id="deploy-champion",
            task_type="analysis",
            benchmark_digest="digest",
            champion_agent_id="challenger",
            champion_model_id="model-b",
            challenger_agent_id="champion",
            challenger_model_id="model-a",
            case_count=5,
            metrics={},
            recommended=True,
            failed_gates=(),
        )
        self.repository.save_evaluation_run(run)
        self.repository.promote_evaluation(run.run_id, "operator")

    def test_active_deployment_overrides_higher_historical_score(self):
        self.deploy_champion()
        engine = self.engine()
        goal = engine.create_goal("Route", "Use approved model", goal_id="routed")

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.SUCCEEDED)
        self.assertEqual(self.champion.calls, 1)
        self.assertEqual(self.challenger.calls, 0)
        started = next(
            event
            for event in self.repository.list_events(goal.goal_id)
            if event.event_type == "task.attempt_started"
        )
        self.assertEqual(
            started.payload["deployment_source_run_id"], "deploy-champion"
        )

    def test_unavailable_deployed_champion_blocks_without_fallback(self):
        self.deploy_champion()
        self.repository.save_agent(
            AgentProfile("champion", "worker", "model-a", ("analysis",), enabled=False)
        )
        engine = self.engine()
        goal = engine.create_goal("Route", "Do not fall back", goal_id="blocked")

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.BLOCKED)
        self.assertIn("deployed champion unavailable", report.reason)
        self.assertEqual(self.champion.calls, 0)
        self.assertEqual(self.challenger.calls, 0)

    def test_task_type_without_deployment_keeps_performance_routing(self):
        engine = self.engine()
        goal = engine.create_goal("Route", "Use social memory", goal_id="weighted")

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.SUCCEEDED)
        self.assertEqual(self.champion.calls, 0)
        self.assertEqual(self.challenger.calls, 1)


if __name__ == "__main__":
    unittest.main()
