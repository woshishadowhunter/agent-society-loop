import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_society_loop.cli import build_parser, main
from agent_society_loop.domain import AgentProfile, EvaluationRun
from agent_society_loop.storage import SQLiteRepository
from tests.a2a_fake_server import FakeA2AServer


SOURCE_REVISION = "5996b79f9cefa6fc390980e383e358a66fb9e49e"


class A2AGovernanceCLITests(unittest.TestCase):
    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parser_exposes_governance_diagnostics(self):
        parser = build_parser()
        policy = parser.parse_args(
            ["a2a", "policy", "import", "policy.json"]
        )
        attestation = parser.parse_args(
            [
                "a2a",
                "attestation",
                "import",
                "remote-a",
                "report.json",
                "--source-revision",
                SOURCE_REVISION,
                "--tool-version",
                "1.0.0",
            ]
        )
        doctor = parser.parse_args(["a2a", "doctor", "remote-a", "analysis"])
        self_test = parser.parse_args(["a2a", "self-test"])

        self.assertEqual((policy.a2a_command, policy.policy_command), ("policy", "import"))
        self.assertEqual(
            (attestation.a2a_command, attestation.attestation_command),
            ("attestation", "import"),
        )
        self.assertEqual(doctor.a2a_command, "doctor")
        self.assertEqual(self_test.a2a_command, "self-test")

    def test_policy_attestation_doctor_and_self_test_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory, FakeA2AServer() as server:
            root = Path(directory)
            database = str(root / "governance.db")
            digest = self.register(server, database)
            self.deploy(database)
            policy_path = self.write_policy(root, digest)
            report_path = self.write_report(root, server)
            common = ["--db", database, "--json"]

            code, output, error = self.run_cli(
                ["a2a", "policy", "validate", str(policy_path), *common]
            )
            self.assertEqual((code, error), (0, ""))
            validated = json.loads(output)

            code, output, error = self.run_cli(
                ["a2a", "policy", "import", str(policy_path), *common]
            )
            policy = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(policy["policy_digest"], validated["policy_digest"])

            code, output, error = self.run_cli(
                [
                    "a2a",
                    "policy",
                    "activate",
                    "analysis",
                    policy["policy_digest"],
                    "--by",
                    "operator",
                    *common,
                ]
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["task_type"], "analysis")

            code, output, error = self.run_cli(
                [
                    "a2a",
                    "attestation",
                    "import",
                    "remote-a",
                    str(report_path),
                    "--source-revision",
                    SOURCE_REVISION,
                    "--tool-version",
                    "1.0.0",
                    *common,
                ]
            )
            attestation = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertTrue(attestation["passed"])

            code, output, error = self.run_cli(
                ["a2a", "policy", "simulate", "remote-a", "analysis", *common]
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["verdict"], "ALLOW")

            decisions_before = self.decision_count(database)
            requests_before = len(server.requests)
            code, output, error = self.run_cli(
                [
                    "a2a",
                    "doctor",
                    "remote-a",
                    "analysis",
                    "--allow-insecure-localhost",
                    *common,
                ]
            )
            doctor = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertTrue(doctor["ready"])
            self.assertEqual(self.decision_count(database), decisions_before)
            self.assertEqual(server.send_count, 0)
            self.assertEqual(len(server.requests), requests_before + 1)

            code, output, error = self.run_cli(
                ["a2a", "self-test", *common]
            )
            self.assertEqual((code, error), (0, ""))
            self.assertTrue(json.loads(output)["passed"])

            code, output, error = self.run_cli(
                ["a2a", "policy", "list", *common]
            )
            listed = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(len(listed["policies"]), 1)
            self.assertEqual(len(listed["activations"]), 1)

            code, output, error = self.run_cli(
                ["a2a", "attestation", "list", "remote-a", *common]
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)[0]["attestation_id"], attestation["attestation_id"])

    def test_production_run_requires_policy_and_fresh_attestation(self):
        with tempfile.TemporaryDirectory() as directory, FakeA2AServer() as server:
            root = Path(directory)
            database = str(root / "production.db")
            digest = self.register(server, database)
            self.deploy(database)
            policy_path = self.write_policy(root, digest)
            report_path = self.write_report(root, server)
            common = ["--db", database, "--json"]
            _, policy_output, _ = self.run_cli(
                ["a2a", "policy", "import", str(policy_path), *common]
            )
            policy_digest = json.loads(policy_output)["policy_digest"]

            missing_policy = self.write_goal(root, "missing-policy")
            code, output, _ = self.run_cli(
                [
                    "run",
                    str(missing_policy),
                    "--allow-remote",
                    "--remote-poll-interval",
                    "0",
                    *common,
                ]
            )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output)["status"], "blocked")
            self.assertEqual(server.send_count, 0)

            self.run_cli(
                [
                    "a2a",
                    "policy",
                    "activate",
                    "analysis",
                    policy_digest,
                    "--by",
                    "operator",
                    *common,
                ]
            )
            missing_attestation = self.write_goal(root, "missing-attestation")
            code, output, _ = self.run_cli(
                [
                    "run",
                    str(missing_attestation),
                    "--allow-remote",
                    "--remote-poll-interval",
                    "0",
                    *common,
                ]
            )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output)["status"], "blocked")
            self.assertEqual(server.send_count, 0)

            self.run_cli(
                [
                    "a2a",
                    "attestation",
                    "import",
                    "remote-a",
                    str(report_path),
                    "--source-revision",
                    SOURCE_REVISION,
                    "--tool-version",
                    "1.0.0",
                    *common,
                ]
            )
            ready = self.write_goal(root, "ready")
            code, output, error = self.run_cli(
                [
                    "run",
                    str(ready),
                    "--allow-remote",
                    "--remote-poll-interval",
                    "0",
                    *common,
                ]
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["status"], "succeeded")
            self.assertEqual(server.send_count, 1)
            repository = SQLiteRepository(database)
            delegation = repository.list_delegations("ready")[0]
            self.assertTrue(delegation.policy_decision_id)
            repository.close()

    def test_invalid_and_failed_evidence_have_distinct_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory, FakeA2AServer() as server:
            root = Path(directory)
            database = str(root / "invalid.db")
            self.register(server, database)
            malformed = root / "malformed.json"
            malformed.write_text('{"default":"allow"}', encoding="utf-8")
            code, _, error = self.run_cli(
                ["a2a", "policy", "validate", str(malformed), "--db", database]
            )
            self.assertEqual(code, 2)
            self.assertIn("missing", error)

            failed_report = json.loads(self.write_report(root, server).read_text())
            failed_report["summary"]["must_compatibility"] = "0.0%"
            failed_report["per_requirement"]["A2A-HTTP-001"]["status"] = "FAIL"
            failed_report["per_transport"]["http_json"].update(
                {"passed": 0, "failed": 1}
            )
            path = root / "failed-report.json"
            path.write_text(json.dumps(failed_report), encoding="utf-8")
            code, output, error = self.run_cli(
                [
                    "a2a",
                    "attestation",
                    "import",
                    "remote-a",
                    str(path),
                    "--source-revision",
                    SOURCE_REVISION,
                    "--tool-version",
                    "1.0.0",
                    "--db",
                    database,
                    "--json",
                ]
            )
            self.assertEqual((code, error), (1, ""))
            self.assertFalse(json.loads(output)["passed"])

    def register(self, server, database):
        digest = hashlib.sha256(server.card_bytes).hexdigest()
        code, _, error = self.run_cli(
            [
                "a2a",
                "register",
                "remote-a",
                server.card_url,
                "--sha256",
                digest,
                "--interface",
                server.interface_url,
                "--skill",
                "analysis=analyze",
                "--allow-insecure-localhost",
                "--db",
                database,
            ]
        )
        self.assertEqual((code, error), (0, ""))
        return digest

    def deploy(self, database):
        repository = SQLiteRepository(database)
        registration = repository.get_remote_agent("remote-a")
        repository.save_agent(
            AgentProfile("local-a", "worker", "local-model", ("analysis",))
        )
        run = EvaluationRun(
            run_id="remote-promotion",
            task_type="analysis",
            benchmark_digest="digest",
            champion_agent_id="local-a",
            champion_model_id="local-model",
            challenger_agent_id="remote-a",
            challenger_model_id=registration.model_id,
            case_count=5,
            metrics={},
            recommended=True,
            failed_gates=(),
        )
        repository.save_evaluation_run(run)
        repository.promote_evaluation(run.run_id, "operator")
        repository.close()

    def write_policy(self, root, digest):
        path = root / "policy.json"
        path.write_text(
            json.dumps(
                {
                    "policy_id": "production",
                    "version": 1,
                    "default": "deny",
                    "rules": [
                        {
                            "rule_id": "analysis-v1",
                            "task_types": ["analysis"],
                            "agent_ids": ["remote-a"],
                            "card_sha256s": [digest],
                            "allowed_context_sections": ["review_feedback"],
                            "max_request_bytes": 65536,
                            "max_result_bytes": 131072,
                            "max_polls": 12,
                            "total_timeout_seconds": 45,
                            "required_attestation_kinds": ["a2a-tck"],
                            "max_attestation_age_hours": 168,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def write_report(self, root, server):
        path = root / "compatibility.json"
        path.write_text(
            json.dumps(
                {
                    "summary": {
                        "timestamp": "2026-07-16T10:00:00+00:00",
                        "sut_url": server.base_url,
                        "spec_version": "1.0",
                        "overall_compatibility": "100.0%",
                        "must_compatibility": "100.0%",
                        "should_compatibility": "100.0%",
                        "may_compatibility": "100.0%",
                    },
                    "per_requirement": {
                        "A2A-HTTP-001": {
                            "level": "MUST",
                            "status": "PASS",
                            "transports": {"http_json": "PASS"},
                            "errors": [],
                            "test_ids": ["tests/test_http.py::test_send"],
                        }
                    },
                    "per_transport": {
                        "http_json": {
                            "total": 1,
                            "passed": 1,
                            "failed": 0,
                            "skipped": 0,
                        }
                    },
                    "agent_card": server.card,
                }
            ),
            encoding="utf-8",
        )
        return path

    def write_goal(self, root, goal_id):
        path = root / f"{goal_id}.json"
        path.write_text(
            json.dumps(
                {
                    "goal_id": goal_id,
                    "title": "Delegate analysis",
                    "description": "Use the governed specialist",
                    "tasks": [
                        {
                            "task_id": "task",
                            "task_type": "analysis",
                            "description": "Analyze evidence",
                            "acceptance_criteria": {"required_terms": ["answer"]},
                            "output": "local fallback",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def decision_count(self, database):
        repository = SQLiteRepository(database)
        count = len(repository.list_policy_decisions())
        repository.close()
        return count


if __name__ == "__main__":
    unittest.main()
