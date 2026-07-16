import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent_society_loop.domain import (
    ConformanceAttestation,
    DelegationPolicy,
    DelegationRecord,
    DelegationRule,
    PolicyActivation,
    PolicyDecision,
    PolicyVerdict,
    RemoteAgentRegistration,
)
from agent_society_loop.storage import SQLiteRepository


class GovernanceDomainTests(unittest.TestCase):
    def rule(self, **replacements):
        arguments = {
            "rule_id": "research-v1",
            "task_types": ["research", "analysis"],
            "agent_ids": ["remote-b", "remote-a"],
            "card_sha256s": ["b" * 64, "a" * 64],
            "allowed_context_sections": ["review_feedback"],
            "max_request_bytes": 65_536,
            "max_result_bytes": 131_072,
            "max_polls": 12,
            "total_timeout_seconds": 45,
            "required_attestation_kinds": ["a2a-tck"],
            "max_attestation_age_hours": 168,
        }
        arguments.update(replacements)
        return DelegationRule.create(**arguments)

    def registration(self):
        return RemoteAgentRegistration.create(
            "remote-a",
            "https://agent.test/.well-known/agent-card.json",
            "a" * 64,
            "https://agent.test/a2a",
            {"research": "research"},
            allowed_context_sections=("review_feedback",),
        )

    def test_policy_digest_is_canonical_and_input_order_independent(self):
        first = DelegationPolicy.create("production", 1, [self.rule()])
        reordered = DelegationPolicy.create(
            "production",
            1,
            [
                self.rule(
                    task_types=["analysis", "research"],
                    agent_ids=["remote-a", "remote-b"],
                    card_sha256s=["a" * 64, "b" * 64],
                )
            ],
        )

        self.assertEqual(first.policy_digest, reordered.policy_digest)
        self.assertEqual(len(first.policy_digest), 64)
        self.assertEqual(first.default, "deny")

    def test_policy_rejects_non_deny_default_and_overlapping_rules(self):
        with self.assertRaisesRegex(ValueError, "default"):
            DelegationPolicy.create("production", 1, [self.rule()], default="allow")

        overlapping = self.rule(rule_id="research-v2", max_polls=8)
        with self.assertRaisesRegex(ValueError, "overlap"):
            DelegationPolicy.create("production", 1, [self.rule(), overlapping])

    def test_rule_rejects_invalid_identity_context_and_limits(self):
        cases = [
            ({"card_sha256s": ["short"]}, "SHA-256"),
            ({"allowed_context_sections": ["private"]}, "context"),
            ({"max_request_bytes": 0}, "positive"),
            ({"required_attestation_kinds": ["custom"]}, "attestation"),
        ]
        for replacements, pattern in cases:
            with self.subTest(replacements=replacements), self.assertRaisesRegex(
                ValueError, pattern
            ):
                self.rule(**replacements)

    def test_attestation_validates_evidence_identity_and_metrics(self):
        attestation = _attestation()

        self.assertEqual(attestation.attestation_id, f"attestation-{'b' * 16}")
        self.assertTrue(attestation.passed)
        with self.assertRaisesRegex(ValueError, "timestamp"):
            _attestation(observed_at="2026-07-16")
        with self.assertRaisesRegex(ValueError, "percentage"):
            _attestation(metrics={"must_compatibility": 100.1})

    def test_decision_binds_exact_policy_rule_and_remote_identity(self):
        policy = DelegationPolicy.create("production", 1, [self.rule()])
        decision = PolicyDecision.create(
            "goal-1",
            "task-1",
            1,
            self.registration(),
            PolicyVerdict.ALLOW,
            policy_digest=policy.policy_digest,
            policy_rule_id="research-v1",
            reason_codes=("policy_allowed",),
            allowed_context_sections=("review_feedback",),
            max_request_bytes=32_768,
            max_result_bytes=65_536,
            max_polls=8,
            total_timeout_seconds=30,
            attestation_ids=(_attestation().attestation_id,),
        )

        delegation = DelegationRecord.create(
            "goal-1",
            "task-1",
            1,
            self.registration(),
            "message-1",
            "c" * 64,
            policy_decision=decision,
        )

        self.assertEqual(delegation.policy_decision_id, decision.decision_id)
        self.assertEqual(delegation.policy_digest, policy.policy_digest)
        self.assertEqual(delegation.policy_rule_id, "research-v1")


