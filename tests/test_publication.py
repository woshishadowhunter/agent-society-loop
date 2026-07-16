import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_society_loop.github import GitHubPullRequest
from agent_society_loop.domain import PublicationRecord
from agent_society_loop.publication import WorkspacePublishPullRequestTool
from agent_society_loop.storage import SQLiteRepository
from agent_society_loop.tools import ToolContext
from agent_society_loop.workspace_tools import WorkspaceRunCheckTool, WorkspaceWriteFileTool


def git(root, *arguments):
    return subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


class FakePullRequests:
    def __init__(self):
        self.calls = []

    def create_or_get(self, repository, head, base, title, body):
        self.calls.append((repository, head, base, title, body))
        return GitHubPullRequest(17, "https://github.com/owner/repo/pull/17")


class PublicationTests(unittest.TestCase):
    def test_verified_goal_changes_commit_push_and_create_one_idempotent_pr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            remote = root / "remote.git"
            workspace.mkdir()
            git(workspace, "init", "-q")
            git(workspace, "config", "user.name", "Test")
            git(workspace, "config", "user.email", "test@example.com")
            source = workspace / "app.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            git(workspace, "add", "app.py")
            git(workspace, "commit", "-qm", "initial")
            git(workspace, "branch", "-M", "main")
            subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
            git(workspace, "remote", "add", "origin", str(remote))
            git(workspace, "push", "-u", "origin", "main")
            git(workspace, "switch", "-c", "agent-society/fix")

            repository = SQLiteRepository(root / "state.db")
            context = ToolContext("goal", "task", "agent")
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            WorkspaceWriteFileTool(workspace, repository).invoke_with_context(
                {"path": "app.py", "content": "VALUE = 2\n", "expected_sha256": before},
                context,
            )
            WorkspaceRunCheckTool(
                workspace,
                repository,
                {"tests": ("python", "-c", "from app import VALUE; assert VALUE == 2")},
            ).invoke_with_context({"name": "tests"}, context)
            pull_requests = FakePullRequests()
            tool = WorkspacePublishPullRequestTool(
                workspace,
                repository,
                pull_requests,
                github_repository="owner/repo",
                check_names=("tests",),
            )
            arguments = {"title": "fix: update value", "body": "Closes #1"}

            approval = tool.approval_arguments_with_context(arguments, context)
            repository.save_publication(PublicationRecord.create("goal", approval))
            git(workspace, "add", "--", "app.py")
            git(workspace, "commit", "-m", arguments["title"])
            first = tool.invoke_with_context(arguments, context)
            second = tool.invoke_with_context(arguments, context)

            self.assertEqual(approval["changed_paths"], ["app.py"])
            self.assertEqual(first, second)
            self.assertEqual(len(pull_requests.calls), 1)
            self.assertEqual(git(workspace, "rev-list", "--count", "main..HEAD"), "1")
            self.assertEqual(
                git(remote, "rev-parse", "refs/heads/agent-society/fix"),
                first["commit_sha"],
            )
            self.assertEqual(repository.get_publication("goal").pull_request_number, 17)
            repository.close()

    def test_publication_rejects_missing_current_check_and_extra_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            git(workspace, "init", "-q")
            git(workspace, "config", "user.name", "Test")
            git(workspace, "config", "user.email", "test@example.com")
            source = workspace / "app.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            git(workspace, "add", "app.py")
            git(workspace, "commit", "-qm", "initial")
            git(workspace, "switch", "-c", "agent-society/fix")
            repository = SQLiteRepository(root / "state.db")
            context = ToolContext("goal", "task", "agent")
            WorkspaceWriteFileTool(workspace, repository).invoke_with_context(
                {
                    "path": "app.py",
                    "content": "VALUE = 2\n",
                    "expected_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                },
                context,
            )
            tool = WorkspacePublishPullRequestTool(
                workspace, repository, FakePullRequests(),
                github_repository="owner/repo", check_names=("tests",),
            )
            arguments = {"title": "fix", "body": "body"}

            with self.assertRaisesRegex(ValueError, "passing checks"):
                tool.approval_arguments_with_context(arguments, context)

            WorkspaceRunCheckTool(
                workspace, repository, {"tests": ("python", "-c", "pass")}
            ).invoke_with_context({"name": "tests"}, context)
            (workspace / "human.txt").write_text("unowned\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly match"):
                tool.invoke_with_context(arguments, context)
            repository.close()


if __name__ == "__main__":
    unittest.main()
