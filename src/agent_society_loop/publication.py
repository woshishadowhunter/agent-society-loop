"""Verification-gated, resumable Git and GitHub publication."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any, Protocol

from .domain import PublicationRecord, PublicationStatus, ToolRisk
from .github import GitHubPullRequest
from .storage import SQLiteRepository
from .tools import ToolContext
from .workspace_tools import WorkspaceDiffTool


_IGNORED_GIT_PATH_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "build", "dist",
}


class PullRequestClient(Protocol):
    def create_or_get(
        self, repository: str, head: str, base: str, title: str, body: str
    ) -> GitHubPullRequest: ...


class WorkspacePublishPullRequestTool:
    name = "workspace_publish_pull_request"
    description = "Commit verified goal-owned changes, push the allowed branch, and create a pull request"
    risk = ToolRisk.WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["title", "body"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        root: str | Path,
        repository_store: SQLiteRepository,
        pull_requests: PullRequestClient,
        *,
        github_repository: str,
        check_names: tuple[str, ...],
        base_branch: str = "main",
        remote: str = "origin",
        branch_prefix: str = "agent-society/",
        timeout_seconds: float = 60.0,
    ):
        self.root = Path(root).resolve()
        self.repository_store = repository_store
        self.pull_requests = pull_requests
        self.github_repository = github_repository
        self.check_names = tuple(sorted(check_names))
        self.base_branch = base_branch
        self.remote = remote
        self.branch_prefix = branch_prefix
        self.timeout_seconds = timeout_seconds
        self.diff_tool = WorkspaceDiffTool(self.root, repository_store)
        if not self.check_names or not all(
            value.strip()
            for value in (github_repository, base_branch, remote, branch_prefix)
        ):
            raise ValueError("publication configuration must not be empty")

    def approval_arguments_with_context(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        return self._payload(arguments, context)

    def invoke_with_context(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        payload = self._payload(arguments, context)
        record = self.repository_store.get_publication(context.goal_id)
        expected = PublicationRecord.create(context.goal_id, payload)
        if record is None:
            changed = _git_changes(self.root, self.timeout_seconds)
            if changed != set(expected.changed_paths):
                raise ValueError("Git changes do not exactly match goal-owned paths")
            if _has_staged_changes(self.root, self.timeout_seconds):
                raise ValueError("publication refuses pre-existing staged changes")
            record = expected
            self.repository_store.save_publication(record)
        elif record.payload_digest != expected.payload_digest:
            raise ValueError("publication payload changed after approval")

        if record.status == PublicationStatus.PREPARED:
            current_head = _git(self.root, ["rev-parse", "HEAD"], self.timeout_seconds).strip()
            if current_head == record.base_head_sha:
                if _git_changes(self.root, self.timeout_seconds) != set(record.changed_paths):
                    raise ValueError("Git changes do not exactly match goal-owned paths")
                _git(self.root, ["add", "--", *record.changed_paths], self.timeout_seconds)
                _git(self.root, ["commit", "-m", record.title], self.timeout_seconds)
                commit_sha = _git(self.root, ["rev-parse", "HEAD"], self.timeout_seconds).strip()
            else:
                parent = _git(self.root, ["rev-parse", "HEAD^"], self.timeout_seconds).strip()
                subject = _git(self.root, ["log", "-1", "--format=%s"], self.timeout_seconds).strip()
                committed_paths = set(
                    _git(self.root, ["diff", "--name-only", f"{record.base_head_sha}..HEAD"], self.timeout_seconds).splitlines()
                )
                if parent != record.base_head_sha or subject != record.title or committed_paths != set(record.changed_paths):
                    raise ValueError("cannot recover publication commit from current HEAD")
                if _git_changes(self.root, self.timeout_seconds):
                    raise ValueError("workspace changed after recovered publication commit")
                commit_sha = current_head
            record = record.advance(PublicationStatus.COMMITTED, commit_sha=commit_sha)
            self.repository_store.save_publication(record)

        current_head = _git(self.root, ["rev-parse", "HEAD"], self.timeout_seconds).strip()
        if current_head != record.commit_sha:
            raise ValueError("workspace HEAD changed after publication commit")

        if record.status == PublicationStatus.COMMITTED:
            if _git_changes(self.root, self.timeout_seconds):
                raise ValueError("workspace changed after publication commit")
            _git(
                self.root,
                ["push", "-u", self.remote, f"HEAD:refs/heads/{record.branch}"],
                self.timeout_seconds,
            )
            record = record.advance(PublicationStatus.PUSHED)
            self.repository_store.save_publication(record)

        if record.status == PublicationStatus.PUSHED:
            pull_request = self.pull_requests.create_or_get(
                record.repository,
                record.branch,
                record.base_branch,
                record.title,
                record.body,
            )
            record = record.advance(
                PublicationStatus.PULL_REQUEST_CREATED,
                pull_request_number=pull_request.number,
                pull_request_url=pull_request.html_url,
            )
            self.repository_store.save_publication(record)

        return {
            "status": record.status.value,
            "commit_sha": record.commit_sha,
            "pull_request_number": record.pull_request_number,
            "pull_request_url": record.pull_request_url,
        }

    def _payload(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        title = arguments.get("title")
        body = arguments.get("body")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("pull request title must contain 1 to 200 characters")
        if not isinstance(body, str) or not body.strip() or len(body) > 20_000:
            raise ValueError("pull request body must contain 1 to 20000 characters")
        branch = _git(self.root, ["branch", "--show-current"], self.timeout_seconds).strip()
        if branch == self.base_branch or not branch.startswith(self.branch_prefix):
            raise ValueError("current branch is not allowed for publication")
        diff = self.diff_tool.invoke_with_context({}, context)
        if not diff["changed_files"]:
            raise ValueError("publication requires goal-owned file changes")
        results = self.repository_store.list_verification_results(context.goal_id)
        missing = [
            name
            for name in self.check_names
            if not any(
                item.check_name == name
                and item.passed
                and item.workspace_digest == diff["workspace_digest"]
                for item in results
            )
        ]
        if missing:
            raise ValueError(f"publication requires current passing checks: {', '.join(missing)}")
        verified_body = (
            f"{body.strip()}\n\n## Agent Society verification\n"
            f"- Workspace digest: `{diff['workspace_digest']}`\n"
            f"- Checks: {', '.join(self.check_names)}"
        )
        existing = self.repository_store.get_publication(context.goal_id)
        base_head_sha = (
            existing.base_head_sha
            if existing is not None
            else _git(self.root, ["rev-parse", "HEAD"], self.timeout_seconds).strip()
        )
        return {
            "repository": self.github_repository,
            "remote": self.remote,
            "branch": branch,
            "base_branch": self.base_branch,
            "title": title.strip(),
            "body": verified_body,
            "base_head_sha": base_head_sha,
            "changed_paths": sorted(diff["changed_files"]),
            "workspace_digest": diff["workspace_digest"],
            "check_names": list(self.check_names),
        }


def _git(root: Path, arguments: list[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments], cwd=root, shell=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Git command could not complete") from error
    if completed.returncode != 0:
        raise RuntimeError(f"Git command failed: {arguments[0]}")
    return completed.stdout


def _git_changes(root: Path, timeout: float) -> set[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root, shell=False, capture_output=True, timeout=timeout, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Git status failed")
    entries = completed.stdout.split(b"\0")
    paths = set()
    for entry in entries:
        if not entry:
            continue
        status = entry[:2].decode("ascii", errors="replace")
        if "R" in status or "C" in status:
            raise ValueError("publication does not support renamed or copied paths")
        try:
            path = entry[3:].decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Git path is not UTF-8") from None
        if any(part in _IGNORED_GIT_PATH_PARTS for part in Path(path).parts):
            continue
        paths.add(path)
    return paths


def _has_staged_changes(root: Path, timeout: float) -> bool:
    completed = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=root, shell=False,
        timeout=timeout, check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError("Git staged diff failed")
    return completed.returncode == 1
