import json
import tempfile
import unittest
from pathlib import Path

from agent_society_loop.domain import (
    AgentProfile,
    CandidateExecution,
    DelegationRecord,
    DelegationStatus,
    EvaluationOutcome,
    RemoteAgentRegistration,
)
from agent_society_loop.storage import SQLiteRepository


class A2ADomainTests(unittest.TestCase):
    def registration(self):
        return RemoteAgentRegistration.create(
            "remote-a",
            "https://agent.test/.well-known/agent-card.json",
            "a" * 64,
            "https://agent.test/a2a",
            {"analysis": "analyze"},
            auth_env="A2A_TOKEN",
            allowed_context_sections=("review_feedback", "dependency_artifacts"),
        )

    def test_registration_derives_identity_and_normalizes_policy(self):
        registration = self.registration()

        self.assertEqual(registration.model_id, f"a2a:{'a' * 64}")
        self.assertEqual(registration.task_types, ("analysis",))
        self.assertEqual(
            registration.allowed_context_sections,
            ("dependency_artifacts", "review_feedback"),
        )

    def test_registration_rejects_invalid_digest_mapping_env_and_context(self):
        values = [
            ({"card_sha256": "short"}, "SHA-256"),
            ({"skill_by_task_type": {}}, "skill"),
            ({"auth_env": "bad-name"}, "environment"),
            ({"allowed_context_sections": ("private",)}, "context"),
        ]
        for replacements, pattern in values:
            arguments = {
                "agent_id": "remote-a",
                "card_url": "https://agent.test/.well-known/agent-card.json",
                "card_sha256": "a" * 64,
                "interface_url": "https://agent.test/a2a",
                "skill_by_task_type": {"analysis": "analyze"},
            }
            arguments.update(replacements)
            with self.subTest(replacements=replacements), self.assertRaisesRegex(
                ValueError, pattern
            ):
                RemoteAgentRegistration.create(**arguments)

    def test_delegation_enforces_state_graph_and_result_identity(self):
        delegation = DelegationRecord.create(
            "goal", "task", 1, self.registration(), "message-1", "b" * 64
        )

        submitting = delegation.advance(DelegationStatus.SUBMITTING)
        accepted = submitting.advance(
            DelegationStatus.ACCEPTED,
            remote_task_id="remote-task",
            remote_task_state="TASK_STATE_WORKING",
        )
        polled = accepted.advance(
            DelegationStatus.ACCEPTED,
            remote_task_state="TASK_STATE_WORKING",
            increment_poll=True,
        )
        completed = polled.advance(
            DelegationStatus.COMPLETED,
            remote_task_state="TASK_STATE_COMPLETED",
            result_content="answer",
        )

        self.assertEqual(polled.poll_count, 1)
        self.assertEqual(completed.result_sha256, _sha256("answer"))
        with self.assertRaisesRegex(ValueError, "terminal"):
            completed.advance(DelegationStatus.ACCEPTED)
        with self.assertRaisesRegex(ValueError, "transition"):
            delegation.advance(DelegationStatus.COMPLETED, result_content="answer")

    def test_unknown_submission_is_terminal_and_requires_sanitized_category(self):
        submitting = DelegationRecord.create(
            "goal", "task", 1, self.registration(), "message-1", "b" * 64
        ).advance(DelegationStatus.SUBMITTING)

        unknown = submitting.advance(
            DelegationStatus.UNKNOWN, error_category="submission_timeout"
        )

        self.assertEqual(unknown.error_category, "submission_timeout")
        with self.assertRaisesRegex(ValueError, "terminal"):
            unknown.advance(DelegationStatus.SUBMITTING)


class A2AStorageTests(unittest.TestCase):
    def registration(self):
        return RemoteAgentRegistration.create(
            "remote-a",
            "https://agent.test/.well-known/agent-card.json",
            "a" * 64,
            "https://agent.test/a2a",
            {"analysis": "analyze"},
        )

    def test_registration_and_delegation_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a2a.db"
            repository = SQLiteRepository(path)
            registration = self.registration()
            delegation = DelegationRecord.create(
                "goal", "task", 1, registration, "message-1", "b" * 64
            )
            repository.save_remote_agent(registration)
            repository.save_delegation(delegation)
            repository.close()

            reopened = SQLiteRepository(path)
            self.assertEqual(reopened.get_remote_agent("remote-a"), registration)
            self.assertEqual(reopened.list_remote_agents(), [registration])
            self.assertEqual(reopened.get_delegation(delegation.delegation_id), delegation)
            self.assertEqual(
                reopened.get_attempt_delegation("goal", "task", 1, "remote-a"),
                delegation,
            )
            self.assertEqual(reopened.list_delegations("goal"), [delegation])
            reopened.close()

    def test_registration_identity_and_attempt_identity_cannot_be_replaced(self):
        repository = SQLiteRepository(":memory:")
        registration = self.registration()
        repository.save_remote_agent(registration)
        delegation = DelegationRecord.create(
            "goal", "task", 1, registration, "message-1", "b" * 64
        )
        repository.save_delegation(delegation)

        changed = RemoteAgentRegistration.create(
            "remote-a",
            registration.card_url,
            "c" * 64,
            registration.interface_url,
            registration.skill_by_task_type,
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            repository.save_remote_agent(changed)
        duplicate = DelegationRecord.create(
            "goal", "task", 1, registration, "message-2", "d" * 64
        )
        with self.assertRaisesRegex(ValueError, "attempt"):
            repository.save_delegation(duplicate)
        repository.close()

    def test_old_agent_and_evaluation_payloads_receive_safe_defaults(self):
        repository = SQLiteRepository(":memory:")
        old_agent = {
            "agent_id": "old",
            "role": "worker",
            "model_id": "model",
            "task_types": ["analysis"],
            "enabled": True,
        }
        old_outcome = {
            "outcome_id": "outcome-old",
            "run_id": "run-old",
            "case_id": "case-old",
            "candidate_agent_id": "old",
            "candidate_model_id": "model",
            "passed": True,
            "score": 90.0,
            "duration_ms": 10.0,
            "critical": False,
            "error": "",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        repository.connection.execute(
            "INSERT INTO agents(agent_id, payload) VALUES (?, ?)",
            ("old", json.dumps(old_agent)),
        )
        repository.connection.execute(
            "INSERT INTO evaluation_outcomes("
            "outcome_id, run_id, case_id, candidate_agent_id, payload"
            ") VALUES (?, ?, ?, ?, ?)",
            ("outcome-old", "run-old", "case-old", "old", json.dumps(old_outcome)),
        )
        repository.connection.commit()

        self.assertEqual(repository.get_agent("old").execution_kind, "local")
        self.assertEqual(
            repository.list_evaluation_outcomes("run-old")[0].evidence, {}
        )
        self.assertEqual(CandidateExecution("x", 1).evidence, {})
        repository.close()


def _sha256(value):
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
