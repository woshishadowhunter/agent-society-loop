import unittest

from agent_society_loop.a2a import (
    A2ABenchmarkRunner,
    A2AHTTPClient,
    A2ALimits,
    A2ARemoteExecutor,
)
from agent_society_loop.domain import (
    BenchmarkCase,
    CandidateIdentity,
    RemoteAgentRegistration,
)
from agent_society_loop.storage import SQLiteRepository
from tests.a2a_fake_server import FakeA2AServer


class A2ABenchmarkRunnerTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")

    def tearDown(self):
        self.repository.close()

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


if __name__ == "__main__":
    unittest.main()
