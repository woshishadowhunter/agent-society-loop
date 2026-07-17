"""Lease-owned delivery for durable side-effect intents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from threading import Event as ThreadEvent, Thread
from typing import Any, Callable
from urllib.parse import urlparse

from .domain import OutboxStatus
from .http_transport import (
    HTTPDeadlineExceeded,
    HTTPResponseTooLarge,
    HTTPTransportError,
    post_bytes,
)


class OutboxDispatchStatus(str, Enum):
    IDLE = "idle"
    DELIVERED = "delivered"
    RETRY = "retry"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class OutboxDispatchResult:
    status: OutboxDispatchStatus
    message_id: str = ""
    delivery_token: int = 0
    error_category: str = ""


class OutboxDispatcher:
    def __init__(
        self,
        repository,
        worker_id: str,
        *,
        topic: str = "webhook",
        lease_seconds: int = 30,
        retry_seconds: int = 5,
        repository_factory: Callable[[], object] | None = None,
    ):
        if not worker_id.strip():
            raise ValueError("outbox worker ID must not be empty")
        if lease_seconds <= 0:
            raise ValueError("outbox lease must be positive")
        if retry_seconds < 0:
            raise ValueError("outbox retry delay must not be negative")
        if not topic.strip():
            raise ValueError("outbox topic must not be empty")
        self.repository = repository
        self.worker_id = worker_id
        self.topic = topic.strip()
        self.lease_seconds = lease_seconds
        self.retry_seconds = retry_seconds
        self.repository_factory = repository_factory

    def run_once(
        self,
        handler: Callable[[str, dict[str, Any], str, int], None],
    ) -> OutboxDispatchResult:
        minimum_lease = getattr(handler, "minimum_lease_seconds", 0)
        if self.lease_seconds < minimum_lease:
            raise ValueError(
                "outbox lease must exceed webhook timeout by a 5 second "
                "safety margin"
            )
        if (
            getattr(handler, "requires_lease_renewal", False)
            and self.repository_factory is None
        ):
            raise ValueError(
                "webhook delivery requires an outbox repository factory "
                "for lease renewal"
            )
        message = self.repository.claim_outbox(
            self.worker_id,
            topic=self.topic,
            now=self.repository.scheduler_now(),
            lease_seconds=self.lease_seconds,
        )
        if message is None:
            return OutboxDispatchResult(OutboxDispatchStatus.IDLE)
        maintainer = (
            _OutboxLeaseMaintainer(
                self.repository_factory,
                message.message_id,
                self.worker_id,
                message.delivery_token,
                self.lease_seconds,
            )
            if self.repository_factory is not None
            else None
        )
        if maintainer is not None:
            maintainer.start()
        try:
            handler(
                message.topic,
                dict(message.payload),
                message.idempotency_key,
                message.delivery_token,
            )
        except Exception as error:
            if maintainer is not None:
                maintainer.stop()
                if maintainer.lost:
                    return OutboxDispatchResult(
                        OutboxDispatchStatus.RETRY,
                        message.message_id,
                        message.delivery_token,
                        "LeaseLost",
                    )
            updated = self.repository.complete_outbox(
                message.message_id,
                self.worker_id,
                message.delivery_token,
                now=self.repository.scheduler_now(),
                error=type(error).__name__,
                retry_seconds=self.retry_seconds,
            )
            return OutboxDispatchResult(
                (
                    OutboxDispatchStatus.FAILED
                    if updated.status == OutboxStatus.FAILED
                    else OutboxDispatchStatus.RETRY
                ),
                updated.message_id,
                updated.delivery_token,
                type(error).__name__,
            )
        if maintainer is not None:
            maintainer.stop()
            if maintainer.lost:
                return OutboxDispatchResult(
                    OutboxDispatchStatus.RETRY,
                    message.message_id,
                    message.delivery_token,
                    "LeaseLost",
                )
        updated = self.repository.complete_outbox(
            message.message_id,
            self.worker_id,
            message.delivery_token,
            now=self.repository.scheduler_now(),
        )
        return OutboxDispatchResult(
            OutboxDispatchStatus.DELIVERED,
            updated.message_id,
            updated.delivery_token,
        )

    def run(
        self,
        handler: Callable[[str, dict[str, Any], str, int], None],
        *,
        stop_event: ThreadEvent,
        poll_interval_seconds: float = 1.0,
    ) -> OutboxDispatchResult:
        if poll_interval_seconds <= 0:
            raise ValueError("outbox poll interval must be positive")
        while not stop_event.is_set():
            result = self.run_once(handler)
            if result.status == OutboxDispatchStatus.IDLE:
                stop_event.wait(poll_interval_seconds)
        return OutboxDispatchResult(OutboxDispatchStatus.STOPPED)


class _OutboxLeaseMaintainer:
    def __init__(
        self,
        repository_factory: Callable[[], object],
        message_id: str,
        worker_id: str,
        delivery_token: int,
        lease_seconds: int,
    ):
        self.repository_factory = repository_factory
        self.message_id = message_id
        self.worker_id = worker_id
        self.delivery_token = delivery_token
        self.lease_seconds = lease_seconds
        self._stop = ThreadEvent()
        self._lost = ThreadEvent()
        self._thread = Thread(target=self._run, daemon=True)

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        repository = None
        try:
            repository = self.repository_factory()
            interval = max(0.1, self.lease_seconds / 3)
            while not self._stop.wait(interval):
                repository.renew_outbox(
                    self.message_id,
                    self.worker_id,
                    self.delivery_token,
                    now=repository.scheduler_now(),
                    lease_seconds=self.lease_seconds,
                )
        except Exception:
            self._lost.set()
        finally:
            if repository is not None:
                close = getattr(repository, "close", None)
                if close is not None:
                    close()


class WebhookOutboxHandler:
    def __init__(
        self,
        url: str,
        *,
        bearer_token: str = "",
        allow_insecure_localhost: bool = False,
        timeout_seconds: float = 30.0,
    ):
        parsed = urlparse(url)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("webhook URL must not contain credentials")
        if parsed.fragment:
            raise ValueError("webhook URL must not contain a fragment")
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https":
            if parsed.scheme != "http" or not loopback:
                raise ValueError("webhook URL must use HTTPS")
            if not allow_insecure_localhost:
                raise ValueError(
                    "HTTP webhook requires explicit loopback opt-in"
                )
        if not parsed.hostname:
            raise ValueError("webhook URL must include a host")
        if timeout_seconds <= 0:
            raise ValueError("webhook timeout must be positive")
        self.url = url
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.minimum_lease_seconds = timeout_seconds + 5
        self.requires_lease_renewal = True

    def __call__(
        self,
        topic: str,
        payload: dict[str, Any],
        idempotency_key: str,
        delivery_token: int,
    ) -> None:
        body = json.dumps(
            {"topic": topic, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "X-Agent-Society-Delivery-Token": str(delivery_token),
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        try:
            response = post_bytes(
                self.url,
                body,
                headers,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=65_536,
            )
        except HTTPDeadlineExceeded:
            raise RuntimeError("webhook wall-clock deadline exceeded") from None
        except HTTPResponseTooLarge:
            raise RuntimeError("webhook response exceeds 65536 bytes") from None
        except HTTPTransportError:
            raise RuntimeError("webhook connection failed") from None
        if not 200 <= response.status < 300:
            raise RuntimeError(
                f"webhook returned HTTP {response.status}"
            )
