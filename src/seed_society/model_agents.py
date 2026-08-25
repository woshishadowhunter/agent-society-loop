"""Strict JSON adapters for model-backed society roles."""

from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Any, Sequence

from .domain import Defect, Goal, Review, Task, Verdict, validate_task_graph
from .ports import ModelProvider
from .tools import ApprovalRequired, ToolContext, ToolDenied, ToolExecutor, ToolResult
from .tracing import TraceRecorder


def parse_json_object(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip().startswith("{"):
        raise ValueError("model output must be exactly one JSON object")
    source = text.strip()
    try:
        value, end = json.JSONDecoder().raw_decode(source)
    except json.JSONDecodeError as error:
        raise ValueError("model output must be exactly one JSON object") from error
    if end != len(source) or not isinstance(value, dict):
        raise ValueError("model output must be exactly one JSON object")
    return value


class ModelPlanner:
    def __init__(
        self,
        provider: ModelProvider,
        tracer: TraceRecorder | None = None,
    ):
        self.provider = provider
        self.tracer = tracer

    def plan(self, goal: Goal, context: dict[str, Any]) -> Sequence[Task]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the planning agent. Return strict JSON with no markdown. "
                    "Output exactly one object shaped as {\"tasks\":[{\"task_id\":str,"
                    "\"task_type\":str,\"description\":str,\"dependencies\":[str],"
                    "\"acceptance_criteria\":object}]}."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"goal": {"title": goal.title, "description": goal.description}, "context": context},
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                ),
            },
        ]
        data = parse_json_object(
            _complete(self.provider, messages, self.tracer, goal.goal_id, "planner.plan")
        )
        _require_keys(data, {"tasks"}, {"tasks"}, "planner output")
        task_values = data["tasks"]
        if not isinstance(task_values, list) or not task_values:
            raise ValueError("planner output tasks must be a non-empty list")
        tasks = []
        for index, value in enumerate(task_values, start=1):
            if not isinstance(value, dict):
                raise ValueError(f"planner task {index} must be an object")
            _require_keys(
                value,
                {"task_id", "task_type", "description"},
                {
                    "task_id",
                    "task_type",
                    "description",
                    "assigned_role",
                    "acceptance_criteria",
                    "dependencies",
                    "context",
                    "max_attempts",
                    "position",
                },
                f"planner task {index}",
            )
            for field in ("task_id", "task_type", "description"):
                if not isinstance(value[field], str):
                    raise ValueError(f"planner task {index} {field} must be a string")
            dependencies = value.get("dependencies", [])
            if not isinstance(dependencies, list) or any(
                not isinstance(item, str) for item in dependencies
            ):
                raise ValueError(f"planner task {index} dependencies must be strings")
            criteria = value.get("acceptance_criteria", {})
            task_context = value.get("context", {})
            if not isinstance(criteria, dict) or not isinstance(task_context, dict):
                raise ValueError(
                    f"planner task {index} acceptance_criteria and context must be objects"
                )
            tasks.append(
                Task.create(
                    goal.goal_id,
                    value["task_id"],
                    value["task_type"],
                    value["description"],
                    assigned_role=str(value.get("assigned_role", "worker")),
                    acceptance_criteria=criteria,
                    dependencies=dependencies,
                    context=task_context,
                    max_attempts=_integer(value.get("max_attempts", 3), "max_attempts"),
                    position=_integer(value.get("position", index), "position"),
                )
            )
        validate_task_graph(tasks)
        return tasks


