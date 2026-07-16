"""Agent Society Loop public package."""

from .domain import (
    ApprovalRequest,
    ApprovalStatus,
    Goal,
    GoalStatus,
    Review,
    RunBudget,
    Task,
    TaskStatus,
    ToolRisk,
    TraceSpan,
    VerificationResult,
    Verdict,
    WorkspaceSnapshot,
)

__all__ = [
    "ApprovalRequest",
    "ApprovalStatus",
    "Goal",
    "GoalStatus",
    "Review",
    "RunBudget",
    "Task",
    "TaskStatus",
    "ToolRisk",
    "TraceSpan",
    "VerificationResult",
    "Verdict",
    "WorkspaceSnapshot",
]

__version__ = "0.3.0"
