import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_society_loop.cli import main


class CLITests(unittest.TestCase):
    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_demo_status_events_and_agents_have_stable_json_output(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "demo.db")
            code, output, error = self.run_cli(
                ["demo", "--db", database, "--goal-id", "cli-demo", "--json"]
            )
            report = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(report["status"], "succeeded")
            self.assertGreaterEqual(report["retries"], 1)

            code, output, _ = self.run_cli(
                ["status", "cli-demo", "--db", database, "--json"]
            )
            status = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(len(status["tasks"]), 4)
            self.assertEqual(len(status["attempts"]), 5)
            self.assertEqual(status["goal"]["status"], "succeeded")

            code, output, _ = self.run_cli(
                ["events", "cli-demo", "--db", database, "--json"]
            )
            events = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(events[0]["event_type"], "goal.created")
            self.assertEqual(events[-1]["event_type"], "goal.succeeded")
            self.assertEqual(
                [event["sequence"] for event in events],
                sorted(event["sequence"] for event in events),
            )

            code, output, _ = self.run_cli(["agents", "--db", database, "--json"])
            agents = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(len(agents), 4)
            self.assertTrue(any(agent["performance"] for agent in agents))

    def test_knowledge_add_and_search_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "knowledge.db")
            code, output, _ = self.run_cli(
                [
                    "knowledge", "add", "Safety rules", "Never bypass review",
                    "--tag", "safety", "--db", database, "--json",
                ]
            )
            knowledge_id = json.loads(output)["knowledge_id"]
            self.assertEqual(code, 0)

            code, output, _ = self.run_cli(
                [
                    "knowledge", "search", "review safety", "--tag", "safety",
                    "--db", database, "--json",
                ]
            )
            results = json.loads(output)
            self.assertEqual(code, 0)
            self.assertEqual(results[0]["knowledge_id"], knowledge_id)

    def test_run_executes_json_goal_specification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "goal.json"
            spec.write_text(
                json.dumps(
                    {
                        "goal_id": "spec-goal",
                        "title": "Write evidence",
                        "description": "Produce a reviewed artifact",
                        "tasks": [
                            {
                                "task_id": "draft",
                                "task_type": "writing",
                                "description": "Write the draft",
                                "acceptance_criteria": {"required_terms": ["evidence"]},
                                "output": "A vague draft",
                                "repair_output": "A clear claim with evidence",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            code, output, error = self.run_cli(
                ["run", str(spec), "--db", str(root / "run.db"), "--json"]
            )
            report = json.loads(output)

            self.assertEqual((code, error), (0, ""))
            self.assertEqual(report["status"], "succeeded")
            self.assertEqual(report["retries"], 1)

    def test_invalid_spec_and_missing_goal_return_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid.json"
            invalid.write_text("{}", encoding="utf-8")

            code, _, error = self.run_cli(
                ["run", str(invalid), "--db", str(root / "db.sqlite")]
            )
            self.assertEqual(code, 2)
            self.assertIn("invalid goal spec", error)

            code, _, error = self.run_cli(
                ["status", "missing", "--db", str(root / "db.sqlite")]
            )
            self.assertEqual(code, 2)
            self.assertIn("goal not found", error)


if __name__ == "__main__":
    unittest.main()
