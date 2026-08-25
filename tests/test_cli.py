import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from seed_society.cli import build_parser, main
from seed_society.domain import (
    ApprovalRequest,
    ApprovalStatus,
    Goal,
    GoalStatus,
)
from seed_society.github import GitHubIssue
from seed_society.storage import SQLiteRepository


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
                "--branch-prefix", "seed-society/",
            ]
        )

        self.assertTrue(args.publish)
        self.assertEqual(args.remote, "upstream")
        self.assertEqual(args.branch_prefix, "seed-society/")
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

    def test_genome_set_and_show_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = str(root / "genome.db")
            genome_path = root / "genome.json"
            genome_path.write_text(
                json.dumps(
                    {
                        "base_model": "local-qwen",
                        "role_seed": "Evidence-first analyst",
                        "self_model": {
                            "mission": "Produce verified analysis",
                            "success_signals": ["passes review"],
                            "failure_modes": ["unsupported claim"],
                        },
                        "traits": ["careful", "evidence"],
                        "tool_profile": ["docs"],
                        "risk_policy": "read_only",
                    }
                ),
                encoding="utf-8-sig",
            )

            code, output, error = self.run_cli(
                [
                    "genome", "set", "analyst", str(genome_path),
                    "--db", database, "--json",
                ]
            )
            saved = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(saved["agent_id"], "analyst")

            code, output, error = self.run_cli(
                ["genome", "show", "analyst", "--db", database, "--json"]
            )
            shown = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(shown["self_model"]["mission"], "Produce verified analysis")

    def test_genome_recombine_saves_child_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = str(root / "genome.db")
            for agent_id, role_seed, risk_policy, tools in (
                ("analyst-a", "Evidence analyst", "read_only", ["search", "sqlite"]),
                ("analyst-b", "Practical analyst", "approval_required", ["sqlite"]),
            ):
                genome_path = root / f"{agent_id}.json"
                genome_path.write_text(
                    json.dumps(
                        {
                            "base_model": "local-qwen",
                            "role_seed": role_seed,
                            "self_model": {
                                "mission": f"Serve as {role_seed}",
                                "success_signals": ["passes review"],
                                "failure_modes": ["unsupported claim"],
                            },
                            "traits": ["structured"],
                            "tool_profile": tools,
                            "risk_policy": risk_policy,
                        }
                    ),
                    encoding="utf-8",
                )
                code, _, error = self.run_cli(
                    [
                        "genome", "set", agent_id, str(genome_path),
                        "--db", database, "--json",
                    ]
                )
                self.assertEqual((code, error), (0, ""))

            code, output, error = self.run_cli(
                [
                    "genome", "recombine", "analyst-child",
                    "--parents", "analyst-a", "analyst-b",
                    "--task-type", "market_analysis",
                    "--db", database, "--json",
                ]
            )
            report = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(report["child"]["agent_id"], "analyst-child")
            self.assertEqual(report["child"]["parents"], ["analyst-a", "analyst-b"])
            self.assertEqual(report["child"]["risk_policy"], "approval_required")
            self.assertEqual(report["child"]["tool_profile"], ["sqlite"])

            code, output, error = self.run_cli(
                ["genome", "show", "analyst-child", "--db", database, "--json"]
            )
            shown = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(shown["agent_id"], "analyst-child")

    def test_experience_distill_and_list_after_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "experience.db")
            code, _, error = self.run_cli(
                ["demo", "--db", database, "--goal-id", "evolve-demo", "--json"]
            )
            self.assertEqual((code, error), (0, ""))

            code, output, error = self.run_cli(
                ["experience", "distill", "evolve-demo", "--db", database, "--json"]
            )
            records = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertTrue(records)
            self.assertTrue(any(record["lessons"] for record in records))

            code, output, error = self.run_cli(
                [
                    "experience", "list", "--task-type", "market_analysis",
                    "--db", database, "--json",
                ]
            )
            filtered = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertTrue(filtered)
            self.assertTrue(all(item["task_type"] == "market_analysis" for item in filtered))

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

    def test_enqueue_plans_json_goal_without_executing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "queued.json"
            database = root / "queued.db"
            spec.write_text(
                json.dumps(
                    {
                        "goal_id": "queued-goal",
                        "title": "Queued work",
                        "description": "Plan now and execute in workers",
                        "tasks": [
                            {
                                "task_id": "draft",
                                "task_type": "writing",
                                "description": "Write the draft",
                                "output": "Evidence-backed draft",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            code, output, error = self.run_cli(
                ["enqueue", str(spec), "--db", str(database), "--json"]
            )
            report = json.loads(output)

            self.assertEqual((code, error), (0, ""))
            self.assertEqual(report["status"], "running")
            self.assertEqual(report["tasks_total"], 1)
            self.assertEqual(report["attempts"], 0)
            repository = SQLiteRepository(database)
            try:
                self.assertEqual(repository.list_tasks("queued-goal")[0].status.value, "pending")
                self.assertEqual(repository.list_attempts("queued-goal"), [])
            finally:
                repository.close()

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
            with patch("seed_society.cli.GitHubIssueClient", FakeIssueClient), patch(
                "seed_society.cli._provider_from_environment", return_value=provider
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

    def test_evaluate_inspect_promote_and_deployments_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = str(root / "evaluation.db")
            spec_path = root / "benchmark.json"
            cases = []
            for index in range(1, 6):
                cases.append(
                    {
                        "case_id": f"case-{index}",
                        "input": {"prompt": f"question {index}"},
                        "acceptance_criteria": {"minimum_score": 70},
                        "critical": index == 1,
                        "results": {
                            "champion": {"passed": True, "score": 80, "duration_ms": 100},
                            "challenger": {"passed": True, "score": 84, "duration_ms": 110},
                        },
                    }
                )
            spec_path.write_text(
                json.dumps(
                    {
                        "task_type": "analysis",
                        "champion": {"agent_id": "champion", "model_id": "model-a"},
                        "challenger": {"agent_id": "challenger", "model_id": "model-b"},
                        "cases": cases,
                    }
                ),
                encoding="utf-8",
            )

            code, output, error = self.run_cli(
                ["evaluate", str(spec_path), "--db", database, "--json"]
            )
            run = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertTrue(run["recommended"])

            repository = SQLiteRepository(database)
            self.assertIsNone(repository.get_deployment("analysis"))
            repository.close()

            code, output, error = self.run_cli(
                ["evaluations", run["run_id"], "--db", database, "--json"]
            )
            inspected = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(len(inspected["outcomes"]), 10)

            code, output, error = self.run_cli(
                [
                    "promote",
                    run["run_id"],
                    "--by",
                    "operator",
                    "--db",
                    database,
                    "--json",
                ]
            )
            deployment = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(deployment["champion_agent_id"], "challenger")

            code, output, error = self.run_cli(
                ["deployments", "--db", database, "--json"]
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output), [deployment])

    def test_empty_evaluation_benchmark_returns_controlled_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "empty.json"
            spec.write_text(
                json.dumps(
                    {
                        "task_type": "analysis",
                        "champion": {"agent_id": "a", "model_id": "model-a"},
                        "challenger": {"agent_id": "b", "model_id": "model-b"},
                        "cases": [],
                    }
                ),
                encoding="utf-8",
            )

            code, output, error = self.run_cli(
                ["evaluate", str(spec), "--db", str(root / "db.sqlite"), "--json"]
            )

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("at least one case", error)


if __name__ == "__main__":
    unittest.main()
