import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from agent_society_loop.a2a import A2ALimits, AgentCardInspection
from agent_society_loop.a2a_governance import build_doctor_report
from agent_society_loop.domain import (
    AgentProfile,
    ConformanceAttestation,
    DelegationPolicy,
    DelegationRule,
    EvaluationRun,
    PolicyActivation,
    RemoteAgentRegistration,
)
from agent_society_loop.storage import SQLiteRepository


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


class A2ADoctorTests(unittest.TestCase):
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
        profile = AgentProfile(
            "remote-a",
            "worker",
            self.registration.model_id,
            ("analysis",),
            execution_kind="a2a",
        )
        self.repository.save_remote_agent_profile(self.registration, profile)
        run = EvaluationRun(
            run_id="doctor-deployment",
            task_type="analysis",
            benchmark_digest="benchmark",
            champion_agent_id="local-a",
            champion_model_id="local-model",
            challenger_agent_id="remote-a",
            challenger_model_id=self.registration.model_id,
            case_count=5,
            metrics={"passed": True},
            recommended=True,
            failed_gates=(),
        )
        self.repository.save_evaluation_run(run)
        self.repository.promote_evaluation(run.run_id, "operator")
        self.policy = self.install_policy()
        self.attestation = self.install_attestation()
        card = {
            "name": "Analysis Agent",
            "supportedInterfaces": [
                {
                    "url": "https://agent.test/a2a",
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0",
                    "tenant": "tenant-a",
                }
            ],
            "skills": [{"id": "analyze"}],
        }
        raw = json.dumps(card, sort_keys=True, separators=(",", ":")).encode()
        self.inspection = AgentCardInspection(
            self.registration.card_url,
            self.registration.card_sha256,
            len(raw),
            card,
        )

    def tearDown(self):
        self.repository.close()

    def install_policy(self):
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
        policy = DelegationPolicy.create("production", 1, [rule])
        self.repository.save_policy(policy)
        self.repository.activate_policy(
            PolicyActivation.create("analysis", policy, "operator")
        )
        return policy

    def install_attestation(self, **replacements):
        values = {
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
        values.update(replacements)
        attestation = ConformanceAttestation.create(**values)
        self.repository.save_attestation(attestation)
        return attestation

    def report(self, inspection=None):
        before = len(self.repository.list_policy_decisions())
        report = build_doctor_report(
            self.repository,
            self.registration,
            inspection or self.inspection,
            "analysis",
            A2ALimits(),
            now=lambda: NOW,
        )
        self.assertEqual(len(self.repository.list_policy_decisions()), before)
        return report

    def test_ready_report_has_stable_checks_and_is_read_only(self):
        report = self.report()

        self.assertTrue(report.ready)
        self.assertEqual(
            tuple(check.check_id for check in report.checks),
            (
                "card_digest",
                "card_interface",
                "card_skills",
                "agent_profile",
                "active_deployment",
                "active_policy",
                "policy_simulation",
                "tck_attestation",
            ),
        )
        self.assertTrue(all(check.passed for check in report.checks))

    def test_card_digest_drift_changes_only_digest_check(self):
        report = self.report(replace(self.inspection, sha256="d" * 64))
        outcomes = {check.check_id: check.passed for check in report.checks}

        self.assertFalse(report.ready)
        self.assertFalse(outcomes.pop("card_digest"))
        self.assertTrue(all(outcomes.values()))

    def test_latest_failed_attestation_fails_evidence_and_simulation_safely(self):
        self.install_attestation(
            report_sha256="d" * 64,
            observed_at="2026-07-16T11:00:00+00:00",
            passed=False,
        )

        report = self.report()
        outcomes = {check.check_id: check.passed for check in report.checks}

        self.assertFalse(outcomes["policy_simulation"])
        self.assertFalse(outcomes["tck_attestation"])
        self.assertNotIn("https://", json.dumps(report.to_dict()))


if __name__ == "__main__":
    unittest.main()
