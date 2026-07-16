"""Policy-controlled tool discovery and execution."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from .domain import ApprovalRequest, ApprovalStatus, ToolRisk
from .storage import SQLiteRepository
from .tracing import TraceRecorder


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: ToolRisk

    def invoke(self, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    goal_id: str
    task_id: str
    agent_id: str
    parent_span_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    output: Any = None
    error: str = ""


class ApprovalRequired(RuntimeError):
    def __init__(self, approval: ApprovalRequest):
        self.approval = approval
        super().__init__(f"approval required for tool {approval.tool_name}")


class ToolDenied(RuntimeError):
    pass


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()):
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool: {tool.name}")
            if not tool.name.strip() or not tool.description.strip():
                raise ValueError("tool name and description must not be empty")
            if not isinstance(tool.risk, ToolRisk):
                raise ValueError(f"tool risk must be a ToolRisk: {tool.name}")
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolDenied(f"unknown tool: {name}") from None

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "risk": tool.risk.value,
            }
            for tool in sorted(self._tools.values(), key=lambda item: item.name)
        ]


class DefaultToolPolicy:
    def requires_approval(self, tool: Tool, context: ToolContext) -> bool:
        return tool.risk in {ToolRisk.WRITE, ToolRisk.EXECUTE}


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: DefaultToolPolicy,
        repository: SQLiteRepository,
        tracer: TraceRecorder | None = None,
    ):
        self.registry = registry
        self.policy = policy
        self.repository = repository
        self.tracer = tracer

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        tool = self.registry.get(name)
        _validate_arguments(tool.input_schema, arguments)
        if self.policy.requires_approval(tool, context):
            requested = ApprovalRequest.create(
                context.goal_id,
                context.task_id,
                tool.name,
                arguments,
                f"{tool.risk.value} tool requires approval",
            )
            approval = (
                self.repository.get_approval_by_fingerprint(requested.fingerprint)
                or requested
            )
            if approval.status == ApprovalStatus.PENDING:
                self.repository.save_approval(approval)
                raise ApprovalRequired(approval)
            if approval.status == ApprovalStatus.REJECTED:
                raise ToolDenied(f"tool call rejected: {tool.name}")

        trace = (
            self.tracer.span(
                context.goal_id,
                tool.name,
                kind="tool",
                task_id=context.task_id,
                agent_id=context.agent_id,
                parent_span_id=context.parent_span_id,
                attributes={"risk": tool.risk.value},
            )
            if self.tracer is not None
            else nullcontext()
        )
        try:
            with trace:
                output = tool.invoke(dict(arguments))
        except Exception as error:
            return ToolResult(False, error=f"{type(error).__name__}: tool execution failed")
        return ToolResult(True, output=output)


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise ToolDenied("tool arguments must be an object")
    if schema.get("type") != "object":
        raise ToolDenied("tool input schema must describe an object")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for name in required:
        if name not in arguments:
            raise ToolDenied(f"required tool argument missing: {name}")
    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(arguments) - set(properties))
        if unexpected:
            raise ToolDenied(f"unexpected tool argument: {unexpected[0]}")
    for name, value in arguments.items():
        expected = properties.get(name, {}).get("type")
        if expected and not _matches_json_type(value, expected):
            raise ToolDenied(f"tool argument {name} must be {expected}")


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False
