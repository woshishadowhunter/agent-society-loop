import unittest
from datetime import datetime, timezone

from seed_society.a2a import (
    A2ABenchmarkRunner,
    A2AHTTPClient,
    A2ALimits,
    A2ARemoteExecutor,
)
from seed_society.a2a_governance import DelegationPolicyEvaluator
from seed_society.domain import (
    BenchmarkCase,
    CandidateIdentity,
    ConformanceAttestation,
    DelegationPolicy,
    DelegationRule,
    PolicyActivation,
    RemoteAgentRegistration,
)
from seed_society.storage import SQLiteRepository
from tests.a2a_fake_server import FakeA2AServer


class A2ABenchmarkRunnerTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")

    def tearDown(self):
        self.repository.close()

    def install_governance(self):
        rule = DelegationRule.create(
            "analysis-v1",
            ["analysis"],
            ["remote-a"],
            ["a" * 64],
            allowed_context_sections=["review_feedback"],
            max_request_bytes=65536,
            max_result_bytes=131072,
            max_polls=12,
            total_timeout_seconds=45,
            required_attestation_kinds=["a2a-tck"],
            max_attestation_age_hours=168,
        )
        policy = DelegationPolicy.create("benchmark", 1, [rule])
        self.repository.save_policy(policy)
        self.repository.activate_policy(
            PolicyActivation.create("analysis", policy, "operator")
        )
        self.repository.save_attestation(
            ConformanceAttestation.create(
                agent_id="remote-a",
                card_sha256="a" * 64,
                kind="a2a-tck",
                report_sha256="b" * 64,
                source_revision="c" * 40,
                tool_version="1.0.0",
                spec_version="1.0",
                observed_at="2026-07-16T10:00:00+00:00",
                passed=True,
                metrics={"must_compatibility": 100.0, "http_json_failed": 0},
            )
        )
        return policy

    def test_execution_links_benchmark_to_delegation_and_card(self):
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
            limits = A2ALimits(poll_interval=0)
            executor = A2ARemoteExecutor(
                self.repository,
                registration,
                A2AHTTPClient(allow_insecure_localhost=True, limits=limits),
                limits=limits,
            )
            runner = A2ABenchmarkRunner(
                {"remote-a": executor}, benchmark_id="benchmark-fixed"
            )
            candidate = CandidateIdentity("remote-a", registration.model_id)
            case = BenchmarkCase.create(
                "case-1",
                "analysis",
                {"prompt": "Analyze evidence"},
                {"required_terms": ["answer"]},
            )

            execution = runner(candidate, case)

        saved = self.repository.list_delegations()[0]
        self.assertEqual(execution.output, "direct answer")
        self.assertEqual(execution.evidence["delegation_id"], saved.delegation_id)
        self.assertEqual(execution.evidence["card_sha256"], registration.card_sha256)
        self.assertEqual(saved.goal_id, "benchmark-benchmark-fixed")

    def test_candidate_model_must_match_pinned_card(self):
        with FakeA2AServer() as server:
            registration = RemoteAgentRegistration.create(
                "remote-a",
                server.card_url,
                "a" * 64,
                server.interface_url,
                {"analysis": "analyze"},
                allow_insecure_localhost=True,
            )
            limits = A2ALimits(poll_interval=0)
            executor = A2ARemoteExecutor(
                self.repository,
                registration,
                A2AHTTPClient(allow_insecure_localhost=True, limits=limits),
                limits=limits,
            )
            runner = A2ABenchmarkRunner({"remote-a": executor})
            case = BenchmarkCase.create(
                "case-1", "analysis", {"prompt": "Analyze"}, {"minimum_score": 1}
            )

            with self.assertRaisesRegex(ValueError, "identity"):
                runner(CandidateIdentity("remote-a", "a2a:" + "b" * 64), case)

        self.assertEqual(server.send_count, 0)

    def test_governed_benchmark_links_policy_decision_evidence(self):
        with FakeA2AServer() as server:
            registration = RemoteAgentRegistration.create(
                "remote-a",
                server.card_url,
                "a" * 64,
                server.interface_url,
                {"analysis": "analyze"},
                allowed_context_sections=("review_feedback",),
                allow_insecure_localhost=True,
            )
            self.repository.save_remote_agent(registration)
            policy = self.install_governance()
            limits = A2ALimits(poll_interval=0)
            executor = A2ARemoteExecutor(
                self.repository,
                registration,
                A2AHTTPClient(allow_insecure_localhost=True, limits=limits),
                limits=limits,
                policy_evaluator=DelegationPolicyEvaluator(
                    self.repository,
                    now=lambda: datetime(
                        2026, 7, 16, 12, 0, tzinfo=timezone.utc
                    ),
                ),
                require_policy=True,
            )
            case = BenchmarkCase.create(
                "case-1",
                "analysis",
                {"prompt": "Analyze"},
                {"minimum_score": 1},
            )

            execution = A2ABenchmarkRunner(
                {"remote-a": executor}, benchmark_id="governed"
            )(CandidateIdentity("remote-a", registration.model_id), case)

        decision = self.repository.list_policy_decisions()[0]
        self.assertEqual(execution.evidence["policy_decision_id"], decision.decision_id)
        self.assertEqual(execution.evidence["policy_digest"], policy.policy_digest)
        self.assertEqual(execution.evidence["policy_rule_id"], "analysis-v1")


if __name__ == "__main__":
    unittest.main()
