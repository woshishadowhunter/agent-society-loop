"""Deterministic local fault campaign for A2A executor safety invariants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .a2a import (
    A2AAmbiguousSubmission,
    A2ALimits,
    A2AProtocolError,
    A2ARemoteExecutor,
)
from .domain import DelegationStatus, RemoteAgentRegistration, Task, utc_now
from .storage import SQLiteRepository


@dataclass(frozen=True, slots=True)
class ReliabilityCheck:
    check_id: str
    passed: bool
    summary: str
    evidence: dict[str, str | int | float | bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "summary": self.summary,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class ReliabilityReport:
    checks: tuple[ReliabilityCheck, ...]
    created_at: str = field(default_factory=utc_now)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "created_at": self.created_at,
        }


class _ScriptedClient:
    def __init__(
        self,
        *,
        sends: list[Any],
        gets: list[Any] | None = None,
        cancels: list[Any] | None = None,
        limits: A2ALimits | None = None,
    ) -> None:
        self.limits = limits or A2ALimits(poll_interval=0)
        self._sends = list(sends)
        self._gets = list(gets or [])
        self._cancels = list(cancels or [])
        self.send_count = 0
        self.get_count = 0
        self.cancel_count = 0

    def send_message(self, interface_url: str, payload: Any, *, tenant: str = ""):
        self.send_count += 1
        return self._take(self._sends)

    def get_task(self, interface_url: str, task_id: str, *, tenant: str = ""):
        self.get_count += 1
        return self._take(self._gets)

    def cancel_task(self, interface_url: str, task_id: str, *, tenant: str = ""):
        self.cancel_count += 1
        return self._take(self._cancels)

    @staticmethod
    def _take(outcomes: list[Any]) -> Any:
        if not outcomes:
            raise A2AProtocolError("scripted A2A outcome is missing")
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run_reliability_campaign(
    *, clients: Mapping[str, Any] | None = None
) -> ReliabilityReport:
    """Run five socket-free scenarios against the real durable executor."""

    supplied = dict(clients or {})
    scenarios = (
        ("ambiguous_submission", _ambiguous_submission),
        ("task_identity_drift", _task_identity_drift),
        ("remote_interruption", _remote_interruption),
        ("unsupported_output", _unsupported_output),
        ("poll_exhaustion", _poll_exhaustion),
    )
    checks: list[ReliabilityCheck] = []
    for check_id, scenario in scenarios:
        try:
            checks.append(scenario(supplied.get(check_id)))
        except Exception as error:
            checks.append(
                ReliabilityCheck(
                    check_id,
                    False,
                    f"{type(error).__name__}: reliability scenario failed safely",
                )
            )
    return ReliabilityReport(tuple(checks))


def _registration() -> RemoteAgentRegistration:
    return RemoteAgentRegistration.create(
        "reliability-agent",
        "https://reliability.invalid/.well-known/agent-card.json",
        "a" * 64,
        "https://reliability.invalid/a2a",
        {"analysis": "analyze"},
        allowed_context_sections=(),
    )


def _task(task_id: str) -> Task:
    return Task.create(
        "reliability-campaign",
        task_id,
        "analysis",
        "Exercise a deterministic A2A failure invariant",
        max_attempts=1,
    )


def _executor(repository: SQLiteRepository, client: Any) -> A2ARemoteExecutor:
    registration = repository.get_remote_agent("reliability-agent")
    if registration is None:
        registration = _registration()
        repository.save_remote_agent(registration)
    return A2ARemoteExecutor(
        repository,
        registration,
        client,
        limits=client.limits,
    )


def _task_response(state: str, *, task_id: str = "remote-task", parts=None):
    task: dict[str, Any] = {"id": task_id, "status": {"state": state}}
    if parts is not None:
        task["artifacts"] = [{"parts": parts}]
    return {"task": task}


def _ambiguous_submission(client: Any | None) -> ReliabilityCheck:
    repository = SQLiteRepository(":memory:")
    try:
        active_client = client or _ScriptedClient(
            sends=[A2AAmbiguousSubmission("ambiguous")]
        )
        executor = _executor(repository, active_client)
        first_raised = False
        second_raised = False
        try:
            executor.delegate(_task("ambiguous"), {}, attempt_no=1)
        except A2AAmbiguousSubmission:
            first_raised = True
        try:
            executor.delegate(_task("ambiguous"), {}, attempt_no=1)
        except A2AProtocolError:
            second_raised = True
        delegation = repository.list_delegations()[0]
        passed = (
            first_raised
            and second_raised
            and active_client.send_count == 1
            and delegation.status == DelegationStatus.UNKNOWN
        )
        return ReliabilityCheck(
            "ambiguous_submission",
            passed,
            "ambiguous submission is terminal and never resent",
            {
                "send_count": active_client.send_count,
                "status": delegation.status.value,
            },
        )
    finally:
        repository.close()


def _task_identity_drift(client: Any | None) -> ReliabilityCheck:
    repository = SQLiteRepository(":memory:")
    try:
        active_client = client or _ScriptedClient(
            sends=[_task_response("TASK_STATE_SUBMITTED", task_id="remote-a")],
            gets=[
                _task_response(
                    "TASK_STATE_COMPLETED",
                    task_id="remote-b",
                    parts=[{"text": "wrong identity"}],
                )
            ],
        )
        raised = False
        try:
            _executor(repository, active_client).delegate(
                _task("identity-drift"), {}, attempt_no=1
            )
        except A2AProtocolError:
            raised = True
        delegation = repository.list_delegations()[0]
        passed = raised and delegation.status == DelegationStatus.FAILED
        return ReliabilityCheck(
            "task_identity_drift",
            passed,
            "remote task identity drift fails closed",
            {
                "send_count": active_client.send_count,
                "get_count": active_client.get_count,
                "status": delegation.status.value,
            },
        )
    finally:
        repository.close()


def _remote_interruption(client: Any | None) -> ReliabilityCheck:
    repository = SQLiteRepository(":memory:")
    try:
        active_client = client or _ScriptedClient(
            sends=[
                _task_response("TASK_STATE_AUTH_REQUIRED"),
                _task_response("TASK_STATE_INPUT_REQUIRED"),
            ]
        )
        interrupted = 0
        for task_id in ("auth-interruption", "input-interruption"):
            try:
                _executor(repository, active_client).delegate(
                    _task(task_id), {}, attempt_no=1
                )
            except A2AProtocolError:
                interrupted += 1
        delegations = repository.list_delegations()
        passed = interrupted == 2 and all(
            item.status == DelegationStatus.INTERRUPTED for item in delegations
        )
        return ReliabilityCheck(
            "remote_interruption",
            passed,
            "input or authentication interruption requires operator action",
            {
                "send_count": active_client.send_count,
                "interruption_count": interrupted,
                "status": (
                    "interrupted"
                    if all(
                        item.status == DelegationStatus.INTERRUPTED
                        for item in delegations
                    )
                    else "unexpected"
                ),
            },
        )
    finally:
        repository.close()


def _unsupported_output(client: Any | None) -> ReliabilityCheck:
    repository = SQLiteRepository(":memory:")
    try:
        active_client = client or _ScriptedClient(
            sends=[
                {"message": {"parts": [{"url": "https://invalid.example"}]}},
                {"message": {"parts": [{"raw": "opaque"}]}},
            ]
        )
        rejected = 0
        for task_id in ("url-output", "raw-output"):
            try:
                _executor(repository, active_client).delegate(
                    _task(task_id), {}, attempt_no=1
                )
            except A2AProtocolError:
                rejected += 1
        delegations = repository.list_delegations()
        passed = rejected == 2 and all(
            item.status == DelegationStatus.FAILED for item in delegations
        )
        return ReliabilityCheck(
            "unsupported_output",
            passed,
            "URL and opaque output parts are rejected",
            {
                "send_count": active_client.send_count,
                "rejected_count": rejected,
            },
        )
    finally:
        repository.close()


def _poll_exhaustion(client: Any | None) -> ReliabilityCheck:
    repository = SQLiteRepository(":memory:")
    try:
        limits = A2ALimits(max_polls=1, poll_interval=0)
        active_client = client or _ScriptedClient(
            sends=[_task_response("TASK_STATE_SUBMITTED")],
            gets=[_task_response("TASK_STATE_WORKING")],
            cancels=[_task_response("TASK_STATE_CANCELED")],
            limits=limits,
        )
        raised = False
        try:
            _executor(repository, active_client).delegate(
                _task("poll-exhaustion"), {}, attempt_no=1
            )
        except A2AProtocolError:
            raised = True
        delegation = repository.list_delegations()[0]
        passed = (
            raised
            and active_client.cancel_count == 1
            and delegation.status == DelegationStatus.CANCELED
        )
        return ReliabilityCheck(
            "poll_exhaustion",
            passed,
            "poll exhaustion triggers exactly one terminal cancellation",
            {
                "send_count": active_client.send_count,
                "get_count": active_client.get_count,
                "cancel_count": active_client.cancel_count,
                "status": delegation.status.value,
            },
        )
    finally:
        repository.close()