class GovernanceStorageTests(unittest.TestCase):
    def records(self):
        registration = GovernanceDomainTests().registration()
        rule = GovernanceDomainTests().rule()
        policy = DelegationPolicy.create("production", 1, [rule])
        activation = PolicyActivation.create("research", policy, "operator@example")
        attestation = _attestation()
        decision = PolicyDecision.create(
            "goal-1",
            "task-1",
            1,
            registration,
            PolicyVerdict.ALLOW,
            policy_digest=policy.policy_digest,
            policy_rule_id=rule.rule_id,
            reason_codes=("policy_allowed",),
            allowed_context_sections=("review_feedback",),
            max_request_bytes=65_536,
            max_result_bytes=131_072,
            max_polls=12,
            total_timeout_seconds=45,
            attestation_ids=(attestation.attestation_id,),
        )
        return policy, activation, attestation, decision

    def test_governance_records_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "governance.db"
            repository = SQLiteRepository(path)
            policy, activation, attestation, decision = self.records()
            repository.save_policy(policy)
            repository.activate_policy(activation)
            repository.save_attestation(attestation)
            repository.save_policy_decision(decision)
            repository.close()

            reopened = SQLiteRepository(path)
            self.assertEqual(reopened.get_policy(policy.policy_digest), policy)
            self.assertEqual(reopened.list_policies(), [policy])
            self.assertEqual(reopened.get_policy_activation("research"), activation)
            self.assertEqual(reopened.list_policy_activations(), [activation])
            self.assertEqual(reopened.list_attestations("remote-a"), [attestation])
            self.assertEqual(reopened.get_policy_decision(decision.decision_id), decision)
            self.assertEqual(
                reopened.get_attempt_policy_decision(
                    "goal-1", "task-1", 1, "remote-a"
                ),
                decision,
            )
            self.assertEqual(reopened.list_policy_decisions("goal-1"), [decision])
            reopened.close()

    def test_activation_can_move_but_evidence_and_decisions_are_immutable(self):
        repository = SQLiteRepository(":memory:")
        policy, activation, attestation, decision = self.records()
        repository.save_policy(policy)
        repository.activate_policy(activation)
        repository.save_attestation(attestation)
        repository.save_policy_decision(decision)

        next_policy = DelegationPolicy.create(
            "production", 2, [GovernanceDomainTests().rule(max_polls=6)]
        )
        repository.save_policy(next_policy)
        next_activation = PolicyActivation.create(
            "research", next_policy, "second-operator@example"
        )
        repository.activate_policy(next_activation)
        self.assertEqual(repository.get_policy_activation("research"), next_activation)

        with self.assertRaisesRegex(ValueError, "attestation"):
            repository.save_attestation(replace(attestation, card_sha256="d" * 64))
        with self.assertRaisesRegex(ValueError, "decision"):
            repository.save_policy_decision(
                replace(decision, reason_codes=("different_reason",))
            )
        duplicate_attempt = replace(
            decision,
            decision_id="decision-duplicate",
            reason_codes=("different_reason",),
        )
        with self.assertRaisesRegex(ValueError, "attempt"):
            repository.save_policy_decision(duplicate_attempt)
        repository.close()

    def test_content_addressed_imports_are_idempotent(self):
        repository = SQLiteRepository(":memory:")
        policy, _, attestation, _ = self.records()
        repository.save_policy(policy)
        repository.save_policy(replace(policy, created_at="2026-07-17T00:00:00+00:00"))
        repository.save_attestation(attestation)
        repository.save_attestation(
            replace(attestation, created_at="2026-07-17T00:00:00+00:00")
        )

        self.assertEqual(repository.get_policy(policy.policy_digest), policy)
        self.assertEqual(repository.get_attestation(attestation.attestation_id), attestation)
        repository.close()

    def test_old_delegation_payload_receives_empty_governance_defaults(self):
        repository = SQLiteRepository(":memory:")
        registration = GovernanceDomainTests().registration()
        payload = {
            "delegation_id": "delegation-old",
            "goal_id": "goal-old",
            "task_id": "task-old",
            "attempt_no": 1,
            "agent_id": registration.agent_id,
            "model_id": registration.model_id,
            "card_sha256": registration.card_sha256,
            "message_id": "message-old",
            "payload_sha256": "d" * 64,
            "status": "prepared",
            "remote_task_id": "",
            "remote_task_state": "",
            "poll_count": 0,
            "result_content": "",
            "result_sha256": "",
            "error_category": "",
            "canceled_by": "",
            "created_at": "2026-07-16T00:00:00+00:00",
            "updated_at": "2026-07-16T00:00:00+00:00",
        }
        repository.connection.execute(
            "INSERT INTO delegations("
            "delegation_id, goal_id, task_id, attempt_no, agent_id, payload"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "delegation-old",
                "goal-old",
                "task-old",
                1,
                registration.agent_id,
                json.dumps(payload),
            ),
        )
        repository.connection.commit()

        loaded = repository.get_delegation("delegation-old")
        self.assertEqual(loaded.policy_decision_id, "")
        self.assertEqual(loaded.policy_digest, "")
        self.assertEqual(loaded.policy_rule_id, "")
        repository.close()


def _attestation(**replacements):
    arguments = {
        "agent_id": "remote-a",
        "card_sha256": "a" * 64,
        "kind": "a2a-tck",
        "report_sha256": "b" * 64,
        "source_revision": "c" * 40,
        "tool_version": "1.0.0",
        "spec_version": "1.0",
        "observed_at": "2026-07-16T10:00:00+00:00",
        "passed": True,
        "metrics": {"must_compatibility": 100.0, "http_json_failed": 0},
    }
    arguments.update(replacements)
    return ConformanceAttestation.create(**arguments)


if __name__ == "__main__":
    unittest.main()
