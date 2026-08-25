import unittest

from seed_society.a2a import (
    A2AHTTPClient,
    A2ALimits,
    A2ARemoteExecutor,
    A2ARemoteWorker,
)
from seed_society.a2a_governance import DelegationPolicyEvaluator
from seed_society.domain import (
    AgentProfile,
    EvaluationRun,
    GoalStatus,
    PerformanceRecord,
    RemoteAgentRegistration,
    Review,
    Task,
    TaskStatus,
    Verdict,
)
from seed_society.engine import LoopEngine
from seed_society.memory import MemoryManager
from seed_society.ports import WorkerBlocked
from seed_society.selection import PerformanceWeightedSelector
from seed_society.storage import SQLiteRepository
from seed_society.tracing import TraceRecorder
from tests.a2a_fake_server import FakeA2AServer


class OneTaskPlanner:
    def plan(self, goal, context):
        return [
            Task.create(
                goal.goal_id,
                "analysis-task",
                "analysis",
                "Analyze evidence",
                acceptance_criteria={"required_terms": ["answer"]},
            )
        ]


class RecordingWorker:
    def __init__(self, agent_id, content="local answer"):
        self.agent_id = agent_id
        self.content = content
        self.calls = 0

    def execute(self, task, context):
        self.calls += 1
        return self.content


class RecordingReviewer:
    def __init__(self):
        self.calls = 0

    def review(self, task, artifact, attempt_no):
        self.calls += 1
        return Review.create(
            task.goal_id,
            task.task_id,
            attempt_no,
            Verdict.PASS,
            95,
            [],
            "accepted locally",
        )


class BlockingWorker:
    agent_id = "remote-a"

    def execute(self, task, context):
        raise WorkerBlocked(
            "remote delegation requires operator review",
            {"delegation_id": "delegation-safe", "card_sha256": "a" * 64},
        )


