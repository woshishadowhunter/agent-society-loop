"""Strict, bounded A2A 1.0 HTTP+JSON transport and registration."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from contextlib import nullcontext
from typing import Any, Mapping
from uuid import uuid4

from .domain import (
    AgentProfile,
    BenchmarkCase,
    CandidateExecution,
    CandidateIdentity,
    DelegationRecord,
    DelegationStatus,
    RemoteAgentRegistration,
    Task,
)
from .ports import WorkerBlocked


class A2AError(RuntimeError):
    """Base class for sanitized A2A failures."""


class A2AHTTPError(A2AError):
    """A definite HTTP transport failure."""


class A2AProtocolError(A2AError):
    """A definite A2A payload or protocol failure."""


class A2AAmbiguousSubmission(A2AError):
    """The server may have accepted a message, so it must not be resent."""


@dataclass(frozen=True, slots=True)
class A2ALimits:
    request_timeout: float = 10.0
    total_timeout: float = 60.0
    max_request_bytes: int = 131_072
    max_response_bytes: int = 1_048_576
    max_result_bytes: int = 262_144
    max_polls: int = 20
    poll_interval: float = 0.25

    def __post_init__(self) -> None:
        values = (
            self.request_timeout,
            self.total_timeout,
            self.max_request_bytes,
            self.max_response_bytes,
            self.max_result_bytes,
            self.max_polls,
        )
        if any(value <= 0 for value in values) or self.poll_interval < 0:
            raise ValueError("A2A limits must be positive")


@dataclass(frozen=True, slots=True)
class AgentCardInspection:
    card_url: str
    sha256: str
    byte_count: int
    card: dict[str, Any]


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class A2AHTTPClient:
    def __init__(
        self,
        *,
        auth_token: str = "",
        limits: A2ALimits | None = None,
        allow_insecure_localhost: bool = False,
    ) -> None:
        self._auth_token = auth_token
        self.limits = limits or A2ALimits()
        self.allow_insecure_localhost = bool(allow_insecure_localhost)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirects()
        )

    def inspect_card(self, card_url: str) -> AgentCardInspection:
        body, headers = self._request("GET", card_url, None, ambiguous=False)
        self._require_json_media_type(headers)
        card = self._parse_object(body, ambiguous=False)
        return AgentCardInspection(
            card_url=card_url,
            sha256=hashlib.sha256(body).hexdigest(),
            byte_count=len(body),
            card=card,
        )

    def send_message(
        self,
        interface_url: str,
        payload: Mapping[str, Any],
        *,
        tenant: str = "",
    ) -> dict[str, Any]:
        request_payload = dict(payload)
        if tenant:
            request_payload["tenant"] = tenant
        return self._json_request(
            "POST",
            f"{interface_url.rstrip('/')}/message:send",
            request_payload,
            ambiguous=True,
        )

    def get_task(
        self, interface_url: str, task_id: str, *, tenant: str = ""
    ) -> dict[str, Any]:
        encoded = urllib.parse.quote(task_id, safe="")
        url = f"{interface_url.rstrip('/')}/tasks/{encoded}"
        if tenant:
            url = f"{url}?{urllib.parse.urlencode({'tenant': tenant})}"
        return self._json_request("GET", url, None, ambiguous=False)

    def cancel_task(
        self, interface_url: str, task_id: str, *, tenant: str = ""
    ) -> dict[str, Any]:
        encoded = urllib.parse.quote(task_id, safe="")
        payload = {"tenant": tenant} if tenant else {}
        return self._json_request(
            "POST",
            f"{interface_url.rstrip('/')}/tasks/{encoded}:cancel",
            payload,
            ambiguous=False,
        )

    def _json_request(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        *,
        ambiguous: bool,
    ) -> dict[str, Any]:
        encoded = None
        if payload is not None:
            encoded = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        body, headers = self._request(method, url, encoded, ambiguous=ambiguous)
        self._require_json_media_type(headers, ambiguous=ambiguous)
        return self._parse_object(body, ambiguous=ambiguous)

    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None,
        *,
        ambiguous: bool,
    ) -> tuple[bytes, Any]:
        self._validate_url(url)
        if body is not None and len(body) > self.limits.max_request_bytes:
            raise A2AProtocolError("A2A request exceeds size limit")
        headers = {
            "A2A-Version": "1.0",
            "Accept": "application/a2a+json, application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/a2a+json"
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.limits.request_timeout) as response:
                try:
                    data = self._bounded_read(response)
                except A2AProtocolError:
                    if ambiguous:
                        raise A2AAmbiguousSubmission(
                            "A2A message submission outcome is ambiguous"
                        ) from None
                    raise
                return data, response.headers
        except urllib.error.HTTPError as error:
            error.close()
            if 300 <= error.code < 400:
                raise A2AHTTPError("A2A redirect rejected") from None
            if ambiguous and error.code >= 500:
                raise A2AAmbiguousSubmission(
                    "A2A message submission outcome is ambiguous"
                ) from None
            raise A2AHTTPError(f"A2A HTTP request failed with status {error.code}") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            if ambiguous:
                raise A2AAmbiguousSubmission(
                    "A2A message submission outcome is ambiguous"
                ) from None
            raise A2AHTTPError("A2A HTTP transport failed") from None

    def _bounded_read(self, response: Any) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > self.limits.max_response_bytes:
                    raise A2AProtocolError("A2A response exceeds size limit")
            except ValueError:
                raise A2AProtocolError("A2A response has invalid content length") from None
        body = response.read(self.limits.max_response_bytes + 1)
        if len(body) > self.limits.max_response_bytes:
            raise A2AProtocolError("A2A response exceeds size limit")
        return body

    @staticmethod
    def _require_json_media_type(headers: Any, *, ambiguous: bool = False) -> None:
        media_type = headers.get_content_type().casefold()
        if media_type not in {"application/json", "application/a2a+json"}:
            error = "A2A response has invalid content type"
            if ambiguous:
                raise A2AAmbiguousSubmission(
                    "A2A message submission outcome is ambiguous"
                )
            raise A2AProtocolError(error)

    @staticmethod
    def _parse_object(body: bytes, *, ambiguous: bool) -> dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if ambiguous:
                raise A2AAmbiguousSubmission(
                    "A2A message submission outcome is ambiguous"
                ) from None
            raise A2AProtocolError("A2A response is not valid JSON") from None
        if not isinstance(value, dict):
            if ambiguous:
                raise A2AAmbiguousSubmission(
                    "A2A message submission outcome is ambiguous"
                )
            raise A2AProtocolError("A2A JSON response must be an object")
        return value

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("A2A URL must not contain credentials")
        if parsed.fragment:
            raise ValueError("A2A URL must not contain a fragment")
        if not parsed.hostname:
            raise ValueError("A2A URL must have a host")
        if parsed.scheme == "https":
            return
        loopback = parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "http" or not self.allow_insecure_localhost or not loopback:
            raise ValueError("A2A URLs require HTTPS except explicit loopback development")


def inspect_agent_card(
    card_url: str, *, client: A2AHTTPClient | None = None
) -> AgentCardInspection:
    return (client or A2AHTTPClient()).inspect_card(card_url)


def register_remote_agent(
    repository: Any,
    client: A2AHTTPClient,
    agent_id: str,
    card_url: str,
    expected_sha256: str,
    interface_url: str,
    skill_by_task_type: Mapping[str, str],
    *,
    auth_env: str = "",
    allowed_context_sections: tuple[str, ...] = ("review_feedback",),
    allow_insecure_localhost: bool = False,
) -> RemoteAgentRegistration:
    inspection = client.inspect_card(card_url)
    expected = expected_sha256.casefold()
    if inspection.sha256 != expected:
        raise ValueError("Agent Card digest does not match the operator pin")

    interfaces = inspection.card.get("supportedInterfaces")
    if not isinstance(interfaces, list):
        raise ValueError("Agent Card interface declaration is missing")
    selected = next(
        (
            item
            for item in interfaces
            if isinstance(item, dict)
            and item.get("url") == interface_url
            and item.get("protocolBinding") == "HTTP+JSON"
            and item.get("protocolVersion") == "1.0"
        ),
        None,
    )
    if selected is None:
        raise ValueError("Agent Card has no exact A2A 1.0 HTTP+JSON interface match")

    skills = inspection.card.get("skills")
    available_skill_ids = {
        item.get("id")
        for item in skills or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    requested_skill_ids = {str(value) for value in skill_by_task_type.values()}
    if not requested_skill_ids or not requested_skill_ids.issubset(available_skill_ids):
        raise ValueError("Agent Card does not declare every pinned skill")

    registration = RemoteAgentRegistration.create(
        agent_id,
        card_url,
        expected,
        interface_url,
        dict(skill_by_task_type),
        tenant=str(selected.get("tenant", "")),
        auth_env=auth_env,
        allowed_context_sections=allowed_context_sections,
        allow_insecure_localhost=allow_insecure_localhost,
    )
    profile = AgentProfile(
        agent_id=registration.agent_id,
        role="worker",
        model_id=registration.model_id,
        task_types=registration.task_types,
        execution_kind="a2a",
    )
    repository.save_remote_agent_profile(registration, profile)
    return registration


@dataclass(frozen=True, slots=True)
class RemoteExecution:
    content: str
    delegation_id: str
    card_sha256: str
    remote_task_id: str = ""
    duration_ms: float = 0.0


_ACTIVE_TASK_STATES = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
_INTERRUPTED_TASK_STATES = {
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
}
_TERMINAL_STATUS_BY_TASK_STATE = {
    "TASK_STATE_FAILED": DelegationStatus.FAILED,
    "TASK_STATE_CANCELED": DelegationStatus.CANCELED,
    "TASK_STATE_REJECTED": DelegationStatus.REJECTED,
}
_SENSITIVE_PARTS = ("key", "token", "secret", "authorization", "password")


def _redact_outbound(value: Any, key: str = "") -> Any:
    if key and any(part in key.casefold() for part in _SENSITIVE_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(name): _redact_outbound(item, str(name))
            for name, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_outbound(item) for item in value]
    return value


class A2ARemoteExecutor:
    def __init__(
        self,
        repository: Any,
        registration: RemoteAgentRegistration,
        client: A2AHTTPClient,
        *,
        tracer: Any | None = None,
        limits: A2ALimits | None = None,
        clock: Any = time.monotonic,
        sleep: Any = time.sleep,
    ) -> None:
        self.repository = repository
        self.registration = registration
        self.client = client
        self.tracer = tracer
        self.limits = limits or client.limits
        self._clock = clock
        self._sleep = sleep

    def delegate(
        self,
        task: Task,
        context: dict[str, Any],
        *,
        attempt_no: int | None = None,
    ) -> RemoteExecution:
        started = self._clock()
        number = attempt_no or len(
            self.repository.list_reviews(task.goal_id, task.task_id)
        ) + 1
        existing = self.repository.get_attempt_delegation(
            task.goal_id,
            task.task_id,
            number,
            self.registration.agent_id,
        )
        if existing is not None:
            return self._resume(existing, started)

        payload = self._build_payload(task, context)
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > self.limits.max_request_bytes:
            raise A2AProtocolError("A2A delegation payload exceeds size limit")
        delegation = DelegationRecord.create(
            task.goal_id,
            task.task_id,
            number,
            self.registration,
            payload["message"]["messageId"],
            hashlib.sha256(encoded).hexdigest(),
        )
        self.repository.save_delegation(delegation)
        delegation = delegation.advance(DelegationStatus.SUBMITTING)
        self.repository.save_delegation(delegation)

        try:
            with self._span(delegation, "a2a.send", operation="send"):
                response = self.client.send_message(
                    self.registration.interface_url,
                    payload,
                    tenant=self.registration.tenant,
                )
        except A2AAmbiguousSubmission:
            unknown = delegation.advance(
                DelegationStatus.UNKNOWN, error_category="ambiguous_send"
            )
            self.repository.save_delegation(unknown)
            raise
        except A2AHTTPError:
            failed = delegation.advance(
                DelegationStatus.FAILED, error_category="send_rejected"
            )
            self.repository.save_delegation(failed)
            raise

        delegation = self._apply_response(delegation, response)
        if delegation.status == DelegationStatus.COMPLETED:
            return self._execution(delegation, started)
        if delegation.status != DelegationStatus.ACCEPTED:
            self._raise_terminal(delegation)
        return self._poll(delegation, started)

    def cancel(self, delegation_id: str, decided_by: str) -> DelegationRecord:
        if not decided_by.strip():
            raise ValueError("cancellation identity must not be empty")
        delegation = self.repository.get_delegation(delegation_id)
        if delegation is None:
            raise KeyError(f"delegation not found: {delegation_id}")
        if delegation.status == DelegationStatus.COMPLETED:
            return delegation
        if delegation.status not in {
            DelegationStatus.ACCEPTED,
            DelegationStatus.INTERRUPTED,
        } or not delegation.remote_task_id:
            raise A2AProtocolError("delegation has no cancelable known remote task")
        return self._cancel_known_task(delegation, decided_by.strip())

    def _resume(self, delegation: DelegationRecord, started: float) -> RemoteExecution:
        if delegation.model_id != self.registration.model_id:
            raise A2AProtocolError("remote registration identity changed")
        if delegation.status == DelegationStatus.COMPLETED:
            return self._execution(delegation, started)
        if delegation.status == DelegationStatus.PREPARED:
            raise A2AProtocolError("prepared delegation cannot be reconstructed safely")
        if delegation.status == DelegationStatus.SUBMITTING:
            unknown = delegation.advance(
                DelegationStatus.UNKNOWN, error_category="interrupted_send"
            )
            self.repository.save_delegation(unknown)
            raise A2AProtocolError("delegation submission outcome is unknown")
        if delegation.status == DelegationStatus.UNKNOWN:
            raise A2AProtocolError("delegation submission outcome is unknown")
        if delegation.status == DelegationStatus.ACCEPTED:
            return self._poll(delegation, started)
        self._raise_terminal(delegation)
        raise AssertionError("unreachable")

    def _poll(self, delegation: DelegationRecord, started: float) -> RemoteExecution:
        while (
            delegation.poll_count < self.limits.max_polls
            and self._clock() - started < self.limits.total_timeout
        ):
            if self.limits.poll_interval:
                self._sleep(self.limits.poll_interval)
            delegation = delegation.advance(
                DelegationStatus.ACCEPTED,
                remote_task_id=delegation.remote_task_id,
                increment_poll=True,
            )
            self.repository.save_delegation(delegation)
            try:
                with self._span(
                    delegation,
                    "a2a.get",
                    operation="get",
                    poll_count=delegation.poll_count,
                ):
                    response = self.client.get_task(
                        self.registration.interface_url,
                        delegation.remote_task_id,
                        tenant=self.registration.tenant,
                    )
            except A2AHTTPError:
                continue
            delegation = self._apply_response(delegation, response)
            if delegation.status == DelegationStatus.COMPLETED:
                return self._execution(delegation, started)
            if delegation.status != DelegationStatus.ACCEPTED:
                self._raise_terminal(delegation)

        self._cancel_known_task(delegation, "deadline")
        raise A2AProtocolError("A2A delegation deadline or poll budget exhausted")

    def _cancel_known_task(
        self, delegation: DelegationRecord, decided_by: str
    ) -> DelegationRecord:
        try:
            with self._span(delegation, "a2a.cancel", operation="cancel"):
                response = self.client.cancel_task(
                    self.registration.interface_url,
                    delegation.remote_task_id,
                    tenant=self.registration.tenant,
                )
            canceled = self._apply_response(
                delegation, response, canceled_by=decided_by
            )
        except A2AError:
            if delegation.status == DelegationStatus.ACCEPTED:
                failed = delegation.advance(
                    DelegationStatus.FAILED,
                    error_category="cancel_failed",
                )
                self.repository.save_delegation(failed)
            raise
        if canceled.status not in {
            DelegationStatus.CANCELED,
            DelegationStatus.COMPLETED,
        }:
            failed = canceled.advance(
                DelegationStatus.FAILED,
                error_category="cancel_not_terminal",
            )
            self.repository.save_delegation(failed)
            raise A2AProtocolError("remote cancellation did not reach a terminal state")
        return canceled

    def _apply_response(
        self,
        delegation: DelegationRecord,
        response: dict[str, Any],
        *,
        canceled_by: str = "",
    ) -> DelegationRecord:
        try:
            if isinstance(response.get("message"), dict):
                content = self._normalize_parts(response["message"].get("parts"))
                updated = delegation.advance(
                    DelegationStatus.COMPLETED, result_content=content
                )
                self.repository.save_delegation(updated)
                return updated
            task = response.get("task")
            if not isinstance(task, dict):
                raise A2AProtocolError("A2A response has neither message nor task")
            remote_task_id = task.get("id")
            if not isinstance(remote_task_id, str) or not remote_task_id.strip():
                raise A2AProtocolError("A2A task identity is missing")
            if delegation.remote_task_id and remote_task_id != delegation.remote_task_id:
                raise A2AProtocolError("A2A task identity does not match delegation")
            status = task.get("status")
            state = status.get("state") if isinstance(status, dict) else None
            if not isinstance(state, str):
                raise A2AProtocolError("A2A task state is missing")
            if state in _ACTIVE_TASK_STATES:
                updated = delegation.advance(
                    DelegationStatus.ACCEPTED,
                    remote_task_id=remote_task_id,
                    remote_task_state=state,
                )
            elif state == "TASK_STATE_COMPLETED":
                artifacts = task.get("artifacts")
                if not isinstance(artifacts, list) or not artifacts:
                    raise A2AProtocolError("completed A2A task has no artifacts")
                parts = []
                for artifact in artifacts:
                    if not isinstance(artifact, dict):
                        raise A2AProtocolError("A2A artifact is invalid")
                    artifact_parts = artifact.get("parts")
                    if not isinstance(artifact_parts, list):
                        raise A2AProtocolError("A2A artifact parts are invalid")
                    parts.extend(artifact_parts)
                content = self._normalize_parts(parts)
                updated = delegation.advance(
                    DelegationStatus.COMPLETED,
                    remote_task_id=remote_task_id,
                    remote_task_state=state,
                    result_content=content,
                )
            elif state in _INTERRUPTED_TASK_STATES:
                updated = delegation.advance(
                    DelegationStatus.INTERRUPTED,
                    remote_task_id=remote_task_id,
                    remote_task_state=state,
                    error_category="remote_interrupted",
                )
            elif state in _TERMINAL_STATUS_BY_TASK_STATE:
                terminal = _TERMINAL_STATUS_BY_TASK_STATE[state]
                updated = delegation.advance(
                    terminal,
                    remote_task_id=remote_task_id,
                    remote_task_state=state,
                    error_category=f"remote_{terminal.value}",
                    canceled_by=canceled_by if terminal == DelegationStatus.CANCELED else "",
                )
            else:
                raise A2AProtocolError("A2A task state is unsupported")
            self.repository.save_delegation(updated)
            return updated
        except A2AProtocolError:
            current = self.repository.get_delegation(delegation.delegation_id) or delegation
            if current.status in {
                DelegationStatus.PREPARED,
                DelegationStatus.SUBMITTING,
                DelegationStatus.ACCEPTED,
            }:
                failed = current.advance(
                    DelegationStatus.FAILED, error_category="invalid_remote_response"
                )
                self.repository.save_delegation(failed)
            raise

    def _normalize_parts(self, parts: Any) -> str:
        if not isinstance(parts, list) or not parts:
            raise A2AProtocolError("A2A result parts must not be empty")
        normalized: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                raise A2AProtocolError("A2A result part is invalid")
            if "text" in part and isinstance(part["text"], str):
                if part["text"].strip():
                    normalized.append(part["text"].strip())
                continue
            if "data" in part:
                normalized.append(
                    json.dumps(
                        part["data"],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                continue
            raise A2AProtocolError("A2A result part type is unsupported")
        content = "\n".join(item for item in normalized if item).strip()
        if not content:
            raise A2AProtocolError("A2A result is empty")
        if len(content.encode("utf-8")) > self.limits.max_result_bytes:
            raise A2AProtocolError("A2A result size exceeds limit")
        return content

    def _build_payload(self, task: Task, context: dict[str, Any]) -> dict[str, Any]:
        skill_id = self.registration.skill_by_task_type.get(task.task_type)
        if not skill_id:
            raise A2AProtocolError("remote agent has no pinned skill for task type")
        allowed_context: dict[str, Any] = {}
        for section in self.registration.allowed_context_sections:
            if section == "goal":
                allowed_context[section] = {"goal_id": task.goal_id}
            elif section == "task_context":
                allowed_context[section] = task.context
            elif section in context:
                allowed_context[section] = context[section]
        data = {
            "skillId": skill_id,
            "task": {
                "description": task.description,
                "acceptanceCriteria": task.acceptance_criteria,
            },
            "context": _redact_outbound(allowed_context),
        }
        return {
            "message": {
                "messageId": f"message-{uuid4().hex}",
                "role": "ROLE_USER",
                "parts": [{"data": data}],
            },
            "returnImmediately": True,
        }

    def _execution(self, delegation: DelegationRecord, started: float) -> RemoteExecution:
        return RemoteExecution(
            content=delegation.result_content,
            delegation_id=delegation.delegation_id,
            card_sha256=delegation.card_sha256,
            remote_task_id=delegation.remote_task_id,
            duration_ms=max(0.0, (self._clock() - started) * 1000.0),
        )

    def _raise_terminal(self, delegation: DelegationRecord) -> None:
        if delegation.status == DelegationStatus.INTERRUPTED:
            raise A2AProtocolError("remote delegation was interrupted")
        raise A2AProtocolError(
            f"remote delegation ended in {delegation.status.value} state"
        )

    def _span(
        self,
        delegation: DelegationRecord,
        name: str,
        **attributes: Any,
    ) -> Any:
        if self.tracer is None:
            return nullcontext(None)
        return self.tracer.span(
            delegation.goal_id,
            name,
            kind="a2a",
            task_id=delegation.task_id,
            agent_id=delegation.agent_id,
            attributes={
                "delegation_id": delegation.delegation_id,
                "card_sha256": delegation.card_sha256,
                **attributes,
            },
        )


class A2ARemoteWorker:
    def __init__(self, executor: A2ARemoteExecutor) -> None:
        self.executor = executor
        self.agent_id = executor.registration.agent_id

    def execute(self, task: Task, context: dict[str, Any]) -> str:
        attempt_no = len(
            self.executor.repository.list_reviews(task.goal_id, task.task_id)
        ) + 1
        try:
            return self.executor.delegate(
                task, context, attempt_no=attempt_no
            ).content
        except (A2AAmbiguousSubmission, A2AProtocolError):
            delegation = self.executor.repository.get_attempt_delegation(
                task.goal_id, task.task_id, attempt_no, self.agent_id
            )
            evidence = {"card_sha256": self.executor.registration.card_sha256}
            if delegation is not None:
                evidence.update(
                    {
                        "delegation_id": delegation.delegation_id,
                        "remote_task_id": delegation.remote_task_id,
                        "status": delegation.status.value,
                        "error_category": delegation.error_category,
                    }
                )
            raise WorkerBlocked(
                "remote delegation requires operator review", evidence
            ) from None


class A2ABenchmarkRunner:
    def __init__(
        self,
        executors: Mapping[str, A2ARemoteExecutor],
        *,
        benchmark_id: str | None = None,
    ) -> None:
        self.executors = dict(executors)
        identifier = benchmark_id or uuid4().hex[:16]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", identifier):
            raise ValueError("benchmark ID must be a safe short identifier")
        self.benchmark_id = identifier

    def __call__(
        self, candidate: CandidateIdentity, case: BenchmarkCase
    ) -> CandidateExecution:
        executor = self.executors.get(candidate.agent_id)
        if executor is None:
            raise LookupError(f"no A2A executor for candidate {candidate.agent_id}")
        if candidate.model_id != executor.registration.model_id:
            raise ValueError("benchmark candidate identity does not match pinned card")
        prompt = case.input.get("prompt")
        description = (
            prompt.strip()
            if isinstance(prompt, str) and prompt.strip()
            else json.dumps(
                case.input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        task = Task.create(
            f"benchmark-{self.benchmark_id}",
            f"{case.case_id}:{candidate.agent_id}",
            case.task_type,
            description,
            acceptance_criteria=case.acceptance_criteria,
            context=case.context,
            max_attempts=1,
        )
        execution = executor.delegate(task, dict(case.context), attempt_no=1)
        return CandidateExecution(
            output=execution.content,
            duration_ms=execution.duration_ms,
            evidence={
                "delegation_id": execution.delegation_id,
                "card_sha256": execution.card_sha256,
                "remote_task_id": execution.remote_task_id,
            },
        )
