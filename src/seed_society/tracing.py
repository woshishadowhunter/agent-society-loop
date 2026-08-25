"""Linked, redacted execution trace recording."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from .domain import SpanStatus, TraceSpan
from .storage import SQLiteRepository


_SENSITIVE_KEY_PARTS = ("key", "token", "secret", "authorization", "password")


def _redact(value: Any, key: str = "") -> Any:
    if key and any(part in key.casefold() for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {name: _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class TraceRecorder:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    @contextmanager
    def span(
        self,
        goal_id: str,
        name: str,
        *,
        kind: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[TraceSpan]:
        span = TraceSpan.start(
            goal_id,
            task_id,
            agent_id,
            kind,
            name,
            parent_span_id=parent_span_id,
            attributes=_redact(attributes or {}),
        )
        try:
            yield span
        except Exception as error:
            self.repository.save_span(
                span.finish(SpanStatus.ERROR, error_category=type(error).__name__)
            )
            raise
        else:
            self.repository.save_span(span.finish(SpanStatus.OK))
