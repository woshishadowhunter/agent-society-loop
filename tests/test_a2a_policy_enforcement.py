import unittest
from datetime import datetime, timezone

from agent_society_loop.a2a import (
    A2ALimits,
    A2AProtocolError,
    A2ARemoteExecutor,
    A2ARemoteWorker,
    PolicyDenied,
)
from agent_society_loop.a2a_governance import DelegationPolicyEvaluator
from agent_society_loop.domain import (
    ConformanceAttestation,
    DelegationPolicy,
    DelegationRecord,
    DelegationRule,
    DelegationStatus,
    PolicyActivation,
    PolicyVerdict,
    RemoteAgentRegistration,
    Task,
)
from agent_society_loop.ports import WorkerBlocked
from agent_society_loop.storage import SQLiteRepository


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


class RecordingClient:
    def __init__(self, response=None, *, limits=None, get_responses=None):
        self.limits = limits or A2ALimits(poll_interval=0)
        self.response = response or {
            "message": {"parts": [{"text": "answer"}]}
        }
        self.sent = []
        self.get_responses = list(get_responses or [])
        self.get_count = 0
        self.cancel_count = 0

    def send_message(self, interface_url, payload, *, tenant=""):
        self.sent.append((interface_url, payload, tenant))
        return self.response

    def get_task(self, interface_url, task_id, *, tenant=""):
        self.get_count += 1
        if self.get_responses:
            return self.get_responses.pop(0)
        return {
            "task": {
                "id": task_id,
                "status": {"state": "TASK_STATE_WORKING"},
            }
        }

    def cancel_task(self, interface_url, task_id, *, tenant=""):
        self.cancel_count += 1
        return {
            "task": {
                "id": task_id,
                "status": {"state": "TASK_STATE_CANCELED"},
            }
        }


