"""Dependency-inversion protocols used by the runtime engine."""

from __future__ import annotations

from typing import Any, Protocol, Sequence

from .domain import Goal, Review, Task


class Planner(Protocol):
    def plan(self, goal: Goal, context: dict[str, Any]) -> Sequence[Task]: ...


class Worker(Protocol):
    agent_id: str

    def execute(self, task: Task, context: dict[str, Any]) -> str: ...


class Reviewer(Protocol):
    def review(self, task: Task, artifact: str, attempt_no: int) -> Review: ...


class ModelProvider(Protocol):
    def complete(
        self, messages: Sequence[dict[str, str]], *, temperature: float = 0.0
    ) -> str: ...
