"""Optional model providers implemented without mandatory SDK dependencies."""

from __future__ import annotations

import json
from typing import Any, Sequence
from urllib.parse import urlparse

from .http_transport import (
    HTTPDeadlineExceeded,
    HTTPResponseTooLarge,
    HTTPTransportError,
    post_bytes,
)


_MODEL_RESPONSE_LIMIT = 1_048_576


class OpenAICompatibleProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        timeout: float = 30.0,
        response_format: dict[str, Any] | None = None,
        allow_insecure_http: bool = False,
    ):
        if not base_url.strip() or not model.strip():
            raise ValueError("base_url and model are required")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("model base_url must be an HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("model base_url must not contain credentials")
        if parsed.query:
            raise ValueError("model base_url must not contain a query")
        if parsed.fragment:
            raise ValueError("model base_url must not contain a fragment")
        hostname = parsed.hostname
        loopback = hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not loopback and not allow_insecure_http:
            raise ValueError(
                "non-loopback model endpoints must use HTTPS "
                "unless allow_insecure_http is explicit"
            )
        if not api_key.strip() and hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("api_key is required for non-loopback model endpoints")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.response_format = dict(response_format) if response_format else None
        self.allow_insecure_http = bool(allow_insecure_http)

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        request_data: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if self.response_format is not None:
            request_data["response_format"] = self.response_format
        payload = json.dumps(request_data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = post_bytes(
                f"{self.base_url}/chat/completions",
                payload,
                headers,
                timeout_seconds=self.timeout,
                max_response_bytes=_MODEL_RESPONSE_LIMIT,
            )
        except HTTPDeadlineExceeded:
            raise RuntimeError("model provider wall-clock deadline exceeded") from None
        except HTTPResponseTooLarge as error:
            raise ValueError(str(error).replace("HTTP", "model provider")) from None
        except HTTPTransportError:
            raise RuntimeError("model provider connection failed") from None
        if not 200 <= response.status < 300:
            raise RuntimeError(
                f"model provider returned HTTP {response.status}"
            )
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("model provider returned malformed JSON") from error

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("model provider returned a malformed response") from error
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model provider returned a malformed response")
        return content
