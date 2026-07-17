import io
import json
import tempfile
import threading
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from agent_society_loop.cli import _open_repository, build_parser, main
from agent_society_loop.storage import SQLiteRepository


class ModelRuntimeHandler(BaseHTTPRequestHandler):
    responses = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        json.loads(self.rfile.read(length))
        content = type(self).responses.pop(0)
        body = json.dumps(
            {"choices": [{"message": {"content": json.dumps(content)}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class WorkerCLITests(unittest.TestCase):
    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def write_spec(self, root):
        path = root / "goal.json"
        path.write_text(
            json.dumps(
                {
                    "goal_id": "worker-cli",
                    "title": "Worker CLI",
                    "description": "Execute a queued task",
                    "tasks": [
                        {
                            "task_id": "draft",
                            "task_type": "writing",
                            "description": "Write evidence",
                            "acceptance_criteria": {"required_terms": ["evidence"]},
                            "output": "A reviewed evidence artifact",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_parser_exposes_bounded_worker_process_options(self):
        args = build_parser().parse_args(
            [
                "worker", "run", "--worker-id", "process-a",
                "--agent-id", "spec-writing", "--once",
                "--heartbeat-ttl", "60", "--lease", "30",
                "--renew-interval", "10", "--poll-interval", "0.5",
            ]
        )

        self.assertEqual(args.worker_id, "process-a")
        self.assertEqual(args.agent_ids, ["spec-writing"])
        self.assertTrue(args.once)
        self.assertEqual(args.lease, 30)

    def test_parser_exposes_continuous_topic_bound_outbox_dispatch(self):
        args = build_parser().parse_args(
            [
                "outbox",
                "dispatch",
                "--worker-id",
                "webhook-a",
                "--topic",
                "notifications",
                "--webhook-url",
                "https://integrations.example/events",
                "--watch",
                "--poll-interval",
                "2.5",
            ]
        )

        self.assertEqual(args.topic, "notifications")
        self.assertTrue(args.watch)
        self.assertEqual(args.poll_interval, 2.5)

    def test_worker_parser_accepts_exactly_one_agent_source(self):
        args = build_parser().parse_args(
            [
                "worker", "run", "--worker-id", "process-a",
                "--model-config", "agents.json", "--once",
            ]
        )

        self.assertEqual(args.model_config, "agents.json")
        self.assertIsNone(args.agent_ids)
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "worker", "run", "--worker-id", "process-a",
                    "--agent-id", "spec-writing",
                    "--model-config", "agents.json", "--once",
                ]
            )

    def test_supported_commands_accept_postgres_connection_options(self):
        args = build_parser().parse_args(
            [
                "worker", "run", "--worker-id", "process-a",
                "--agent-id", "spec-writing", "--once",
                "--database-url", "postgresql://db/agents",
                "--postgres-schema", "worker_pool",
            ]
        )

        self.assertEqual(args.database_url, "postgresql://db/agents")
        self.assertEqual(args.postgres_schema, "worker_pool")

        for command in (
            ["traces", "goal-a"],
            ["approvals", "goal-a"],
            ["approve", "approval-a", "--by", "operator"],
            ["reject", "approval-a", "--by", "operator"],
        ):
            with self.subTest(command=command[0]):
                parsed = build_parser().parse_args(
                    [
                        *command,
                        "--database-url",
                        "postgresql://db/agents",
                        "--postgres-schema",
                        "worker_pool",
                    ]
                )
                self.assertEqual(parsed.database_url, "postgresql://db/agents")
                self.assertEqual(parsed.postgres_schema, "worker_pool")

    def test_operations_commands_open_postgres_repository(self):
        for command in ("health", "metrics", "outbox"):
            args = Namespace(
                command=command,
                database_url="postgresql://db/agents",
                postgres_schema="operations",
            )
            with self.subTest(command=command), patch(
                "agent_society_loop.cli.PostgreSQLRepository"
            ) as repository_type:
                repository = _open_repository(args)

                self.assertIs(repository, repository_type.return_value)
                repository_type.assert_called_once_with(
                    "postgresql://db/agents",
                    schema="operations",
                )

    def test_postgres_without_extra_is_a_controlled_cli_error(self):
        code, output, error = self.run_cli(
            [
                "status", "missing", "--database-url", "postgresql://unused",
                "--json",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("postgres extra", error)

    def test_health_and_metrics_commands_return_bounded_json(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "operations.db"

            health_code, health_output, health_error = self.run_cli(
                ["health", "--db", str(database), "--json"]
            )
            metrics_code, metrics_output, metrics_error = self.run_cli(
                ["metrics", "--db", str(database), "--json"]
            )

        health = json.loads(health_output)
        metrics = json.loads(metrics_output)
        self.assertEqual((health_code, health_error), (0, ""))
        self.assertTrue(health["ready"])
        self.assertEqual(health["status"], "ok")
        self.assertEqual((metrics_code, metrics_error), (0, ""))
        self.assertEqual(metrics["workers_total"], 0)
        self.assertEqual(metrics["outbox_pending"], 0)

    def test_health_command_can_require_a_live_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "operations.db"

            code, output, error = self.run_cli(
                [
                    "health",
                    "--worker-id",
                    "missing-worker",
                    "--db",
                    str(database),
                    "--json",
                ]
            )

        health = json.loads(output)
        self.assertEqual((code, error), (1, ""))
        self.assertFalse(health["ready"])
        self.assertEqual(health["worker"]["state"], "missing")

    def test_enqueue_worker_once_and_status_form_an_offline_process_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.write_spec(root)
            database = root / "worker.db"
            code, _, error = self.run_cli(
                ["enqueue", str(spec), "--db", str(database), "--json"]
            )
            self.assertEqual((code, error), (0, ""))

            code, output, error = self.run_cli(
                [
                    "worker", "run", "--worker-id", "process-a",
                    "--session-id", "session-a", "--agent-id", "spec-writing",
                    "--once", "--db", str(database), "--json",
                ]
            )
            result = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["task_status"], "succeeded")

            code, output, error = self.run_cli(
                ["status", "worker-cli", "--db", str(database), "--json"]
            )
            status = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(status["goal"]["status"], "succeeded")
            self.assertEqual(len(status["attempts"]), 1)

    def test_model_config_executes_queued_task_through_compatible_endpoint(self):
        server = HTTPServer(("127.0.0.1", 0), ModelRuntimeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        ModelRuntimeHandler.responses = [
            {"type": "final", "content": "A reviewed evidence artifact"},
            {
                "verdict": "PASS",
                "score": 95,
                "defects": [],
                "summary": "Meets the evidence criterion.",
            },
        ]
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spec = self.write_spec(root)
                database = root / "worker.db"
                config = root / "agents.json"
                config.write_text(
                    json.dumps(
                        {
                            "providers": {
                                "local": {
                                    "base_url": (
                                        f"http://127.0.0.1:{server.server_port}/v1"
                                    ),
                                    "model": "local-small",
                                }
                            },
                            "agents": [
                                {
                                    "agent_id": "local-writing",
                                    "provider": "local",
                                    "task_types": ["writing"],
                                }
                            ],
                            "reviewer": {"provider": "local"},
                        }
                    ),
                    encoding="utf-8",
                )
                self.run_cli(["enqueue", str(spec), "--db", str(database)])

                code, output, error = self.run_cli(
                    [
                        "worker", "run", "--worker-id", "model-process",
                        "--session-id", "model-session",
                        "--model-config", str(config), "--once",
                        "--db", str(database), "--json",
                    ]
                )

                result = json.loads(output)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(result["status"], "committed")
                self.assertEqual(result["task_status"], "succeeded")
                self.assertEqual(ModelRuntimeHandler.responses, [])
                repository = SQLiteRepository(database)
                try:
                    spans = repository.list_spans("worker-cli")
                    profiles = {
                        profile.agent_id: profile
                        for profile in repository.list_agents()
                    }
                finally:
                    repository.close()
                self.assertEqual(
                    [span.name for span in spans][-3:],
                    [
                        "worker.model",
                        "worker.execute",
                        "reviewer.review",
                    ],
                )
                self.assertEqual(spans[-1].agent_id, "model-reviewer")
                self.assertEqual(profiles["model-reviewer"].role, "reviewer")
                self.assertEqual(
                    profiles["model-reviewer"].model_id,
                    profiles["local-writing"].model_id,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_model_doctor_reports_exact_json_compatibility(self):
        server = HTTPServer(("127.0.0.1", 0), ModelRuntimeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        ModelRuntimeHandler.responses = [{"status": "ok"}]
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "agents.json"
                config.write_text(
                    json.dumps(
                        {
                            "providers": {
                                "local": {
                                    "base_url": (
                                        f"http://127.0.0.1:{server.server_port}/v1"
                                    ),
                                    "model": "local-small",
                                }
                            },
                            "agents": [
                                {
                                    "agent_id": "local-writing",
                                    "provider": "local",
                                    "task_types": ["writing"],
                                }
                            ],
                            "reviewer": {"provider": "local"},
                        }
                    ),
                    encoding="utf-8",
                )

                code, output, error = self.run_cli(
                    ["model", "doctor", str(config), "--json"]
                )

                report = json.loads(output)
                self.assertEqual((code, error), (0, ""))
                self.assertTrue(report["passed"])
                self.assertEqual(report["providers"][0]["model"], "local-small")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_worker_rejects_unknown_agent_and_unsafe_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = self.write_spec(root)
            database = root / "worker.db"
            self.run_cli(["enqueue", str(spec), "--db", str(database)])

            code, _, error = self.run_cli(
                [
                    "worker", "run", "--worker-id", "process-a",
                    "--agent-id", "missing", "--once", "--db", str(database),
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn("agent not found", error)

            code, _, error = self.run_cli(
                [
                    "worker", "run", "--worker-id", "process-a",
                    "--agent-id", "spec-writing", "--once", "--db", str(database),
                    "--heartbeat-ttl", "30", "--lease", "30",
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn("longer than", error)


if __name__ == "__main__":
    unittest.main()