class ModelReviewer:
    def __init__(
        self,
        provider: ModelProvider,
        tracer: TraceRecorder | None = None,
        *,
        agent_id: str = "reviewer",
    ):
        if not agent_id.strip():
            raise ValueError("reviewer agent_id must not be empty")
        self.provider = provider
        self.tracer = tracer
        self.agent_id = agent_id.strip()

    def review(self, task: Task, artifact: str, attempt_no: int) -> Review:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an independent quality reviewer. Return strict JSON with no markdown. "
                    "Output exactly one object with verdict PASS or FAIL, score 0-100, defects "
                    "as objects containing location, issue, suggestion, and summary."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": {
                            "description": task.description,
                            "acceptance_criteria": task.acceptance_criteria,
                        },
                        "artifact": artifact,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ]
        data = parse_json_object(
            _complete(
                self.provider,
                messages,
                self.tracer,
                task.goal_id,
                "reviewer.review",
                task_id=task.task_id,
                agent_id=self.agent_id,
            )
        )
        _require_keys(
            data,
            {"verdict", "score", "defects", "summary"},
            {"verdict", "score", "defects", "summary"},
            "reviewer output",
        )
        try:
            verdict = Verdict(data["verdict"])
        except (TypeError, ValueError):
            raise ValueError("reviewer verdict must be PASS or FAIL") from None
        score = data["score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("reviewer score must be a number")
        if not isinstance(data["summary"], str):
            raise ValueError("reviewer summary must be a string")
        if not isinstance(data["defects"], list):
            raise ValueError("reviewer defects must be a list")
        defects = []
        for index, value in enumerate(data["defects"], start=1):
            if not isinstance(value, dict):
                raise ValueError(f"reviewer defect {index} must be an object")
            _require_keys(
                value,
                {"location", "issue", "suggestion"},
                {"location", "issue", "suggestion"},
                f"reviewer defect {index}",
            )
            if any(not isinstance(value[field], str) for field in value):
                raise ValueError(f"reviewer defect {index} values must be strings")
            defects.append(Defect(**value))
        return Review.create(
            task.goal_id,
            task.task_id,
            attempt_no,
            verdict,
            float(score),
            defects,
            data["summary"],
        )


class ModelWorker:
    def __init__(
        self,
        agent_id: str,
        provider: ModelProvider,
        tool_executor: ToolExecutor | None = None,
        *,
        max_tool_steps: int = 8,
        tracer: TraceRecorder | None = None,
    ):
        if not agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if max_tool_steps < 1:
            raise ValueError("max_tool_steps must be positive")
        self.agent_id = agent_id
        self.provider = provider
        self.tool_executor = tool_executor
        self.max_tool_steps = max_tool_steps
        self.tracer = tracer

    def execute(self, task: Task, context: dict[str, Any]) -> str:
        parent = (
            self.tracer.span(
                task.goal_id,
                "worker.execute",
                kind="agent",
                task_id=task.task_id,
                agent_id=self.agent_id,
            )
            if self.tracer is not None
            else nullcontext(None)
        )
        with parent as parent_span:
            parent_span_id = parent_span.span_id if parent_span is not None else None
            return self._execute_loop(task, context, parent_span_id)

    def _execute_loop(
        self,
        task: Task,
        context: dict[str, Any],
        parent_span_id: str | None,
    ) -> str:
        schemas = (
            self.tool_executor.registry.schemas()
            if self.tool_executor is not None
            else []
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an execution specialist. Return strict JSON with no markdown. "
                    "Either request one tool as {\"type\":\"tool_call\",\"name\":str,"
                    "\"arguments\":object} or finish as {\"type\":\"final\",\"content\":str}."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": {
                            "description": task.description,
                            "acceptance_criteria": task.acceptance_criteria,
                        },
                        "context": context,
                        "tools": schemas,
                    },
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                ),
            },
        ]
        tool_steps = 0
        while True:
            raw = _complete(
                self.provider,
                messages,
                self.tracer,
                task.goal_id,
                "worker.model",
                task_id=task.task_id,
                agent_id=self.agent_id,
                parent_span_id=parent_span_id,
            )
            data = parse_json_object(raw)
            action_type = data.get("type")
            if action_type == "final":
                _require_keys(data, {"type", "content"}, {"type", "content"}, "worker final")
                if not isinstance(data["content"], str) or not data["content"].strip():
                    raise ValueError("worker final content must be a non-empty string")
                return data["content"].strip()
            if action_type != "tool_call":
                raise ValueError("worker output type must be tool_call or final")
            _require_keys(
                data,
                {"type", "name", "arguments"},
                {"type", "name", "arguments"},
                "worker tool call",
            )
            if not isinstance(data["name"], str) or not isinstance(data["arguments"], dict):
                raise ValueError("worker tool call name and arguments are invalid")
            if self.tool_executor is None:
                raise ToolDenied("worker has no tools")
            if tool_steps >= self.max_tool_steps:
                raise RuntimeError(f"tool step budget exhausted at {self.max_tool_steps}")
            tool_steps += 1
            try:
                result = self.tool_executor.execute(
                    data["name"],
                    data["arguments"],
                    ToolContext(
                        task.goal_id,
                        task.task_id,
                        self.agent_id,
                        parent_span_id,
                    ),
                )
            except ApprovalRequired:
                raise
            except ToolDenied as error:
                result = ToolResult(False, error=str(error))
            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "tool_result": {
                                    "name": data["name"],
                                    "ok": result.ok,
                                    "output": result.output,
                                    "error": result.error,
                                }
                            },
                            ensure_ascii=False,
                            default=str,
                            sort_keys=True,
                        ),
                    },
                ]
            )


def _complete(
    provider: ModelProvider,
    messages: Sequence[dict[str, str]],
    tracer: TraceRecorder | None,
    goal_id: str,
    name: str,
    *,
    task_id: str | None = None,
    agent_id: str | None = None,
    parent_span_id: str | None = None,
) -> str:
    span = (
        tracer.span(
            goal_id,
            name,
            kind="model",
            task_id=task_id,
            agent_id=agent_id,
            parent_span_id=parent_span_id,
            attributes={"message_count": len(messages)},
        )
        if tracer is not None
        else nullcontext()
    )
    with span:
        return provider.complete(messages, temperature=0.0)


def _require_keys(
    value: dict[str, Any],
    required: set[str],
    allowed: set[str],
    location: str,
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"{location} missing required field: {missing[0]}")
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValueError(f"{location} has unexpected field: {extra[0]}")


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value
