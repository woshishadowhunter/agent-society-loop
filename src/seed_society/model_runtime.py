"""Strict, secret-free configuration for model-backed worker processes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Mapping

from .domain import AgentProfile
from .model_agents import ModelReviewer, ModelWorker, parse_json_object
from .providers import OpenAICompatibleProvider


def _keys(
    value: dict,
    *,
    required: set[str],
    allowed: set[str],
    location: str,
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        raise ValueError(f"{location} missing required keys: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"unknown {location} keys: {', '.join(unknown)}")


def _text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ModelProviderConfig:
    name: str
    base_url: str
    model: str
    api_key_env: str = ""
    timeout_seconds: float = 60.0
    structured_output: bool = True
    allow_insecure_http: bool = False

    @property
    def identity(self) -> str:
        endpoint = sha256(self.base_url.rstrip("/").encode("utf-8")).hexdigest()[:16]
        return f"openai-compatible:{self.model}@{endpoint}"

    def build(self, environment: Mapping[str, str]) -> OpenAICompatibleProvider:
        api_key = ""
        if self.api_key_env:
            api_key = environment.get(self.api_key_env, "")
            if not api_key:
                raise ValueError(
                    f"model provider secret environment is missing: {self.api_key_env}"
                )
        return OpenAICompatibleProvider(
            api_key,
            self.base_url,
            self.model,
            timeout=self.timeout_seconds,
            response_format=(
                {"type": "json_object"} if self.structured_output else None
            ),
            allow_insecure_http=self.allow_insecure_http,
        )


@dataclass(frozen=True, slots=True)
class ModelRuntime:
    providers: dict[str, ModelProviderConfig]
    workers: dict[str, ModelWorker]
    assignments: dict[str, str]
    reviewer: ModelReviewer
    profiles: tuple[AgentProfile, ...]
    provider_clients: dict[str, OpenAICompatibleProvider] = field(repr=False)


def doctor_model_runtime(runtime: ModelRuntime) -> dict:
    results = []
    for name in sorted(runtime.providers):
        config = runtime.providers[name]
        result = {
            "name": name,
            "identity": config.identity,
            "base_url": config.base_url,
            "model": config.model,
            "structured_output": config.structured_output,
            "allow_insecure_http": config.allow_insecure_http,
        }
        try:
            response = runtime.provider_clients[name].complete(
                [
                    {
                        "role": "system",
                        "content": (
                            'Return exactly one JSON object: {"status":"ok"}. '
                            "Do not add markdown or other keys."
                        ),
                    },
                    {"role": "user", "content": "Run the compatibility probe."},
                ],
                temperature=0.0,
            )
            if parse_json_object(response) != {"status": "ok"}:
                raise ValueError("model provider failed the exact JSON probe")
        except Exception as error:
            result["passed"] = False
            result["error_category"] = type(error).__name__
        else:
            result["passed"] = True
            result["error_category"] = ""
        results.append(result)
    return {
        "passed": all(result["passed"] for result in results),
        "providers": results,
    }


def load_model_runtime(
    path: str | Path,
    environment: Mapping[str, str],
    *,
    tracer=None,
) -> ModelRuntime:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid model runtime config: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("model runtime config must be an object")
    _keys(
        value,
        required={"providers", "agents", "reviewer"},
        allowed={"providers", "agents", "reviewer"},
        location="model runtime",
    )

    provider_values = value["providers"]
    if not isinstance(provider_values, dict) or not provider_values:
        raise ValueError("model runtime providers must be a non-empty object")
    providers: dict[str, ModelProviderConfig] = {}
    clients: dict[str, OpenAICompatibleProvider] = {}
    for raw_name, raw_provider in provider_values.items():
        name = _text(raw_name, "provider name")
        if not isinstance(raw_provider, dict):
            raise ValueError(f"provider {name} must be an object")
        _keys(
            raw_provider,
            required={"base_url", "model"},
            allowed={
                "base_url",
                "model",
                "api_key_env",
                "timeout_seconds",
                "structured_output",
                "allow_insecure_http",
            },
            location=f"provider {name}",
        )
        timeout = raw_provider.get("timeout_seconds", 60.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"provider {name} timeout_seconds must be positive")
        structured = raw_provider.get("structured_output", True)
        if not isinstance(structured, bool):
            raise ValueError(f"provider {name} structured_output must be boolean")
        allow_insecure_http = raw_provider.get("allow_insecure_http", False)
        if not isinstance(allow_insecure_http, bool):
            raise ValueError(
                f"provider {name} allow_insecure_http must be boolean"
            )
        api_key_env = raw_provider.get("api_key_env", "")
        if not isinstance(api_key_env, str):
            raise ValueError(f"provider {name} api_key_env must be a string")
        config = ModelProviderConfig(
            name=name,
            base_url=_text(raw_provider["base_url"], f"provider {name} base_url"),
            model=_text(raw_provider["model"], f"provider {name} model"),
            api_key_env=api_key_env.strip(),
            timeout_seconds=float(timeout),
            structured_output=structured,
            allow_insecure_http=allow_insecure_http,
        )
        providers[name] = config
        clients[name] = config.build(environment)

    agent_values = value["agents"]
    if not isinstance(agent_values, list) or not agent_values:
        raise ValueError("model runtime agents must be a non-empty list")
    workers: dict[str, ModelWorker] = {}
    assignments: dict[str, str] = {}
    profiles: list[AgentProfile] = []
    for index, raw_agent in enumerate(agent_values, start=1):
        if not isinstance(raw_agent, dict):
            raise ValueError(f"agent {index} must be an object")
        _keys(
            raw_agent,
            required={"agent_id", "provider", "task_types"},
            allowed={"agent_id", "provider", "task_types", "max_tool_steps"},
            location=f"agent {index}",
        )
        agent_id = _text(raw_agent["agent_id"], f"agent {index} agent_id")
        provider_name = _text(raw_agent["provider"], f"agent {index} provider")
        if provider_name not in providers:
            raise ValueError(f"agent {agent_id} references unknown provider: {provider_name}")
        task_values = raw_agent["task_types"]
        if (
            not isinstance(task_values, list)
            or not task_values
            or any(not isinstance(item, str) or not item.strip() for item in task_values)
        ):
            raise ValueError(f"agent {agent_id} task_types must be non-empty strings")
        task_types = tuple(sorted(set(item.strip() for item in task_values)))
        if "*" in task_types:
            raise ValueError(f"agent {agent_id} must declare explicit task types")
        max_tool_steps = raw_agent.get("max_tool_steps", 8)
        if (
            isinstance(max_tool_steps, bool)
            or not isinstance(max_tool_steps, int)
            or max_tool_steps < 1
        ):
            raise ValueError(f"agent {agent_id} max_tool_steps must be positive")
        if agent_id in workers:
            raise ValueError(f"duplicate model agent ID: {agent_id}")
        for task_type in task_types:
            owner = assignments.get(task_type)
            if owner is not None:
                raise ValueError(
                    f"task type {task_type} is assigned to multiple agents"
                )
            assignments[task_type] = agent_id
        workers[agent_id] = ModelWorker(
            agent_id,
            clients[provider_name],
            max_tool_steps=max_tool_steps,
            tracer=tracer,
        )
        profiles.append(
            AgentProfile(
                agent_id,
                "worker",
                providers[provider_name].identity,
                task_types,
            )
        )

    reviewer_value = value["reviewer"]
    if not isinstance(reviewer_value, dict):
        raise ValueError("model runtime reviewer must be an object")
    _keys(
        reviewer_value,
        required={"provider"},
        allowed={"provider", "agent_id"},
        location="reviewer",
    )
    reviewer_provider = _text(reviewer_value["provider"], "reviewer provider")
    if reviewer_provider not in clients:
        raise ValueError(f"reviewer references unknown provider: {reviewer_provider}")
    reviewer_id = _text(
        reviewer_value.get("agent_id", "model-reviewer"),
        "reviewer agent_id",
    )
    if reviewer_id in workers:
        raise ValueError(f"reviewer agent ID conflicts with worker: {reviewer_id}")
    profiles.append(
        AgentProfile(
            reviewer_id,
            "reviewer",
            providers[reviewer_provider].identity,
        )
    )

    return ModelRuntime(
        providers=providers,
        workers=workers,
        assignments=dict(sorted(assignments.items())),
        reviewer=ModelReviewer(
            clients[reviewer_provider],
            tracer,
            agent_id=reviewer_id,
        ),
        profiles=tuple(sorted(profiles, key=lambda profile: profile.agent_id)),
        provider_clients=clients,
    )
