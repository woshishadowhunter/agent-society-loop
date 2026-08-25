import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from seed_society.cli import build_parser, main
from seed_society.domain import (
    AgentProfile,
    ConformanceAttestation,
    DelegationPolicy,
    DelegationRecord,
    DelegationRule,
    DelegationStatus,
    EvaluationRun,
    PolicyActivation,
)
from seed_society.storage import SQLiteRepository
from tests.a2a_fake_server import FakeA2AServer


class A2ACLITests(unittest.TestCase):
    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parser_exposes_remote_opt_in_and_a2a_commands(self):
        run = build_parser().parse_args(
            [
                "run",
                "goal.json",
                "--allow-remote",
                "--remote-timeout",
                "30",
                "--remote-max-polls",
                "4",
                "--remote-poll-interval",
                "0.1",
            ]
        )
        inspect = build_parser().parse_args(
            ["a2a", "inspect-card", "https://agent.example/card"]
        )

        self.assertTrue(run.allow_remote)
        self.assertEqual((run.remote_timeout, run.remote_max_polls), (30, 4))
        self.assertEqual(inspect.a2a_command, "inspect-card")

    def test_inspect_register_list_and_invalid_mapping(self):
        with tempfile.TemporaryDirectory() as directory, FakeA2AServer() as server:
            database = str(Path(directory) / "a2a.db")
            common = ["--db", database, "--json"]
            code, output, error = self.run_cli(
                [
                    "a2a",
                    "inspect-card",
                    server.card_url,
                    "--allow-insecure-localhost",
                    *common,
                ]
            )
            inspection = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(
                inspection["sha256"], hashlib.sha256(server.card_bytes).hexdigest()
            )

            code, output, error = self.run_cli(
                [
                    "a2a",
                    "register",
                    "remote-a",
                    server.card_url,
                    "--sha256",
                    inspection["sha256"],
                    "--interface",
                    server.interface_url,
                    "--skill",
                    "analysis=analyze",
                    "--allow-insecure-localhost",
                    *common,
                ]
            )
            registration = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(registration["agent_id"], "remote-a")

            code, output, error = self.run_cli(
                ["a2a", "agents", *common]
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)[0]["card_sha256"], inspection["sha256"])

            code, _, error = self.run_cli(
                [
                    "a2a",
                    "register",
                    "bad",
                    server.card_url,
                    "--sha256",
                    inspection["sha256"],
                    "--interface",
                    server.interface_url,
                    "--skill",
                    "invalid",
                    "--allow-insecure-localhost",
                    *common,
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn("TASK_TYPE=SKILL_ID", error)

    def test_registration_rejects_missing_authentication_environment(self):
        with tempfile.TemporaryDirectory() as directory, FakeA2AServer() as server:
            code, output, error = self.run_cli(
                [
                    "a2a",
                    "register",
                    "remote-a",
                    server.card_url,
                    "--sha256",
                    hashlib.sha256(server.card_bytes).hexdigest(),
                    "--interface",
                    server.interface_url,
                    "--skill",
                    "analysis=analyze",
                    "--auth-env",
                    "DEFINITELY_MISSING_A2A_TOKEN",
                    "--allow-insecure-localhost",
                    "--db",
                    str(Path(directory) / "a2a.db"),
                ]
            )

        self.assertEqual((code, output), (2, ""))
        self.assertIn("environment variable is missing", error)
        self.assertEqual(server.requests, [])

    def test_promoted_remote_requires_opt_in_then_runs_and_is_inspectable(self):
        with tempfile.TemporaryDirectory() as directory, FakeA2AServer() as server:
            root = Path(directory)
            database = str(root / "run.db")
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
            rule = DelegationRule.create(
                "analysis-v1",
                ["analysis"],
                ["remote-a"],
                [digest],
                allowed_context_sections=["review_feedback"],
                max_request_bytes=65536,
                max_result_bytes=131072,
                max_polls=12,
                total_timeout_seconds=45,
                required_attestation_kinds=["a2a-tck"],
                max_attestation_age_hours=168,
            )
            policy = DelegationPolicy.create("production", 1, [rule])
            repository.save_policy(policy)
            repository.activate_policy(
                PolicyActivation.create("analysis", policy, "operator")
            )
            repository.save_attestation(
                ConformanceAttestation.create(
                    agent_id="remote-a",
                    card_sha256=digest,
                    kind="a2a-tck",
                    report_sha256="b" * 64,
                    source_revision="c" * 40,
                    tool_version="1.0.0",
                    spec_version="1.0",
                    observed_at="2026-07-16T10:00:00+00:00",
                    passed=True,
                    metrics={
                        "must_compatibility": 100.0,
                        "http_json_failed": 0,
                    },
                )
            )
            repository.close()

            def write_spec(goal_id):
                path = root / f"{goal_id}.json"
                path.write_text(
                    json.dumps(
                        {
                            "goal_id": goal_id,
                            "title": "Delegate analysis",
                            "description": "Use the approved remote specialist",
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
                return str(path)

            code, output, _ = self.run_cli(
                ["run", write_spec("remote-disabled"), "--db", database, "--json"]
            )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output)["status"], "blocked")
            self.assertEqual(server.send_count, 0)

            code, output, error = self.run_cli(
                [
                    "run",
                    write_spec("remote-enabled"),
                    "--allow-remote",
                    "--remote-poll-interval",
                    "0",
                    "--db",
                    database,
                    "--json",
                ]
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["status"], "succeeded")
            self.assertEqual(server.send_count, 1)

            code, output, error = self.run_cli(
                ["a2a", "delegations", "--db", database, "--json"]
            )
            delegations = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(delegations[0]["status"], "completed")

    def test_cancel_command_records_operator(self):
        with tempfile.TemporaryDirectory() as directory, FakeA2AServer() as server:
            database = str(Path(directory) / "cancel.db")
            repository = SQLiteRepository(database)
            registration = repository_registration(server)
            repository.save_remote_agent(registration)
            delegation = DelegationRecord.create(
                "goal", "task", 1, registration, "message", "b" * 64
            ).advance(DelegationStatus.SUBMITTING).advance(
                DelegationStatus.ACCEPTED,
                remote_task_id="task-1",
                remote_task_state="TASK_STATE_WORKING",
            )
            repository.save_delegation(delegation)
            repository.close()

            code, output, error = self.run_cli(
                [
                    "a2a",
                    "cancel",
                    delegation.delegation_id,
                    "--by",
                    "operator-a",
                    "--db",
                    database,
                    "--json",
                ]
            )

        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["canceled_by"], "operator-a")
        self.assertEqual(server.cancel_count, 1)


def repository_registration(server):
    from seed_society.domain import RemoteAgentRegistration

    return RemoteAgentRegistration.create(
        "remote-a",
        server.card_url,
        "a" * 64,
        server.interface_url,
        {"analysis": "analyze"},
        allow_insecure_localhost=True,
    )


if __name__ == "__main__":
    unittest.main()
