import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_society_loop.cli import build_parser, main
from agent_society_loop.domain import (
    ApprovalRequest,
    ApprovalStatus,
    Goal,
    GoalStatus,
)
from agent_society_loop.github import GitHubIssue
from agent_society_loop.storage import SQLiteRepository


class ScriptedProvider:
    model = "scripted-model"

    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, messages, *, temperature=0.0):
        return self.responses.pop(0)


class FakeIssueClient:
    def __init__(self, *args, **kwargs):
        pass

    def get_issue(self, repository, number):
        return GitHubIssue(
            repository,
            number,
            "Fix parser",
            "Handle empty input",
            ("bug",),
            f"https://github.com/{repository}/issues/{number}",
            "open",
        )


class CLITests(unittest.TestCase):
    def test_maintain_apply_parser_requires_named_check_declarations(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "maintain",
                "owner/repo",
                "12",
                "--workspace",
                ".",
                "--apply",
                "--check",
                "tests=python -m unittest",
            ]
        )

        self.assertTrue(args.apply)
        self.assertEqual(args.checks, ["tests=python -m unittest"])

    def test_maintain_publish_parser_captures_branch_policy(self):
        args = build_parser().parse_args(
            [
                "maintain", "owner/repo", "12", "--workspace", ".", "--apply",
                "--check", "tests=python -m unittest", "--publish",
                "--base", "main", "--remote", "upstream",
                "--branch-prefix", "agent-society/",
            ]
        )

        self.assertTrue(args.publish)
        self.assertEqual(args.remote, "upstream")
        self.assertEqual(args.branch_prefix, "agent-society/")
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

    def test_maintain_runs_issue_workflow_and_exposes_traces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "parser.py").write_text("def parse(value): return value\n", encoding="utf-8")
            provider = ScriptedProvider(
                [
                    '{"tasks":[{"task_id":"proposal","task_type":"maintenance",'
                    '"description":"Propose a fix","acceptance_criteria":{}}]}',
                    '{"type":"final","content":"Issue interpretation: empty input\\n'
                    'Relevant files: parser.py\\nProposed changes: guard empty input\\n'
                    'Tests: empty input\\nRisks: none"}',
                    '{"verdict":"PASS","score":90,"defects":[],"summary":"Accepted"}',
                ]
            )
            database = str(root / "maintain.db")
            with patch("agent_society_loop.cli.GitHubIssueClient", FakeIssueClient), patch(
                "agent_society_loop.cli._provider_from_environment", return_value=provider
            ):
                code, output, error = self.run_cli(
                    [
                        "maintain",
                        "owner/repo",
                        "12",
                        "--workspace",
                        str(root),
                        "--db",
                        database,
                        "--goal-id",
                        "maintain-12",
                        "--json",
                    ]
                )

            report = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(report["status"], "succeeded")

            code, output, _ = self.run_cli(
                ["traces", "maintain-12", "--db", database, "--json"]
            )
            traces = json.loads(output)
            self.assertEqual(code, 0)
            self.assertTrue(traces)
            self.assertTrue(all(trace["trace_id"] == "maintain-12" for trace in traces))

            code, output, _ = self.run_cli(
                ["approvals", "maintain-12", "--db", database, "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output), [])

    def test_approve_and_reject_commands_resolve_paused_goals(self):
        for command, expected in (("approve", ApprovalStatus.APPROVED), ("reject", ApprovalStatus.REJECTED)):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                database = str(Path(directory) / "approval.db")
                repository = SQLiteRepository(database)
                goal = replace(
                    Goal.create("Approval", "Resolve it", goal_id="approval-goal"),
                    status=GoalStatus.PAUSED,
                )
                approval = ApprovalRequest.create(
                    goal.goal_id,
                    "task",
                    "write_file",
                    {"path": "candidate.txt"},
                    "write tool requires approval",
                )
                repository.save_goal(goal)
                repository.save_approval(approval)
                repository.close()

                code, output, error = self.run_cli(
                    [
                        command,
                        approval.approval_id,
                        "--by",
                        "operator",
                        "--db",
                        database,
                        "--json",
                    ]
                )

                self.assertEqual((code, error), (0, ""))
                self.assertEqual(json.loads(output)["status"], expected.value)
                reopened = SQLiteRepository(database)
                self.assertEqual(
                    reopened.get_approval(approval.approval_id).status, expected
                )
                if command == "reject":
                    self.assertEqual(
                        reopened.get_goal(goal.goal_id).status, GoalStatus.FAILED
                    )
                reopened.close()


if __name__ == "__main__":
    unittest.main()
