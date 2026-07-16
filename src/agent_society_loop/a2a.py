"""Strict, bounded A2A 1.0 HTTP+JSON transport and registration."""

from __future__ import annotations

import hashlib
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .domain import AgentProfile, RemoteAgentRegistration


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
                data = self._bounded_read(response)
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
