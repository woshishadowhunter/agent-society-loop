"""Small GitHub issue and pull-request HTTP clients."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class GitHubIssue:
    repository: str
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    html_url: str
    state: str


@dataclass(frozen=True, slots=True)
class GitHubPullRequest:
    number: int
    html_url: str


class GitHubIssueClient:
    def __init__(
        self,
        base_url: str = "https://api.github.com",
        *,
        token: str = "",
        timeout: float = 15.0,
        max_response_bytes: int = 1_000_000,
    ):
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if timeout <= 0 or max_response_bytes < 1:
            raise ValueError("timeout and max_response_bytes must be positive")
        self.base_url = base_url.rstrip("/")
        self._token = token.strip()
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def get_issue(self, repository: str, number: int) -> GitHubIssue:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository must use owner/name format")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError("issue number must be positive")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-society-loop/0.2",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(
            f"{self.base_url}/repos/{repository}/issues/{number}",
            headers=headers,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > self.max_response_bytes:
                    raise ValueError("GitHub response is too large")
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise ValueError("GitHub response is too large")
        except HTTPError as error:
            raise RuntimeError(f"GitHub returned HTTP {error.code}") from None
        except URLError as error:
            raise RuntimeError(f"GitHub connection failed: {error.reason}") from None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("GitHub returned malformed JSON") from error
        return _normalize_issue(repository, number, data)


class GitHubPullRequestClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        *,
        timeout: float = 20.0,
        max_response_bytes: int = 1_000_000,
    ):
        if not token.strip():
            raise ValueError("GitHub token is required for pull-request publication")
        self._token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def create_or_get(
        self,
        repository: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> GitHubPullRequest:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository must use owner/name format")
        owner = repository.split("/", 1)[0]
        query = urlencode({"state": "open", "head": f"{owner}:{head}", "base": base})
        existing = self._request_json(
            "GET", f"/repos/{repository}/pulls?{query}"
        )
        if isinstance(existing, list) and existing:
            return _normalize_pull_request(existing[0])
        created = self._request_json(
            "POST",
            f"/repos/{repository}/pulls",
            {"title": title, "body": body, "head": head, "base": base},
        )
        return _normalize_pull_request(created)

    def _request_json(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> object:
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "agent-society-loop/0.4",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise ValueError("GitHub response is too large")
        except HTTPError as error:
            raise RuntimeError(f"GitHub returned HTTP {error.code}") from None
        except URLError as error:
            raise RuntimeError(f"GitHub connection failed: {error.reason}") from None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("GitHub returned malformed JSON") from error


def _normalize_issue(
    repository: str, number: int, data: object
) -> GitHubIssue:
    if not isinstance(data, dict):
        raise ValueError("GitHub returned a malformed issue")
    title = data.get("title")
    body = data.get("body") or ""
    html_url = data.get("html_url")
    state = data.get("state")
    labels = data.get("labels", [])
    if (
        not isinstance(title, str)
        or not isinstance(body, str)
        or not isinstance(html_url, str)
        or not isinstance(state, str)
        or not isinstance(labels, list)
    ):
        raise ValueError("GitHub returned a malformed issue")
    names = []
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise ValueError("GitHub returned a malformed issue")
        names.append(label["name"])
    return GitHubIssue(
        repository=repository,
        number=number,
        title=title.strip(),
        body=body,
        labels=tuple(names),
        html_url=html_url,
        state=state,
    )


def _normalize_pull_request(data: object) -> GitHubPullRequest:
    if not isinstance(data, dict):
        raise ValueError("GitHub returned a malformed pull request")
    number = data.get("number")
    html_url = data.get("html_url")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("GitHub returned a malformed pull request")
    if not isinstance(html_url, str) or not html_url.startswith("https://"):
        raise ValueError("GitHub returned a malformed pull request")
    return GitHubPullRequest(number, html_url)
