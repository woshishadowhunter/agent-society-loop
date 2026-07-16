import tempfile
import unittest
import hashlib
import json
import subprocess
from pathlib import Path

from agent_society_loop.domain import Artifact, GoalStatus, Task, ToolRisk, Verdict
from agent_society_loop.github import GitHubIssue
from agent_society_loop.maintenance import (
    build_maintenance_engine,
    create_maintenance_goal,
)
from agent_society_loop.storage import SQLiteRepository
from agent_society_loop.tools import ToolContext


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, *, temperature=0.0):
        self.calls.append(list(messages))
        return self.responses.pop(0)


class MaintenanceWorkflowTests(unittest.TestCase):
    def test_guarded_reviewer_requires_current_durable_check_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "app.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            provider = ScriptedProvider(
                ['{"verdict":"PASS","score":99,"defects":[],"summary":"Looks good"}']
            )
            repository = SQLiteRepository(workspace / "state.db")
            engine = build_maintenance_engine(
                repository,
                provider,
                workspace,
                apply=True,
                checks={"tests": ("python", "-c", "pass")},
                protected_paths=("state.db",),
            )
            task = Task.create("goal", "implement", "repository_maintenance", "Change app")
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            writer = engine.workers[
                "repository-maintainer"
            ].tool_executor.registry.get("workspace_write_file")
            writer.invoke_with_context(
                {
                    "path": "app.py",
                    "content": "VALUE = 2\n",
                    "expected_sha256": before,
                },
                ToolContext("goal", "implement", "agent"),
            )
            artifact = Artifact.create("goal", "implement", "agent", "Checks: passed")

            review = engine.reviewer.review(task, artifact.content, 1)

            self.assertEqual(review.verdict, Verdict.FAIL)
            self.assertIn("tests", review.summary)
            repository.close()

    def test_guarded_run_pauses_for_write_and_check_then_finishes_with_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "parser.py"
            source.write_text("def parse(value):\n    return value\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "add", "parser.py"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"],
                cwd=workspace,
                check=True,
            )
            original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            replacement = "def parse(value):\n    return value.strip()\n"
            write_call = json.dumps(
                {
                    "type": "tool_call",
                    "name": "workspace_write_file",
                    "arguments": {
                        "path": "parser.py",
                        "content": replacement,
                        "expected_sha256": original_hash,
                    },
                }
            )
            provider = ScriptedProvider(
                [
                    '{"tasks":[{"task_id":"implement","task_type":"repository_maintenance",'
                    '"description":"Implement and verify the parser fix",'
                    '"acceptance_criteria":{"required_sections":["Changed files","Checks","Risks"]}}]}',
                    '{"type":"tool_call","name":"workspace_read_file","arguments":{"path":"parser.py"}}',
                    write_call,
                    write_call,
                    '{"type":"tool_call","name":"workspace_run_check","arguments":{"name":"parser"}}',
                    '{"type":"tool_call","name":"workspace_run_check","arguments":{"name":"parser"}}',
                    '{"type":"tool_call","name":"workspace_diff","arguments":{}}',
                    json.dumps(
                        {
                            "type": "final",
                            "content": "Changed files: parser.py\nChecks: parser passed\nRisks: no remote action",
                        }
                    ),
                    '{"verdict":"PASS","score":94,"defects":[],"summary":"Verified"}',
                ]
            )
            repository = SQLiteRepository(workspace / "state.db")
            engine = build_maintenance_engine(
                repository,
                provider,
                workspace,
                apply=True,
                checks={
                    "parser": (
                        "python",
                        "-c",
                        "from parser import parse; assert parse(' x ') == 'x'",
                    )
                },
                protected_paths=("state.db",),
            )
            issue = GitHubIssue(
                "owner/repo",
                13,
                "Trim parser input",
                "parse should trim surrounding whitespace",
                ("bug",),
                "https://github.com/owner/repo/issues/13",
                "open",
            )
            goal = create_maintenance_goal(
                engine,
                issue,
                goal_id="guarded-13",
                apply=True,
                workspace=workspace,
                check_names=("parser",),
            )

            first = engine.run(goal.goal_id)
            self.assertEqual(first.status, GoalStatus.PAUSED)
            write_approval = repository.list_approvals(goal.goal_id)[0]
            engine.resolve_approval(write_approval.approval_id, approved=True, decided_by="tester")
            second = engine.resume(goal.goal_id)
            self.assertEqual(second.status, GoalStatus.PAUSED)
            check_approval = repository.list_approvals(goal.goal_id)[1]
            self.assertEqual(
                check_approval.arguments["command"][:2], ["python", "-c"]
            )
            engine.resolve_approval(check_approval.approval_id, approved=True, decided_by="tester")
            completed = engine.resume(goal.goal_id)

            self.assertEqual(
                completed.status,
                GoalStatus.SUCCEEDED,
                msg=(
                    f"{completed.reason}; "
                    f"reviews={[review.summary for review in repository.list_reviews(goal.goal_id)]}; "
                    f"events={[event.event_type for event in repository.list_events(goal.goal_id)]}"
                ),
            )
            self.assertIn("strip", source.read_text(encoding="utf-8"))
            self.assertTrue(repository.list_verification_results(goal.goal_id)[0].passed)
            self.assertIn(
                "parser.py",
                repository.list_workspace_snapshots(goal.goal_id)[0].path,
            )
            risks = {
                schema["name"]: schema["risk"]
                for schema in engine.workers["repository-maintainer"].tool_executor.registry.schemas()
            }
            self.assertEqual(risks["workspace_write_file"], ToolRisk.WRITE.value)
            self.assertEqual(risks["workspace_run_check"], ToolRisk.EXECUTE.value)
            repository.close()

    def test_guarded_goal_requires_clean_tracked_git_workspace(self):
        repository = SQLiteRepository(":memory:")
        provider = ScriptedProvider([])
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "file.txt").write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "add", "file.txt"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"],
                cwd=workspace,
                check=True,
            )
            (workspace / "file.txt").write_text("human edit\n", encoding="utf-8")
            engine = build_maintenance_engine(
                repository,
                provider,
                workspace,
                apply=True,
                checks={"test": ("python", "-c", "pass")},
            )
            issue = GitHubIssue(
                "owner/repo", 1, "Issue", "Body", (), "https://example.test/1", "open"
            )

            with self.assertRaisesRegex(ValueError, "clean"):
                create_maintenance_goal(
                    engine,
                    issue,
                    apply=True,
                    workspace=workspace,
                    check_names=("test",),
                )

        repository.close()
    def test_issue_intake_runs_a_reviewed_read_only_maintenance_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "parser.py").write_text(
                "def parse(value):\n    return value.strip()\n", encoding="utf-8"
            )
            provider = ScriptedProvider(
                [
                    '{"tasks":[{"task_id":"proposal","task_type":"repository_maintenance",'
                    '"description":"Inspect the parser issue and propose a verified fix",'
                    '"acceptance_criteria":{"required_sections":["Relevant files",'
                    '"Proposed changes","Tests","Risks"]}}]}',
                    '{"type":"tool_call","name":"workspace_list_files","arguments":{}}',
                    '{"type":"tool_call","name":"workspace_read_file",'
                    '"arguments":{"path":"parser.py"}}',
                    '{"type":"final","content":"Relevant files: parser.py\\n'
                    'Proposed changes: handle empty input\\nTests: add an empty-input test\\n'
                    'Risks: preserve whitespace behavior"}',
                    '{"verdict":"PASS","score":92,"defects":[],"summary":"Accepted"}',
                ]
            )
            repository = SQLiteRepository(":memory:")
            engine = build_maintenance_engine(repository, provider, workspace)
            issue = GitHubIssue(
                "owner/repo",
                12,
                "Parser fails on empty input",
                "Calling parse with an empty string raises an error.",
                ("bug",),
                "https://github.com/owner/repo/issues/12",
                "open",
            )
            goal = create_maintenance_goal(engine, issue, goal_id="maintain-12")

            report = engine.run(goal.goal_id)

            self.assertEqual(report.status, GoalStatus.SUCCEEDED)
            artifact = repository.list_artifacts(goal.goal_id)[0].content
            self.assertIn("Proposed changes", artifact)
            self.assertIn(issue.html_url, repository.get_goal(goal.goal_id).description)
            worker = engine.workers["repository-maintainer"]
            schemas = worker.tool_executor.registry.schemas()
            self.assertTrue(schemas)
            self.assertEqual({item["risk"] for item in schemas}, {"read"})
            span_names = {span.name for span in repository.list_spans(goal.goal_id)}
            self.assertIn("planner.plan", span_names)
            self.assertIn("workspace_read_file", span_names)
            self.assertIn("reviewer.review", span_names)
            repository.close()

    def test_goal_description_contains_normalized_issue_evidence(self):
        repository = SQLiteRepository(":memory:")
        provider = ScriptedProvider([])
        with tempfile.TemporaryDirectory() as directory:
            engine = build_maintenance_engine(repository, provider, directory)
            issue = GitHubIssue(
                "owner/repo",
                7,
                "Improve docs",
                "Add a usage example",
                ("documentation", "good first issue"),
                "https://github.com/owner/repo/issues/7",
                "open",
            )

            goal = create_maintenance_goal(engine, issue, goal_id="docs-7")

        self.assertEqual(goal.title, "Maintain owner/repo#7")
        self.assertIn("Improve docs", goal.description)
        self.assertIn("good first issue", goal.description)
        repository.close()


if __name__ == "__main__":
    unittest.main()