class A2APolicyEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.registration = RemoteAgentRegistration.create(
            "remote-a",
            "https://agent.test/.well-known/agent-card.json",
            "a" * 64,
            "https://agent.test/a2a",
            {"analysis": "analyze"},
            tenant="tenant-a",
            allowed_context_sections=("review_feedback",),
        )
        self.repository.save_remote_agent(self.registration)
        self.task = Task.create(
            "goal-1",
            "task-1",
            "analysis",
            "Analyze evidence",
            acceptance_criteria={"required_terms": ["answer"]},
        )

    def tearDown(self):
        self.repository.close()

    def install_governance(self, **rule_limits):
        values = {
            "max_request_bytes": 65536,
            "max_result_bytes": 131072,
            "max_polls": 12,
            "total_timeout_seconds": 45,
        }
        values.update(rule_limits)
        rule = DelegationRule.create(
            "analysis-v1",
            ["analysis"],
            ["remote-a"],
            ["a" * 64],
            allowed_context_sections=["review_feedback"],
            required_attestation_kinds=["a2a-tck"],
            max_attestation_age_hours=168,
            **values,
        )
        policy = DelegationPolicy.create("production", 1, [rule])
        self.repository.save_policy(policy)
        self.repository.activate_policy(
            PolicyActivation.create("analysis", policy, "operator")
        )
        attestation = ConformanceAttestation.create(
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
        self.repository.save_attestation(attestation)
        return policy

    def evaluator(self):
        return DelegationPolicyEvaluator(self.repository, now=lambda: NOW)

    def executor(self, client, *, evaluator=None, limits=None, require_policy=True):
        effective_limits = limits or client.limits
        return A2ARemoteExecutor(
            self.repository,
            self.registration,
            client,
            limits=effective_limits,
            policy_evaluator=evaluator,
            require_policy=require_policy,
        )

    def test_required_policy_without_evaluator_is_rejected_at_composition(self):
        with self.assertRaisesRegex(ValueError, "policy evaluator"):
            self.executor(RecordingClient(), evaluator=None)

    def test_deny_is_persisted_before_network_and_creates_no_delegation(self):
        client = RecordingClient()
        executor = self.executor(client, evaluator=self.evaluator())

        with self.assertRaises(PolicyDenied) as caught:
            executor.delegate(self.task, {"review_feedback": ["private"]}, attempt_no=1)

        self.assertEqual(caught.exception.decision.verdict, PolicyVerdict.DENY)
        self.assertIn("no_active_policy", caught.exception.decision.reason_codes)
        self.assertEqual(client.sent, [])
        self.assertEqual(self.repository.list_delegations(), [])
        self.assertEqual(
            self.repository.list_policy_decisions("goal-1"),
            [caught.exception.decision],
        )

    def test_allow_binds_decision_and_limits_context_before_send(self):
        policy = self.install_governance(max_polls=8, total_timeout_seconds=30)
        client = RecordingClient()
        limits = A2ALimits(
            max_request_bytes=32768,
            max_result_bytes=65536,
            max_polls=10,
            total_timeout=40,
            poll_interval=0,
        )
        executor = self.executor(
            client, evaluator=self.evaluator(), limits=limits
        )

        result = executor.delegate(
            self.task,
            {"review_feedback": ["keep"], "knowledge": "do not send"},
            attempt_no=1,
        )

        decision = self.repository.list_policy_decisions("goal-1")[0]
        delegation = self.repository.get_delegation(result.delegation_id)
        transmitted = client.sent[0][1]["message"]["parts"][0]["data"]
        self.assertEqual(decision.verdict, PolicyVerdict.ALLOW)
        self.assertEqual(decision.policy_digest, policy.policy_digest)
        self.assertEqual((decision.max_polls, decision.total_timeout_seconds), (8, 30))
        self.assertEqual(delegation.policy_decision_id, decision.decision_id)
        self.assertEqual(result.policy_decision_id, decision.decision_id)
        self.assertEqual(result.policy_digest, policy.policy_digest)
        self.assertEqual(
            transmitted["context"], {"review_feedback": ["keep"]}
        )

    def test_policy_result_and_poll_limits_override_broader_runtime(self):
        self.install_governance(max_result_bytes=4, max_polls=1)
        client = RecordingClient()
        executor = self.executor(client, evaluator=self.evaluator())
        with self.assertRaisesRegex(A2AProtocolError, "result size"):
            executor.delegate(self.task, {}, attempt_no=1)
        self.assertEqual(
            self.repository.list_delegations()[0].status, DelegationStatus.FAILED
        )

        repository = SQLiteRepository(":memory:")
        try:
            self.repository.close()
            self.repository = repository
            self.repository.save_remote_agent(self.registration)
            self.install_governance(max_result_bytes=1024, max_polls=1)
            polling = RecordingClient(
                {
                    "task": {
                        "id": "remote-task",
                        "status": {"state": "TASK_STATE_SUBMITTED"},
                    }
                }
            )
            with self.assertRaisesRegex(A2AProtocolError, "poll budget"):
                self.executor(polling, evaluator=self.evaluator()).delegate(
                    self.task, {}, attempt_no=1
                )
            self.assertEqual(polling.get_count, 1)
            self.assertEqual(polling.cancel_count, 1)
        finally:
            repository.close()
            self.repository = SQLiteRepository(":memory:")

    def test_policy_request_limit_stops_after_allow_but_before_send(self):
        self.install_governance(max_request_bytes=128)
        client = RecordingClient()

        with self.assertRaisesRegex(A2AProtocolError, "payload exceeds"):
            self.executor(client, evaluator=self.evaluator()).delegate(
                self.task, {"review_feedback": ["large enough"]}, attempt_no=1
            )

        self.assertEqual(client.sent, [])
        self.assertEqual(self.repository.list_delegations(), [])
        self.assertEqual(
            self.repository.list_policy_decisions("goal-1")[0].verdict,
            PolicyVerdict.ALLOW,
        )

    def test_policy_bound_completed_delegation_replays_after_policy_removal(self):
        self.install_governance()
        evaluator = self.evaluator()
        decision = evaluator.decide(
            self.task, self.registration, 1, A2ALimits(poll_interval=0)
        )
        delegation = DelegationRecord.create(
            "goal-1",
            "task-1",
            1,
            self.registration,
            "message-1",
            "d" * 64,
            policy_decision=decision,
        ).advance(DelegationStatus.SUBMITTING).advance(
            DelegationStatus.COMPLETED, result_content="persisted"
        )
        self.repository.save_delegation(delegation)
        accepted_task = Task.create(
            "goal-1", "task-2", "analysis", "Resume analysis"
        )
        accepted_decision = evaluator.decide(
            accepted_task, self.registration, 1, A2ALimits(poll_interval=0)
        )
        accepted = DelegationRecord.create(
            "goal-1",
            "task-2",
            1,
            self.registration,
            "message-2",
            "e" * 64,
            policy_decision=accepted_decision,
        ).advance(DelegationStatus.SUBMITTING).advance(
            DelegationStatus.ACCEPTED,
            remote_task_id="remote-task-2",
            remote_task_state="TASK_STATE_WORKING",
        )
        self.repository.save_delegation(accepted)
        self.repository.connection.execute("DELETE FROM policy_activations")
        self.repository.connection.commit()
        client = RecordingClient(
            get_responses=[
                {
                    "task": {
                        "id": "remote-task-2",
                        "status": {"state": "TASK_STATE_COMPLETED"},
                        "artifacts": [{"parts": [{"text": "resumed"}]}],
                    }
                }
            ]
        )

        result = self.executor(client, evaluator=evaluator).delegate(
            self.task, {}, attempt_no=1
        )
        resumed = self.executor(client, evaluator=evaluator).delegate(
            accepted_task, {}, attempt_no=1
        )

        self.assertEqual(result.content, "persisted")
        self.assertEqual(result.policy_decision_id, decision.decision_id)
        self.assertEqual(resumed.content, "resumed")
        self.assertEqual(resumed.policy_decision_id, accepted_decision.decision_id)
        self.assertEqual(client.sent, [])
        self.assertEqual(client.get_count, 1)

    def test_remote_worker_converts_policy_deny_to_durable_block_evidence(self):
        worker = A2ARemoteWorker(
            self.executor(RecordingClient(), evaluator=self.evaluator())
        )

        with self.assertRaises(WorkerBlocked) as caught:
            worker.execute(self.task, {})

        decision = self.repository.list_policy_decisions("goal-1")[0]
        self.assertEqual(caught.exception.evidence["policy_decision_id"], decision.decision_id)
        self.assertEqual(caught.exception.evidence["policy_verdict"], "DENY")
        self.assertEqual(caught.exception.evidence["policy_reason"], "no_active_policy")


if __name__ == "__main__":
    unittest.main()
