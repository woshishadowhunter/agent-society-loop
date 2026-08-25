import hashlib
import json
import unittest

from seed_society.a2a import (
    A2AAmbiguousSubmission,
    A2AHTTPClient,
    A2ALimits,
    A2AProtocolError,
    A2ARemoteExecutor,
)
from seed_society.domain import (
    DelegationRecord,
    DelegationStatus,
    RemoteAgentRegistration,
    Task,
)
from seed_society.storage import SQLiteRepository
from seed_society.tracing import TraceRecorder
from tests.a2a_fake_server import FakeA2AServer


def task_response(state, *, task_id="task-1", parts=None):
    task = {"id": task_id, "status": {"state": state}}
    if parts is not None:
        task["artifacts"] = [{"artifactId": "artifact-1", "parts": parts}]
    return {"task": task}


class A2ADelegationTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")

    def tearDown(self):
        self.repository.close()

    def registration(self, server):
        registration = RemoteAgentRegistration.create(
            "remote-a",
            server.card_url,
            "a" * 64,
            server.interface_url,
            {"analysis": "analyze"},
            tenant="tenant-a",
            allowed_context_sections=("review_feedback",),
            allow_insecure_localhost=True,
        )
        self.repository.save_remote_agent(registration)
        return registration

    def task(self):
        return Task.create(
            "goal-1",
            "task-local",
            "analysis",
            "Analyze the supplied evidence",
            acceptance_criteria={"required_terms": ["evidence"]},
            context={"private": "must-not-leave"},
        )

    def executor(self, server, **kwargs):
        limits = kwargs.pop("limits", A2ALimits(poll_interval=0))
        return A2ARemoteExecutor(
            self.repository,
            self.registration(server),
            A2AHTTPClient(
                allow_insecure_localhost=True,
                limits=limits,
            ),
            tracer=TraceRecorder(self.repository),
            limits=limits,
            **kwargs,
        )

    def test_direct_message_is_persisted_and_context_is_allowlisted(self):
        with FakeA2AServer() as server:
            result = self.executor(server).delegate(
                self.task(),
                {"review_feedback": ["Add evidence"], "knowledge": "private"},
            )

        saved = self.repository.get_delegation(result.delegation_id)
        request = next(item for item in server.requests if item["path"].endswith("message:send"))
        payload = json.loads(request["body"])
        transmitted = payload["message"]["parts"][0]["data"]
        self.assertEqual(result.content, "direct answer")
        self.assertEqual(saved.status, DelegationStatus.COMPLETED)
        self.assertEqual(saved.result_sha256, hashlib.sha256(b"direct answer").hexdigest())
        self.assertTrue(payload["configuration"]["returnImmediately"])
        self.assertEqual(
            payload["configuration"]["acceptedOutputModes"],
            ["text/plain", "application/json"],
        )
        self.assertEqual(transmitted["context"], {"review_feedback": ["Add evidence"]})
        self.assertNotIn("private", json.dumps(payload))

    def test_async_task_polls_and_normalizes_text_and_data(self):
        with FakeA2AServer() as server:
            server.send_response = task_response("TASK_STATE_SUBMITTED")
            server.task_responses = [
                task_response("TASK_STATE_WORKING"),
                task_response(
                    "TASK_STATE_COMPLETED",
                    parts=[{"text": "remote result"}, {"data": {"score": 1}}],
                ),
            ]
            result = self.executor(server).delegate(self.task(), {})

        saved = self.repository.get_delegation(result.delegation_id)
        self.assertEqual(result.content, 'remote result\n{"score":1}')
        self.assertEqual(server.send_count, 1)
        self.assertEqual(server.get_count, 2)
        self.assertEqual(saved.poll_count, 2)
        self.assertEqual(saved.remote_task_id, "task-1")

    def test_accepted_task_resumes_without_resending(self):
        with FakeA2AServer() as server:
            executor = self.executor(server)
            registration = self.repository.get_remote_agent("remote-a")
            delegation = DelegationRecord.create(
                "goal-1", "task-local", 1, registration, "message-1", "b" * 64
            ).advance(DelegationStatus.SUBMITTING).advance(
                DelegationStatus.ACCEPTED,
                remote_task_id="task-1",
                remote_task_state="TASK_STATE_SUBMITTED",
            )
            self.repository.save_delegation(delegation)
            server.task_responses = [
                task_response("TASK_STATE_COMPLETED", parts=[{"text": "resumed"}])
            ]
            result = executor.delegate(self.task(), {})

        self.assertEqual(result.content, "resumed")
        self.assertEqual(server.send_count, 0)
        self.assertEqual(server.get_count, 1)

    def test_completed_task_replays_without_network(self):
        with FakeA2AServer() as server:
            executor = self.executor(server)
            registration = self.repository.get_remote_agent("remote-a")
            delegation = DelegationRecord.create(
                "goal-1", "task-local", 1, registration, "message-1", "b" * 64
            ).advance(DelegationStatus.SUBMITTING).advance(
                DelegationStatus.COMPLETED, result_content="persisted"
            )
            self.repository.save_delegation(delegation)
            result = executor.delegate(self.task(), {})

        self.assertEqual(result.content, "persisted")
        self.assertEqual(server.send_count + server.get_count, 0)

    def test_ambiguous_send_becomes_unknown_and_is_never_resent(self):
        with FakeA2AServer() as server:
            server.close_send_without_response = True
            executor = self.executor(server)
            with self.assertRaises(A2AAmbiguousSubmission):
                executor.delegate(self.task(), {})
            with self.assertRaisesRegex(A2AProtocolError, "unknown"):
                executor.delegate(self.task(), {})

        saved = self.repository.list_delegations("goal-1")[0]
        self.assertEqual(saved.status, DelegationStatus.UNKNOWN)
        self.assertEqual(saved.error_category, "ambiguous_send")
        self.assertEqual(server.send_count, 1)

    def test_poll_budget_exhaustion_attempts_one_cancel(self):
        limits = A2ALimits(max_polls=1, poll_interval=0)
        with FakeA2AServer() as server:
            server.send_response = task_response("TASK_STATE_SUBMITTED")
            server.task_responses = [task_response("TASK_STATE_WORKING")]
            result_error = None
            try:
                self.executor(server, limits=limits).delegate(self.task(), {})
            except A2AProtocolError as error:
                result_error = error

        saved = self.repository.list_delegations("goal-1")[0]
        self.assertIsNotNone(result_error)
        self.assertEqual(saved.status, DelegationStatus.CANCELED)
        self.assertEqual(saved.canceled_by, "deadline")
        self.assertEqual(server.cancel_count, 1)

    def test_explicit_cancel_records_operator(self):
        with FakeA2AServer() as server:
            executor = self.executor(server)
            registration = self.repository.get_remote_agent("remote-a")
            delegation = DelegationRecord.create(
                "goal-1", "task-local", 1, registration, "message-1", "b" * 64
            ).advance(DelegationStatus.SUBMITTING).advance(
                DelegationStatus.ACCEPTED,
                remote_task_id="task-1",
                remote_task_state="TASK_STATE_WORKING",
            )
            self.repository.save_delegation(delegation)
            canceled = executor.cancel(delegation.delegation_id, "operator-a")

        self.assertEqual(canceled.status, DelegationStatus.CANCELED)
        self.assertEqual(canceled.canceled_by, "operator-a")
        self.assertEqual(server.cancel_count, 1)

    def test_interrupted_wrong_identity_and_unsupported_output_fail_closed(self):
        cases = [
            (task_response("TASK_STATE_INPUT_REQUIRED"), None, "interrupted", DelegationStatus.INTERRUPTED),
            (task_response("TASK_STATE_SUBMITTED"), task_response("TASK_STATE_COMPLETED", task_id="other", parts=[{"text": "x"}]), "identity", DelegationStatus.FAILED),
            (task_response("TASK_STATE_COMPLETED", parts=[{"url": "https://private"}]), None, "part", DelegationStatus.FAILED),
        ]
        for response, poll_response, pattern, status in cases:
            with self.subTest(pattern=pattern), FakeA2AServer() as server:
                repository = SQLiteRepository(":memory:")
                try:
                    self.repository.close()
                    self.repository = repository
                    server.send_response = response
                    if poll_response is not None:
                        server.task_responses = [poll_response]
                    with self.assertRaisesRegex(A2AProtocolError, pattern):
                        self.executor(server).delegate(self.task(), {})
                    self.assertEqual(repository.list_delegations()[0].status, status)
                finally:
                    repository.close()
                    self.repository = SQLiteRepository(":memory:")

    def test_oversized_output_fails_before_local_artifact(self):
        limits = A2ALimits(max_result_bytes=4, poll_interval=0)
        with FakeA2AServer() as server:
            with self.assertRaisesRegex(A2AProtocolError, "result size"):
                self.executor(server, limits=limits).delegate(self.task(), {})

        self.assertEqual(
            self.repository.list_delegations("goal-1")[0].status,
            DelegationStatus.FAILED,
        )


if __name__ == "__main__":
    unittest.main()