class A2AEngineTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.local = RecordingWorker("local-a")
        self.reviewer = RecordingReviewer()
        self.repository.save_agent(
            AgentProfile("local-a", "worker", "local-model", ("analysis",))
        )

    def tearDown(self):
        self.repository.close()

    def engine(self, workers):
        return LoopEngine(
            planner=OneTaskPlanner(),
            workers=workers,
            reviewer=self.reviewer,
            repository=self.repository,
            memory=MemoryManager(self.repository),
            selector=PerformanceWeightedSelector(),
        )

    def remote_profile(self, model_id="a2a:" + "a" * 64):
        profile = AgentProfile(
            "remote-a", "worker", model_id, ("analysis",), execution_kind="a2a"
        )
        self.repository.save_agent(profile)
        return profile

    def deploy_remote(self, profile):
        run = EvaluationRun(
            run_id="deploy-remote",
            task_type="analysis",
            benchmark_digest="benchmark",
            champion_agent_id="local-a",
            champion_model_id="local-model",
            challenger_agent_id=profile.agent_id,
            challenger_model_id=profile.model_id,
            case_count=5,
            metrics={"passed": True},
            recommended=True,
            failed_gates=(),
        )
        self.repository.save_evaluation_run(run)
        self.repository.promote_evaluation(run.run_id, "operator")

    def test_unpromoted_remote_is_excluded_from_performance_routing(self):
        self.remote_profile()
        remote = RecordingWorker("remote-a", "remote answer")
        self.repository.save_performance(
            PerformanceRecord("local-a", "analysis", 10, 1, 10, 1000)
        )
        self.repository.save_performance(
            PerformanceRecord("remote-a", "analysis", 10, 10, 100, 1)
        )
        engine = self.engine({"local-a": self.local, "remote-a": remote})
        goal = engine.create_goal("Guard", "Keep remote experimental", goal_id="unpromoted")

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.SUCCEEDED)
        self.assertEqual(self.local.calls, 1)
        self.assertEqual(remote.calls, 0)

    def test_promoted_remote_without_loaded_worker_blocks_without_fallback(self):
        profile = self.remote_profile()
        self.deploy_remote(profile)
        engine = self.engine({"local-a": self.local})
        goal = engine.create_goal("Guard", "Require explicit remote loading", goal_id="not-loaded")

        report = engine.run(goal.goal_id)

        self.assertEqual(report.status, GoalStatus.BLOCKED)
        self.assertEqual(report.attempts, 0)
        self.assertIn("deployed champion unavailable", report.reason)
        self.assertEqual(self.local.calls, 0)

    def test_promoted_remote_output_still_passes_local_review(self):
        with FakeA2AServer() as server:
            registration = RemoteAgentRegistration.create(
                "remote-a",
                server.card_url,
                "a" * 64,
                server.interface_url,
                {"analysis": "analyze"},
                allow_insecure_localhost=True,
            )
            self.repository.save_remote_agent(registration)
            profile = self.remote_profile(registration.model_id)
            self.deploy_remote(profile)
            limits = A2ALimits(poll_interval=0)
            remote = A2ARemoteWorker(
                A2ARemoteExecutor(
                    self.repository,
                    registration,
                    A2AHTTPClient(
                        allow_insecure_localhost=True,
                        limits=limits,
                    ),
                    tracer=TraceRecorder(self.repository),
                    limits=limits,
                )
            )
            engine = self.engine({"local-a": self.local, "remote-a": remote})
            goal = engine.create_goal("Delegate", "Use promoted specialist", goal_id="remote-ok")
            report = engine.run(goal.goal_id)

        artifact = self.repository.list_artifacts(goal.goal_id)[0]
        self.assertEqual(report.status, GoalStatus.SUCCEEDED)
        self.assertEqual(server.send_count, 1)
        self.assertEqual(artifact.content, "direct answer")
        self.assertEqual(self.reviewer.calls, 1)

    def test_worker_blocked_records_attempt_evidence_and_blocks_goal(self):
        profile = self.remote_profile()
        self.deploy_remote(profile)
        engine = self.engine({"local-a": self.local, "remote-a": BlockingWorker()})
        goal = engine.create_goal("Guard", "Preserve remote uncertainty", goal_id="remote-blocked")

        report = engine.run(goal.goal_id)

        task = self.repository.list_tasks(goal.goal_id)[0]
        review = self.repository.list_reviews(goal.goal_id)[0]
        blocked = [
            event
            for event in self.repository.list_events(goal.goal_id)
            if event.event_type == "goal.blocked"
        ][-1]
        self.assertEqual(report.status, GoalStatus.BLOCKED)
        self.assertEqual((report.attempts, report.actions), (1, 1))
        self.assertEqual(task.status, TaskStatus.BLOCKED)
        self.assertEqual(review.score, 0)
        self.assertEqual(blocked.payload["delegation_id"], "delegation-safe")

    def test_policy_denial_blocks_loop_with_decision_evidence_and_no_send(self):
        with FakeA2AServer() as server:
            registration = RemoteAgentRegistration.create(
                "remote-a",
                server.card_url,
                "a" * 64,
                server.interface_url,
                {"analysis": "analyze"},
                allow_insecure_localhost=True,
            )
            self.repository.save_remote_agent(registration)
            profile = self.remote_profile(registration.model_id)
            self.deploy_remote(profile)
            limits = A2ALimits(poll_interval=0)
            remote = A2ARemoteWorker(
                A2ARemoteExecutor(
                    self.repository,
                    registration,
                    A2AHTTPClient(
                        allow_insecure_localhost=True,
                        limits=limits,
                    ),
                    limits=limits,
                    policy_evaluator=DelegationPolicyEvaluator(self.repository),
                    require_policy=True,
                )
            )
            goal = self.engine(
                {"local-a": self.local, "remote-a": remote}
            ).create_goal("Delegate", "Require policy", goal_id="policy-denied")
            report = self.engine(
                {"local-a": self.local, "remote-a": remote}
            ).run(goal.goal_id)

        decision = self.repository.list_policy_decisions(goal.goal_id)[0]
        blocked = [
            event
            for event in self.repository.list_events(goal.goal_id)
            if event.event_type == "goal.blocked"
        ][-1]
        self.assertEqual(report.status, GoalStatus.BLOCKED)
        self.assertEqual(server.send_count, 0)
        self.assertEqual(blocked.payload["policy_decision_id"], decision.decision_id)
        self.assertEqual(blocked.payload["policy_verdict"], "DENY")


if __name__ == "__main__":
    unittest.main()
