"""Optional model providers implemented without mandatory SDK dependencies."""

from __future__ import annotations

import json
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OpenAICompatibleProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        timeout: float = 30.0,
        response_format: dict[str, Any] | None = None,
    ):
        if not api_key.strip() or not base_url.strip() or not model.strip():
            raise ValueError("api_key, base_url, and model are required")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.response_format = dict(response_format) if response_format else None

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
        request = Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise RuntimeError(
                f"model provider returned HTTP {error.code}"
            ) from None
        except URLError as error:
            raise RuntimeError(f"model provider connection failed: {error.reason}") from None
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("model provider returned malformed JSON") from error

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("model provider returned a malformed response") from error
        if not isinstance(content, str) or not content.strip():
            raise ValueError("model provider returned a malformed response")
        return content
