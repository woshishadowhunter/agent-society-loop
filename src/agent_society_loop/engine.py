"""Goal lifecycle engine implementing the outer and inner loops."""

from __future__ import annotations

from dataclasses import replace
from time import perf_counter
from typing import Mapping

from .domain import (
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    Attempt,
    Defect,
    Event,
    Goal,
    GoalStatus,
    Review,
    RunBudget,
    RunReport,
    Task,
    TaskStatus,
    Verdict,
    transition_goal,
    validate_task_graph,
)
from .memory import MemoryManager
from .ports import Planner, Reviewer, Worker, WorkerBlocked
from .selection import PerformanceWeightedSelector
from .storage import SQLiteRepository
from .tools import ApprovalRequired
from .tracing import TraceRecorder


class LoopEngine:
    def __init__(
        self,
        *,
        planner: Planner,
        workers: Mapping[str, Worker],
        reviewer: Reviewer,
        repository: SQLiteRepository,
        memory: MemoryManager,
        selector: PerformanceWeightedSelector,
        budget: RunBudget | None = None,
        tracer: TraceRecorder | None = None,
    ):
        self.planner = planner
        self.workers = dict(workers)
        self.reviewer = reviewer
        self.repository = repository
        self.memory = memory
        self.selector = selector
        self.budget = budget or RunBudget()
        self.tracer = tracer or TraceRecorder(repository)

    def create_goal(
        self, title: str, description: str, *, goal_id: str | None = None
    ) -> Goal:
        goal = Goal.create(title, description, goal_id)
        if self.repository.get_goal(goal.goal_id) is not None:
            raise ValueError(f"goal already exists: {goal.goal_id}")
        self.repository.save_goal(goal)
        self._event(goal.goal_id, "goal.created", {"title": goal.title})
        return goal

    def run(self, goal_id: str) -> RunReport:
        goal = self.repository.get_goal(goal_id)
        if goal is None:
            raise KeyError(f"goal not found: {goal_id}")
        if goal.status in {GoalStatus.SUCCEEDED, GoalStatus.FAILED, GoalStatus.BLOCKED}:
            return self._report(goal)

        if goal.status == GoalStatus.PAUSED:
            approvals = self.repository.list_approvals(goal.goal_id)
            if any(item.status == ApprovalStatus.PENDING for item in approvals):
                return self._report(goal)
            rejected = next(
                (
                    item
                    for item in approvals
                    if item.status == ApprovalStatus.REJECTED
                ),
                None,
            )
            if rejected is not None:
                reason = f"approval rejected for tool {rejected.tool_name}"
                goal = transition_goal(goal, GoalStatus.FAILED, reason)
                self.repository.save_goal(goal)
                self._event(goal.goal_id, "goal.failed", {"reason": reason})
                return self._report(goal)
            goal = transition_goal(goal, GoalStatus.RUNNING)
            self.repository.save_goal(goal)
            self._event(goal.goal_id, "goal.resumed", {})

        if goal.status in {GoalStatus.CREATED, GoalStatus.PLANNING}:
            goal = self._plan(goal)
            if goal.status == GoalStatus.FAILED:
                return self._report(goal)

        self._recover_interrupted_tasks(goal.goal_id)
        return self._execute(goal)

    def resume(self, goal_id: str) -> RunReport:
        return self.run(goal_id)

    def resolve_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
    ) -> ApprovalRequest:
        return resolve_approval(
            self.repository,
            approval_id,
            approved=approved,
            decided_by=decided_by,
        )

    def _plan(self, goal: Goal) -> Goal:
        if goal.status == GoalStatus.CREATED:
            goal = transition_goal(goal, GoalStatus.PLANNING)
            self.repository.save_goal(goal)
            self._event(goal.goal_id, "goal.planning", {})
        try:
            tasks = list(self.planner.plan(goal, {"knowledge": []}))
            if not tasks:
                raise ValueError("planner returned no tasks")
            if any(task.goal_id != goal.goal_id for task in tasks):
                raise ValueError("planner returned a task for another goal")
            validate_task_graph(tasks)
        except Exception as error:
            failed = transition_goal(goal, GoalStatus.FAILED, str(error))
            self.repository.save_goal(failed)
            self._event(failed.goal_id, "goal.failed", {"reason": str(error), "phase": "planning"})
            return failed

        self.repository.save_tasks(tasks)
        self._event(goal.goal_id, "goal.planned", {"task_count": len(tasks)})
        running = transition_goal(goal, GoalStatus.RUNNING)
        self.repository.save_goal(running)
        self._event(goal.goal_id, "goal.running", {})
        return running

    def _recover_interrupted_tasks(self, goal_id: str) -> None:
        for task in self.repository.list_tasks(goal_id):
            if task.status == TaskStatus.RUNNING:
                self.repository.save_task(replace(task, status=TaskStatus.PENDING))
                self._event(goal_id, "task.recovered", {"task_id": task.task_id})

    def _execute(self, goal: Goal) -> RunReport:
        actions = self._attempt_count(goal.goal_id)
        while True:
            tasks = self.repository.list_tasks(goal.goal_id)
            if tasks and all(task.status == TaskStatus.SUCCEEDED for task in tasks):
                goal = transition_goal(goal, GoalStatus.SUCCEEDED)
                self.repository.save_goal(goal)
                self._event(goal.goal_id, "goal.succeeded", {})
                return self._report(goal)

            if actions >= self.budget.max_actions:
                reason = f"action budget exhausted at {self.budget.max_actions}"
                goal = transition_goal(goal, GoalStatus.BLOCKED, reason)
                self.repository.save_goal(goal)
                self._event(goal.goal_id, "goal.blocked", {"reason": reason})
                return self._report(goal)

            succeeded = {
                task.task_id for task in tasks if task.status == TaskStatus.SUCCEEDED
            }
            ready = [
                task
                for task in tasks
                if task.status == TaskStatus.PENDING
                and set(task.dependencies).issubset(succeeded)
            ]
            ready.sort(key=lambda task: (task.position, task.task_id))
            if not ready:
                reason = "no task can progress because dependencies are unresolved"
                goal = transition_goal(goal, GoalStatus.BLOCKED, reason)
                self.repository.save_goal(goal)
                self._event(goal.goal_id, "goal.blocked", {"reason": reason})
                return self._report(goal)

            task = ready[0]
            try:
                candidates = self.repository.list_agents()
                deployment = self.repository.get_deployment(task.task_type)
                if deployment is not None:
                    candidates = [
                        agent
                        for agent in candidates
                        if agent.agent_id == deployment.champion_agent_id
                        and agent.model_id == deployment.champion_model_id
                    ]
                    eligible = [
                        agent
                        for agent in candidates
                        if agent.enabled
                        and agent.role == task.assigned_role
                        and (
                            "*" in agent.task_types
                            or task.task_type in agent.task_types
                        )
                        and agent.agent_id in self.workers
                    ]
                    if not eligible:
                        raise LookupError(
                            "deployed champion unavailable for task type "
                            f"{task.task_type}"
                        )
                    candidates = eligible
                else:
                    candidates = [
                        agent
                        for agent in candidates
                        if agent.execution_kind == "local"
                    ]
                decision = self.selector.select(
                    task,
                    candidates,
                    self.repository.list_performance(),
                )
                worker = self.workers[decision.agent_id]
            except (LookupError, KeyError) as error:
                reason = str(error)
                goal = transition_goal(goal, GoalStatus.BLOCKED, reason)
                self.repository.save_goal(goal)
                self._event(goal.goal_id, "goal.blocked", {"reason": reason})
                return self._report(goal)

            task = replace(
                task, status=TaskStatus.RUNNING, assigned_agent_id=decision.agent_id
            )
            self.repository.save_task(task)
            attempt_no = len(self.repository.list_reviews(goal.goal_id, task.task_id)) + 1
            self._event(
                goal.goal_id,
                "task.attempt_started",
                {
                    "task_id": task.task_id,
                    "attempt_no": attempt_no,
                    "agent_id": decision.agent_id,
                    "selection": decision.considered,
                    "deployment_source_run_id": (
                        deployment.source_run_id if deployment is not None else ""
                    ),
                },
            )
            started = perf_counter()
            artifact: Artifact | None = None
            blocked_error: WorkerBlocked | None = None
            try:
                context = self.memory.build_context(goal, task)
                content = worker.execute(task, context)
                artifact = Artifact.create(
                    goal.goal_id, task.task_id, decision.agent_id, content
                )
                self.repository.save_artifact(artifact)
                review = self.reviewer.review(task, content, attempt_no)
            except ApprovalRequired as error:
                self.repository.save_task(replace(task, status=TaskStatus.PENDING))
                reason = f"approval required for tool {error.approval.tool_name}"
                goal = transition_goal(goal, GoalStatus.PAUSED, reason)
                self.repository.save_goal(goal)
                self._event(
                    goal.goal_id,
                    "approval.requested",
                    {
                        "approval_id": error.approval.approval_id,
                        "task_id": task.task_id,
                        "tool_name": error.approval.tool_name,
                    },
                )
                return self._report(goal)
            except WorkerBlocked as error:
                blocked_error = error
                review = Review.create(
                    goal.goal_id,
                    task.task_id,
                    attempt_no,
                    Verdict.FAIL,
                    0,
                    [
                        Defect(
                            "execution",
                            error.reason,
                            "operator review is required before resuming",
                        )
                    ],
                    error.reason,
                )
            except Exception as error:
                review = Review.create(
                    goal.goal_id,
                    task.task_id,
                    attempt_no,
                    Verdict.FAIL,
                    0,
                    [Defect("execution", type(error).__name__, str(error))],
                    f"Execution failed: {error}",
                )
            duration_ms = (perf_counter() - started) * 1000.0
            passed = (
                artifact is not None
                and review.verdict == Verdict.PASS
                and review.score >= self.budget.min_passing_score
            )
            performance = self.memory.calculate_outcome(
                decision.agent_id, task.task_type, passed, review.score, duration_ms
            )
            attempt = Attempt.create(
                goal.goal_id,
                task.task_id,
                decision.agent_id,
                attempt_no,
                duration_ms,
                artifact.artifact_id if artifact is not None else None,
                review.review_id,
                review.summary if artifact is None else "",
            )
            completion_event = Event.create(
                goal.goal_id,
                "task.attempt_completed",
                {
                    "task_id": task.task_id,
                    "attempt_no": attempt_no,
                    "agent_id": decision.agent_id,
                    "passed": passed,
                    "score": review.score,
                    **(blocked_error.evidence if blocked_error is not None else {}),
                },
            )
            self.repository.save_attempt_outcome(
                attempt, review, performance, completion_event
            )
            actions += 1

            if blocked_error is not None:
                task = replace(task, status=TaskStatus.BLOCKED)
                self.repository.save_task(task)
                reason = blocked_error.reason
                goal = transition_goal(goal, GoalStatus.BLOCKED, reason)
                self.repository.save_goal(goal)
                self._event(
                    goal.goal_id,
                    "goal.blocked",
                    {
                        "reason": reason,
                        "task_id": task.task_id,
                        "agent_id": decision.agent_id,
                        **blocked_error.evidence,
                    },
                )
                return self._report(goal)

            if passed:
                task = replace(
                    task,
                    status=TaskStatus.SUCCEEDED,
                    artifact_id=artifact.artifact_id,
                )
                self.repository.save_task(task)
                self._event(
                    goal.goal_id,
                    "task.succeeded",
                    {
                        "task_id": task.task_id,
                        "attempt_no": attempt_no,
                        "score": review.score,
                    },
                )
                continue

            self._event(
                goal.goal_id,
                "task.review_failed",
                {
                    "task_id": task.task_id,
                    "attempt_no": attempt_no,
                    "score": review.score,
                    "summary": review.summary,
                },
            )
            if attempt_no >= task.max_attempts:
                task = replace(task, status=TaskStatus.FAILED)
                self.repository.save_task(task)
                reason = (
                    f"task {task.task_id} exhausted {task.max_attempts} attempts"
                )
                goal = transition_goal(goal, GoalStatus.FAILED, reason)
                self.repository.save_goal(goal)
                self._event(goal.goal_id, "goal.failed", {"reason": reason})
                return self._report(goal)

            self.repository.save_task(replace(task, status=TaskStatus.PENDING))
            self._event(
                goal.goal_id,
                "task.retry_scheduled",
                {"task_id": task.task_id, "next_attempt": attempt_no + 1},
            )

    def _attempt_count(self, goal_id: str) -> int:
        return sum(
            event.event_type == "task.attempt_completed"
            for event in self.repository.list_events(goal_id)
        )

    def _report(self, goal: Goal) -> RunReport:
        tasks = self.repository.list_tasks(goal.goal_id)
        review_counts: dict[str, int] = {}
        for review in self.repository.list_reviews(goal.goal_id):
            review_counts[review.task_id] = review_counts.get(review.task_id, 0) + 1
        attempts = sum(review_counts.values())
        return RunReport(
            goal_id=goal.goal_id,
            status=goal.status,
            tasks_total=len(tasks),
            tasks_succeeded=sum(task.status == TaskStatus.SUCCEEDED for task in tasks),
            actions=self._attempt_count(goal.goal_id),
            attempts=attempts,
            retries=sum(max(0, count - 1) for count in review_counts.values()),
            artifacts=len(self.repository.list_artifacts(goal.goal_id)),
            reason=goal.failure_reason,
        )

    def _event(self, goal_id: str, event_type: str, payload: dict[str, object]) -> None:
        self.repository.append_event(Event.create(goal_id, event_type, payload))
        kind = "approval" if event_type.startswith("approval.") else "lifecycle"
        with self.tracer.span(
            goal_id,
            event_type,
            kind=kind,
            task_id=str(payload["task_id"]) if "task_id" in payload else None,
            agent_id=str(payload["agent_id"]) if "agent_id" in payload else None,
            attributes=dict(payload),
        ):
            pass


