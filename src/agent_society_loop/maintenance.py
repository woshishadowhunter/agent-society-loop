"""Composition root for read-only GitHub issue maintenance runs."""

from __future__ import annotations

import json
from pathlib import Path

from .domain import AgentProfile, Goal, RunBudget
from .engine import LoopEngine
from .github import GitHubIssue
from .memory import MemoryManager
from .model_agents import ModelPlanner, ModelReviewer, ModelWorker
from .ports import ModelProvider
from .selection import PerformanceWeightedSelector
from .storage import SQLiteRepository
from .tools import DefaultToolPolicy, ToolExecutor, ToolRegistry
from .tracing import TraceRecorder
from .workspace_tools import (
    WorkspaceListFilesTool,
    WorkspaceReadFileTool,
    WorkspaceSearchTool,
)


MAINTAINER_AGENT_ID = "repository-maintainer"


def build_maintenance_engine(
    repository: SQLiteRepository,
    provider: ModelProvider,
    workspace: str | Path,
    *,
    max_actions: int = 30,
    max_tool_steps: int = 12,
) -> LoopEngine:
    tracer = TraceRecorder(repository)
    registry = ToolRegistry(
        [
            WorkspaceListFilesTool(workspace),
            WorkspaceReadFileTool(workspace),
            WorkspaceSearchTool(workspace),
        ]
    )
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
    return LoopEngine(
        planner=ModelPlanner(provider, tracer),
        workers={MAINTAINER_AGENT_ID: worker},
        reviewer=ModelReviewer(provider, tracer),
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
) -> Goal:
    evidence = {
        "repository": issue.repository,
        "issue_number": issue.number,
        "issue_url": issue.html_url,
        "state": issue.state,
        "title": issue.title,
        "body": issue.body,
        "labels": list(issue.labels),
        "operating_boundary": (
            "Inspect the local checkout with read-only tools and produce a reviewed "
            "maintenance proposal. Do not modify files, execute commands, or create a pull request."
        ),
        "required_output_sections": [
            "Issue interpretation",
            "Relevant files",
            "Proposed changes",
            "Tests",
            "Risks",
        ],
    }
    return engine.create_goal(
        f"Maintain {issue.repository}#{issue.number}",
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
        goal_id=goal_id,
    )
