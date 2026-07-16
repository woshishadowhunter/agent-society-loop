import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from agent_society_loop.a2a import A2ALimits
from agent_society_loop.a2a_governance import (
    DelegationPolicyEvaluator,
    parse_a2a_tck_report,
    parse_policy_document,
)
from agent_society_loop.domain import (
    ConformanceAttestation,
    PolicyActivation,
    PolicyVerdict,
    RemoteAgentRegistration,
    Task,
)
from agent_society_loop.storage import SQLiteRepository


FIXTURE = Path(__file__).parent / "fixtures" / "a2a-tck-passing.json"
SOURCE_REVISION = "5996b79f9cefa6fc390980e383e358a66fb9e49e"


def _registration(**replacements):
    arguments = {
        "agent_id": "remote-a",
        "card_url": "https://agent.test/.well-known/agent-card.json",
        "card_sha256": "a" * 64,
        "interface_url": "https://agent.test/a2a",
        "skill_by_task_type": {"research": "research"},
        "tenant": "tenant-a",
        "allowed_context_sections": ("review_feedback",),
    }
    arguments.update(replacements)
    return RemoteAgentRegistration.create(**arguments)


def _policy_bytes(**rule_replacements):
    rule = {
        "rule_id": "research-v1",
        "task_types": ["research"],
        "agent_ids": ["remote-a"],
        "card_sha256s": ["a" * 64],
        "allowed_context_sections": ["review_feedback"],
        "max_request_bytes": 65536,
        "max_result_bytes": 131072,
        "max_polls": 12,
        "total_timeout_seconds": 45,
        "required_attestation_kinds": ["a2a-tck"],
        "max_attestation_age_hours": 168,
    }
    rule.update(rule_replacements)
    return json.dumps(
        {
            "policy_id": "production-research",
            "version": 1,
            "default": "deny",
            "rules": [rule],
        }
    ).encode()


class PolicyParserTests(unittest.TestCase):
    def test_parses_strict_canonical_policy(self):
        policy = parse_policy_document(_policy_bytes())

        self.assertEqual(policy.policy_id, "production-research")
        self.assertEqual(policy.rules[0].agent_ids, ("remote-a",))
        self.assertEqual(len(policy.policy_digest), 64)

    def test_rejects_unknown_duplicate_and_wrong_typed_fields(self):
        unknown = json.loads(_policy_bytes())
        unknown["callback"] = "allow()"
        wrong_type = json.loads(_policy_bytes())
        wrong_type["rules"][0]["max_polls"] = "12"
        duplicate_array = json.loads(_policy_bytes())
        duplicate_array["rules"][0]["agent_ids"] = ["remote-a", "remote-a"]
        cases = [
            (json.dumps(unknown).encode(), "unknown"),
            (json.dumps(wrong_type).encode(), "integer"),
            (json.dumps(duplicate_array).encode(), "duplicate"),
            (b'{"policy_id":"a","policy_id":"b"}', "duplicate"),
            (b"\xff", "UTF-8"),
        ]
        for raw, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                ValueError, pattern
            ):
                parse_policy_document(raw)

    def test_rejects_default_allow_empty_rules_and_overlapping_domains(self):
        default_allow = json.loads(_policy_bytes())
        default_allow["default"] = "allow"
        empty = json.loads(_policy_bytes())
        empty["rules"] = []
        overlapping = json.loads(_policy_bytes())
        second = dict(overlapping["rules"][0])
        second["rule_id"] = "research-v2"
        overlapping["rules"].append(second)
        for payload, pattern in [
            (default_allow, "default"),
            (empty, "rules"),
            (overlapping, "overlap"),
        ]:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                ValueError, pattern
            ):
                parse_policy_document(json.dumps(payload).encode())


class TCKParserTests(unittest.TestCase):
    def report(self, mutation=None):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        if mutation:
            mutation(payload)
        return json.dumps(payload).encode()

    def parse(self, raw):
        return parse_a2a_tck_report(
            raw,
            _registration(),
            source_revision=SOURCE_REVISION,
            tool_version="1.0.0",
        )

    def test_imports_passing_official_report_as_content_addressed_evidence(self):
        attestation = self.parse(FIXTURE.read_bytes())

        self.assertTrue(attestation.passed)
        self.assertEqual(attestation.kind, "a2a-tck")
        self.assertEqual(attestation.source_revision, SOURCE_REVISION)
        self.assertEqual(attestation.metrics["must_compatibility"], 100.0)
        self.assertEqual(attestation.metrics["http_json_failed"], 0)

    def test_well_formed_test_failure_is_preserved_as_failed_evidence(self):
        def fail_must(payload):
            payload["summary"]["must_compatibility"] = "50.0%"
            payload["per_requirement"]["A2A-HTTP-001"]["status"] = "FAIL"
            payload["per_requirement"]["A2A-HTTP-001"]["transports"][
                "http_json"
            ] = "FAIL"
            payload["per_transport"]["http_json"].update(
                {"passed": 1, "failed": 1}
            )

        attestation = self.parse(self.report(fail_must))

        self.assertFalse(attestation.passed)
        self.assertEqual(attestation.metrics["http_json_failed"], 1)

    def test_rejects_schema_version_transport_and_card_identity_drift(self):
        cases = [
            (lambda value: value.update({"extra": {}}), "unknown"),
            (
                lambda value: value["summary"].update({"spec_version": "0.3"}),
                "spec version",
            ),
            (lambda value: value.update({"per_transport": {}}), "http_json"),
            (
                lambda value: value["agent_card"]["supportedInterfaces"][0].update(
                    {"url": "https://other.test/a2a"}
                ),
                "interface",
            ),
            (
                lambda value: value["agent_card"].update({"skills": []}),
                "skill",
            ),
            (
                lambda value: value["summary"].update(
                    {"must_compatibility": "NaN%"}
                ),
                "percentage",
            ),
            (
                lambda value: value["summary"].update({"timestamp": "yesterday"}),
                "timestamp",
            ),
        ]
        for mutation, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                ValueError, pattern
            ):
                self.parse(self.report(mutation))

    def test_rejects_invalid_source_revision_and_tool_version(self):
        for revision, version, pattern in [
            ("main", "1.0.0", "revision"),
            (SOURCE_REVISION, "bad version", "version"),
        ]:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                ValueError, pattern
            ):
                parse_a2a_tck_report(
                    FIXTURE.read_bytes(),
                    _registration(),
                    source_revision=revision,
                    tool_version=version,
                )


class PolicyEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.registration = _registration()
        self.policy = parse_policy_document(_policy_bytes())
        self.repository.save_policy(self.policy)
        self.repository.activate_policy(
            PolicyActivation.create("research", self.policy, "operator")
        )
        self.attestation = parse_a2a_tck_report(
            FIXTURE.read_bytes(),
            self.registration,
            source_revision=SOURCE_REVISION,
            tool_version="1.0.0",
        )
        self.repository.save_attestation(self.attestation)
        self.task = Task.create("goal-1", "task-1", "research", "Research safely")
        self.now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.repository.close()

    def evaluator(self):
        return DelegationPolicyEvaluator(self.repository, now=lambda: self.now)

    def test_allows_exact_fresh_evidence_and_tightens_runtime_limits(self):
        decision = self.evaluator().decide(
            self.task,
            self.registration,
            1,
            A2ALimits(
                max_request_bytes=32768,
                max_result_bytes=65536,
                max_polls=8,
                total_timeout=30,
            ),
        )

        self.assertEqual(decision.verdict, PolicyVerdict.ALLOW)
        self.assertEqual(decision.policy_digest, self.policy.policy_digest)
        self.assertEqual(decision.max_request_bytes, 32768)
        self.assertEqual(decision.max_result_bytes, 65536)
        self.assertEqual(decision.max_polls, 8)
        self.assertEqual(decision.total_timeout_seconds, 30)
        self.assertEqual(decision.attestation_ids, (self.attestation.attestation_id,))
        self.assertEqual(
            self.repository.get_attempt_policy_decision(
                "goal-1", "task-1", 1, "remote-a"
            ),
            decision,
        )

    def test_denies_without_activation_context_or_usable_attestation(self):
        self.repository.connection.execute("DELETE FROM policy_activations")
        self.repository.connection.commit()
        inactive = self.evaluator().decide(
            replace(self.task, task_id="task-2"),
            self.registration,
            1,
            A2ALimits(),
        )
        self.assertEqual(inactive.verdict, PolicyVerdict.DENY)
        self.assertIn("no_active_policy", inactive.reason_codes)

        self.repository.activate_policy(
            PolicyActivation.create("research", self.policy, "operator")
        )
        broad_registration = _registration(
            allowed_context_sections=("dependency_artifacts", "review_feedback")
        )
        context_denied = self.evaluator().decide(
            replace(self.task, task_id="task-3"),
            broad_registration,
            1,
            A2ALimits(),
        )
        self.assertEqual(context_denied.verdict, PolicyVerdict.DENY)
        self.assertIn("context_not_allowed", context_denied.reason_codes)

        self.repository.connection.execute("DELETE FROM conformance_attestations")
        self.repository.connection.commit()
        missing = self.evaluator().decide(
            replace(self.task, task_id="task-4"),
            self.registration,
            1,
            A2ALimits(),
        )
        self.assertIn("missing_attestation", missing.reason_codes)

    def test_denies_failed_or_stale_latest_attestation(self):
        failed = replace(
            self.attestation,
            attestation_id="attestation-failed",
            report_sha256="d" * 64,
            passed=False,
            observed_at="2026-07-16T11:00:00+00:00",
        )
        self.repository.save_attestation(failed)
        denied = self.evaluator().decide(
            replace(self.task, task_id="task-failed"),
            self.registration,
            1,
            A2ALimits(),
        )
        self.assertIn("failed_attestation", denied.reason_codes)

        stale_repository = SQLiteRepository(":memory:")
        stale_repository.save_policy(self.policy)
        stale_repository.activate_policy(
            PolicyActivation.create("research", self.policy, "operator")
        )
        stale_repository.save_attestation(
            replace(
                self.attestation,
                observed_at="2026-07-01T00:00:00+00:00",
            )
        )
        stale = DelegationPolicyEvaluator(
            stale_repository, now=lambda: self.now
        ).decide(self.task, self.registration, 1, A2ALimits())
        self.assertIn("stale_attestation", stale.reason_codes)
        stale_repository.close()

    def test_same_attempt_replays_decision_while_new_attempt_reevaluates(self):
        evaluator = self.evaluator()
        allowed = evaluator.decide(
            self.task, self.registration, 1, A2ALimits()
        )
        self.repository.connection.execute("DELETE FROM policy_activations")
        self.repository.connection.commit()

        replayed = evaluator.decide(
            self.task, self.registration, 1, A2ALimits(max_polls=1)
        )
        denied = evaluator.decide(
            self.task, self.registration, 2, A2ALimits()
        )

        self.assertEqual(replayed, allowed)
        self.assertEqual(denied.verdict, PolicyVerdict.DENY)
        self.assertIn("no_active_policy", denied.reason_codes)


if __name__ == "__main__":
    unittest.main()
