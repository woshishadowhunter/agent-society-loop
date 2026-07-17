import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_society_loop.cli import build_parser, main


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