def resolve_approval(
    repository: SQLiteRepository,
    approval_id: str,
    *,
    approved: bool,
    decided_by: str,
) -> ApprovalRequest:
    approval = repository.get_approval(approval_id)
    if approval is None:
        raise KeyError(f"approval not found: {approval_id}")
    goal = repository.get_goal(approval.goal_id)
    if goal is None:
        raise KeyError(f"goal not found: {approval.goal_id}")
    if goal.status != GoalStatus.PAUSED:
        raise ValueError(f"goal is not paused: {goal.goal_id}")
    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    resolved = approval.resolve(status, decided_by)
    event_payload = {
        "approval_id": resolved.approval_id,
        "task_id": resolved.task_id,
        "tool_name": resolved.tool_name,
        "decided_by": resolved.decided_by,
    }
    events = [
        Event.create(
            goal.goal_id,
            f"approval.{status.value}",
            event_payload,
        )
    ]
    failed = None
    if status == ApprovalStatus.REJECTED:
        reason = f"approval rejected for tool {resolved.tool_name}"
        failed = transition_goal(goal, GoalStatus.FAILED, reason)
        events.append(Event.create(failed.goal_id, "goal.failed", {"reason": reason}))
    repository.save_approval_resolution(resolved, events, failed)
    with TraceRecorder(repository).span(
        goal.goal_id,
        f"approval.{status.value}",
        kind="approval",
        task_id=resolved.task_id,
        attributes=event_payload,
    ):
        pass
    if failed is not None:
        with TraceRecorder(repository).span(
            failed.goal_id,
            "goal.failed",
            kind="lifecycle",
            attributes={"reason": failed.failure_reason},
        ):
            pass
    return resolved
