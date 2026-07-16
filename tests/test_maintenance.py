import tempfile
import unittest
from pathlib import Path

from agent_society_loop.domain import GoalStatus
from agent_society_loop.github import GitHubIssue
from agent_society_loop.maintenance import (
    build_maintenance_engine,
    create_maintenance_goal,
)
from agent_society_loop.storage import SQLiteRepository


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, *, temperature=0.0):
        self.calls.append(list(messages))
        return self.responses.pop(0)


class MaintenanceWorkflowTests(unittest.TestCase):
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
