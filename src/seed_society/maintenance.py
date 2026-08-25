"""Composition root for read-only and guarded GitHub issue maintenance runs."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from .domain import (
    AgentProfile, Defect, Goal, PublicationStatus, Review, RunBudget, Task, Verdict,
)
from .engine import LoopEngine
from .github import GitHubIssue
from .memory import MemoryManager
from .model_agents import ModelPlanner, ModelReviewer, ModelWorker
from .ports import ModelProvider
from .publication import PullRequestClient, WorkspacePublishPullRequestTool
from .selection import PerformanceWeightedSelector
from .storage import SQLiteRepository
from .tools import DefaultToolPolicy, ToolContext, ToolExecutor, ToolRegistry
from .tracing import TraceRecorder
from .workspace_tools import (
    WorkspaceDiffTool,
    WorkspaceListFilesTool,
    WorkspaceReadFileTool,
    WorkspaceRestoreChangesTool,
    WorkspaceRunCheckTool,
    WorkspaceSearchTool,
    WorkspaceWriteFileTool,
)


MAINTAINER_AGENT_ID = "repository-maintainer"


class VerificationGateReviewer:
    def __init__(
        self,
        delegate: ModelReviewer,
        repository: SQLiteRepository,
        diff_tool: WorkspaceDiffTool,
        check_names: tuple[str, ...],
    ):
        self.delegate = delegate
        self.repository = repository
        self.diff_tool = diff_tool
        self.check_names = tuple(sorted(check_names))

    def review(
        self, task: Task, artifact: str, attempt_no: int
    ) -> Review:
        review = self.delegate.review(task, artifact, attempt_no)
        if review.verdict == Verdict.FAIL:
            return review
        diff = self.diff_tool.invoke_with_context(
            {}, ToolContext(task.goal_id, task.task_id, "verification-gate")
        )
        defects = []
        if not diff["changed_files"]:
            defects.append(
                Defect(
                    "workspace",
                    "no goal-owned file changes are present",
                    "apply and inspect the required change before finishing",
                )
            )
        results = self.repository.list_verification_results(task.goal_id)
        missing = []
        for name in self.check_names:
            if not any(
                result.check_name == name
                and result.passed
                and result.workspace_digest == diff["workspace_digest"]
                for result in results
            ):
                missing.append(name)
                defects.append(
                    Defect(
                        f"check:{name}",
                        "no passing result exists for the current workspace digest",
                        f"run workspace_run_check with name {name} after the latest write",
                    )
                )
        if not defects:
            return review
        details = ", ".join(missing) if missing else "workspace change"
        return Review.create(
            task.goal_id,
            task.task_id,
            attempt_no,
            Verdict.FAIL,
            min(review.score, 60.0),
            defects,
            f"Verification gate failed: {details}",
        )


class PublicationGateReviewer:
    def __init__(self, delegate: object, repository: SQLiteRepository):
        self.delegate = delegate
        self.repository = repository

    def review(self, task: Task, artifact: str, attempt_no: int) -> Review:
        review = self.delegate.review(task, artifact, attempt_no)
        if review.verdict == Verdict.FAIL:
            return review
        publication = self.repository.get_publication(task.goal_id)
        if publication is not None and publication.status == PublicationStatus.PULL_REQUEST_CREATED:
            return review
        return Review.create(
            task.goal_id, task.task_id, attempt_no, Verdict.FAIL, min(review.score, 60.0),
            [Defect("publication", "no completed pull request publication exists", "use workspace_publish_pull_request after all checks pass")],
            "Publication gate failed: pull request not created",
        )


def build_maintenance_engine(
    repository: SQLiteRepository,
    provider: ModelProvider,
    workspace: str | Path,
    *,
    apply: bool = False,
    checks: dict[str, tuple[str, ...]] | None = None,
    protected_paths: tuple[str, ...] = (),
    publish: bool = False,
    pull_request_client: PullRequestClient | None = None,
    github_repository: str = "",
    base_branch: str = "main",
    remote: str = "origin",
    branch_prefix: str = "seed-society/",
    max_actions: int = 30,
    max_tool_steps: int = 12,
) -> LoopEngine:
    tracer = TraceRecorder(repository)
    tools = [
        WorkspaceListFilesTool(workspace),
        WorkspaceReadFileTool(workspace),
        WorkspaceSearchTool(workspace),
    ]
    diff_tool = None
    if apply:
        if not checks:
            raise ValueError("guarded maintenance requires at least one named check")
        diff_tool = WorkspaceDiffTool(workspace, repository)
        tools.extend(
            [
                WorkspaceWriteFileTool(
                    workspace, repository, protected_paths=protected_paths
                ),
                diff_tool,
                WorkspaceRunCheckTool(workspace, repository, checks),
                WorkspaceRestoreChangesTool(workspace, repository),
            ]
        )
        if publish:
            if pull_request_client is None or not github_repository.strip():
                raise ValueError("publication requires GitHub repository and pull-request client")
            tools.append(
                WorkspacePublishPullRequestTool(
                    workspace,
                    repository,
                    pull_request_client,
                    github_repository=github_repository,
                    check_names=tuple(checks),
                    base_branch=base_branch,
                    remote=remote,
                    branch_prefix=branch_prefix,
                )
            )
    elif checks:
        raise ValueError("named checks require guarded apply mode")
    if publish and not apply:
        raise ValueError("publication requires guarded apply mode")
    registry = ToolRegistry(tools)
    executor = ToolExecutor(
        registry,
        DefaultToolPolicy(),
        repository,
        tracer,
    )
    worker = ModelWorker(
        MAINTAINER_AGENT_ID,
        provider,
        executor,
        max_tool_steps=max_tool_steps,
        tracer=tracer,
    )
    repository.save_agent(
        AgentProfile(
            MAINTAINER_AGENT_ID,
            "worker",
            getattr(provider, "model", "injected-model"),
            ("*",),
        )
    )
    memory = MemoryManager(repository)
    reviewer = ModelReviewer(provider, tracer)
    if apply:
        assert diff_tool is not None
        reviewer = VerificationGateReviewer(
            reviewer, repository, diff_tool, tuple(checks or {})
        )
        if publish:
            reviewer = PublicationGateReviewer(reviewer, repository)
    return LoopEngine(
        planner=ModelPlanner(provider, tracer),
        workers={MAINTAINER_AGENT_ID: worker},
        reviewer=reviewer,
        repository=repository,
        memory=memory,
        selector=PerformanceWeightedSelector(),
        budget=RunBudget(max_actions=max_actions),
        tracer=tracer,
    )


def create_maintenance_goal(
    engine: LoopEngine,
    issue: GitHubIssue,
    *,
    goal_id: str | None = None,
    apply: bool = False,
    workspace: str | Path | None = None,
    check_names: tuple[str, ...] = (),
    publish: bool = False,
    base_branch: str = "main",
    remote: str = "origin",
    branch_prefix: str = "seed-society/",
) -> Goal:
    if apply:
        if workspace is None:
            raise ValueError("guarded maintenance requires a workspace")
        if not check_names:
            raise ValueError("guarded maintenance requires at least one named check")
        _require_clean_tracked_workspace(workspace)
    elif check_names:
        raise ValueError("named checks require guarded apply mode")
    if publish and not apply:
        raise ValueError("publication requires guarded apply mode")
    boundary = (
        "Inspect the local checkout, implement content-addressed UTF-8 changes, "
        "run every configured named check, repair failures, and report the final "
        "diff. Do not commit, push, or create a pull request."
        if apply
        else (
            "Inspect the local checkout with read-only tools and produce a reviewed "
            "maintenance proposal. Do not modify files, execute commands, or create a pull request."
        )
    )
    evidence = {
        "repository": issue.repository,
        "issue_number": issue.number,
        "issue_url": issue.html_url,
        "state": issue.state,
        "title": issue.title,
        "body": issue.body,
        "labels": list(issue.labels),
        "execution_mode": "guarded_apply" if apply else "read_only",
        "operating_boundary": (
            boundary.replace(
                "Do not commit, push, or create a pull request.",
                "Publish only through the verification-gated pull-request tool. Do not merge or force-push.",
            )
            if publish else boundary
        ),
        "checks": sorted(check_names),
        "publication": {
            "enabled": publish,
            "base_branch": base_branch,
            "remote": remote,
            "branch_prefix": branch_prefix,
        },
        "required_output_sections": (
            ["Issue interpretation", "Changed files", "Checks", "Pull request", "Risks"]
            if publish
            else ["Issue interpretation", "Changed files", "Checks", "Risks"]
            if apply
            else [
                "Issue interpretation",
                "Relevant files",
                "Proposed changes",
                "Tests",
                "Risks",
            ]
        ),
    }
    return engine.create_goal(
        f"Maintain {issue.repository}#{issue.number}",
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
        goal_id=goal_id,
    )


def maintenance_goal_configuration(goal: Goal) -> tuple[bool, tuple[str, ...]]:
    try:
        evidence = json.loads(goal.description)
    except json.JSONDecodeError as error:
        raise ValueError("maintenance goal has invalid configuration") from error
    mode = evidence.get("execution_mode", "read_only")
    if mode not in {"read_only", "guarded_apply"}:
        raise ValueError("maintenance goal has unknown execution mode")
    checks = evidence.get("checks", [])
    if not isinstance(checks, list) or any(not isinstance(item, str) for item in checks):
        raise ValueError("maintenance goal has invalid checks")
    return mode == "guarded_apply", tuple(sorted(checks))


def maintenance_publication_configuration(goal: Goal) -> dict[str, object]:
    try:
        evidence = json.loads(goal.description)
    except json.JSONDecodeError as error:
        raise ValueError("maintenance goal has invalid configuration") from error
    publication = evidence.get("publication", {})
    if not isinstance(publication, dict) or not isinstance(publication.get("enabled", False), bool):
        raise ValueError("maintenance goal has invalid publication configuration")
    return {
        "enabled": publication.get("enabled", False),
        "base_branch": publication.get("base_branch", "main"),
        "remote": publication.get("remote", "origin"),
        "branch_prefix": publication.get("branch_prefix", "seed-society/"),
    }


def _require_clean_tracked_workspace(workspace: str | Path) -> None:
    root = Path(workspace).resolve()
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("guarded maintenance requires an accessible Git workspace") from error
    if completed.returncode != 0:
        raise ValueError("guarded maintenance requires a Git workspace")
    if completed.stdout.strip():
        raise ValueError("guarded maintenance requires a clean tracked Git workspace")
