"""Command-line interface for running and inspecting agent societies."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from .a2a import (
    A2AHTTPClient,
    A2ALimits,
    A2ARemoteExecutor,
    A2ARemoteWorker,
    register_remote_agent,
)
from .deterministic import CriteriaReviewer, build_demo_engine
from .domain import (
    AgentProfile,
    BenchmarkCase,
    CandidateExecution,
    CandidateIdentity,
    CaseEvaluation,
    Goal,
    RunBudget,
    Task,
)
from .engine import LoopEngine, resolve_approval
from .evaluation import BenchmarkEvaluator, PromotionPolicy
from .github import GitHubIssueClient, GitHubPullRequestClient
from .maintenance import (
    build_maintenance_engine,
    create_maintenance_goal,
    maintenance_goal_configuration,
    maintenance_publication_configuration,
)
from .memory import MemoryManager
from .providers import OpenAICompatibleProvider
from .selection import PerformanceWeightedSelector
from .storage import SQLiteRepository
from .tracing import TraceRecorder


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _emit(value: Any, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(human if human is not None else value)


class SpecPlanner:
    def __init__(self, task_specs: Sequence[dict[str, Any]]):
        self.task_specs = list(task_specs)

    def plan(self, goal: Goal, context: dict[str, Any]) -> Sequence[Task]:
        tasks = []
        for index, spec in enumerate(self.task_specs, start=1):
            tasks.append(
                Task.create(
                    goal.goal_id,
                    str(spec["task_id"]),
                    str(spec["task_type"]),
                    str(spec["description"]),
                    assigned_role=str(spec.get("assigned_role", "worker")),
                    acceptance_criteria=dict(spec.get("acceptance_criteria", {})),
                    dependencies=tuple(spec.get("dependencies", ())),
                    context={
                        "output": str(spec.get("output", "")),
                        "repair_output": str(spec.get("repair_output", "")),
                    },
                    max_attempts=int(spec.get("max_attempts", 3)),
                    position=int(spec.get("position", index)),
                )
            )
        return tasks


class SpecWorker:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id

    def execute(self, task: Task, context: dict[str, Any]) -> str:
        use_repair = bool(context.get("review_feedback"))
        key = "repair_output" if use_repair else "output"
        output = task.context.get(key) or task.context.get("output")
        if not output:
            raise ValueError(f"task {task.task_id} has no {key}")
        return str(output)


def _load_spec(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid goal spec: {error}") from error
    required = ("title", "description", "tasks")
    if not isinstance(data, dict) or any(not data.get(field) for field in required):
        raise ValueError("invalid goal spec: title, description, and tasks are required")
    if not isinstance(data["tasks"], list) or not data["tasks"]:
        raise ValueError("invalid goal spec: tasks must be a non-empty list")
    task_required = ("task_id", "task_type", "description", "output")
    for index, task in enumerate(data["tasks"]):
        if not isinstance(task, dict) or any(field not in task for field in task_required):
            raise ValueError(
                f"invalid goal spec: task {index + 1} requires "
                + ", ".join(task_required)
            )
    return data


def _agent_id(task_type: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task_type.casefold()).strip("-")
    return f"spec-{slug or 'worker'}"


def _build_spec_engine(
    repository: SQLiteRepository,
    spec: dict[str, Any],
    *,
    allow_remote: bool = False,
    remote_limits: A2ALimits | None = None,
) -> LoopEngine:
    workers = {}
    task_types: dict[str, set[str]] = {}
    for task in spec["tasks"]:
        agent_id = _agent_id(str(task["task_type"]))
        task_types.setdefault(agent_id, set()).add(str(task["task_type"]))
        workers.setdefault(agent_id, SpecWorker(agent_id))
    for agent_id, supported in task_types.items():
        repository.save_agent(
            AgentProfile(agent_id, "worker", "static-spec-v1", tuple(sorted(supported)))
        )
    if allow_remote:
        active_identities = {
            (deployment.champion_agent_id, deployment.champion_model_id)
            for deployment in repository.list_deployments()
        }
        limits = remote_limits or A2ALimits()
        for registration in repository.list_remote_agents():
            if (registration.agent_id, registration.model_id) not in active_identities:
                continue
            token = _registration_token(registration.auth_env)
            client = A2AHTTPClient(
                auth_token=token,
                limits=limits,
                allow_insecure_localhost=registration.allow_insecure_localhost,
            )
            workers[registration.agent_id] = A2ARemoteWorker(
                A2ARemoteExecutor(
                    repository,
                    registration,
                    client,
                    tracer=TraceRecorder(repository),
                    limits=limits,
                )
            )
    memory = MemoryManager(repository)
    return LoopEngine(
        planner=SpecPlanner(spec["tasks"]),
        workers=workers,
        reviewer=CriteriaReviewer(),
        repository=repository,
        memory=memory,
        selector=PerformanceWeightedSelector(),
        budget=RunBudget(max_actions=int(spec.get("max_actions", 100))),
    )


def _registration_token(auth_env: str) -> str:
    if not auth_env:
        return ""
    token = os.environ.get(auth_env, "")
    if not token:
        raise ValueError(f"remote authentication environment variable is missing: {auth_env}")
    return token


def _parse_skill_declarations(values: Sequence[str]) -> dict[str, str]:
    skills: dict[str, str] = {}
    for declaration in values:
        task_type, separator, skill_id = declaration.partition("=")
        task_type = task_type.strip()
        skill_id = skill_id.strip()
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_type)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", skill_id)
        ):
            raise ValueError("skills must use TASK_TYPE=SKILL_ID with safe identifiers")
        if task_type in skills:
            raise ValueError(f"duplicate remote task type: {task_type}")
        skills[task_type] = skill_id
    if not skills:
        raise ValueError("at least one --skill TASK_TYPE=SKILL_ID is required")
    return skills


def _status(repository: SQLiteRepository, goal_id: str) -> dict[str, Any]:
    goal = repository.get_goal(goal_id)
    if goal is None:
        raise KeyError(f"goal not found: {goal_id}")
    return {
        "goal": goal,
        "tasks": repository.list_tasks(goal_id),
        "attempts": repository.list_attempts(goal_id),
        "reviews": repository.list_reviews(goal_id),
        "artifacts": repository.list_artifacts(goal_id),
        "approvals": repository.list_approvals(goal_id),
        "spans": repository.list_spans(goal_id),
        "workspace_snapshots": repository.list_workspace_snapshots(goal_id),
        "verification_results": repository.list_verification_results(goal_id),
        "publication": repository.get_publication(goal_id),
    }


def _provider_from_environment() -> OpenAICompatibleProvider:
    api_key = os.environ.get("MODEL_API_KEY", "")
    model = os.environ.get("MODEL_ID", "")
    if not api_key or not model:
        raise ValueError("MODEL_API_KEY and MODEL_ID are required")
    return OpenAICompatibleProvider(
        api_key,
        os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1"),
        model,
        response_format={"type": "json_object"},
    )


class _ObservedBenchmarkRunner:
    def __init__(self, results: dict[tuple[str, str], dict[str, Any]]):
        self.results = results

    def __call__(
        self, candidate: CandidateIdentity, case: BenchmarkCase
    ) -> CandidateExecution:
        result = self.results[(case.case_id, candidate.agent_id)]
        return CandidateExecution(result, float(result["duration_ms"]))


def _score_observed_result(case: BenchmarkCase, output: Any) -> CaseEvaluation:
    if not isinstance(output, dict):
        raise ValueError("observed candidate result must be an object")
    return CaseEvaluation(bool(output["passed"]), float(output["score"]))


def _load_evaluation_spec(
    path: str,
) -> tuple[
    tuple[BenchmarkCase, ...],
    CandidateIdentity,
    CandidateIdentity,
    dict[tuple[str, str], dict[str, Any]],
]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid evaluation spec: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("invalid evaluation spec: cases must be a list")
    if not data["cases"]:
        raise ValueError("invalid evaluation spec: benchmark needs at least one case")
    task_type = str(data.get("task_type", "")).strip()
    identities = []
    for role in ("champion", "challenger"):
        value = data.get(role)
        if not isinstance(value, dict):
            raise ValueError(f"invalid evaluation spec: {role} must be an object")
        identities.append(
            CandidateIdentity(
                str(value.get("agent_id", "")), str(value.get("model_id", ""))
            )
        )
    champion, challenger = identities
    benchmark: list[BenchmarkCase] = []
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for index, value in enumerate(data["cases"], start=1):
        if not isinstance(value, dict):
            raise ValueError(f"invalid evaluation spec: case {index} must be an object")
        case = BenchmarkCase.create(
            str(value.get("case_id", "")),
            task_type,
            dict(value.get("input", {})),
            dict(value.get("acceptance_criteria", {})),
            context=dict(value.get("context", {})),
            critical=bool(value.get("critical", False)),
        )
        observed = value.get("results")
        if not isinstance(observed, dict):
            raise ValueError(f"invalid evaluation spec: case {case.case_id} needs results")
        for candidate in (champion, challenger):
            result = observed.get(candidate.agent_id)
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("passed"), bool)
                or not isinstance(result.get("score"), (int, float))
                or not isinstance(result.get("duration_ms"), (int, float))
            ):
                raise ValueError(
                    f"invalid evaluation result for {case.case_id}/{candidate.agent_id}"
                )
            CaseEvaluation(result["passed"], float(result["score"]))
            CandidateExecution(result, float(result["duration_ms"]))
            results[(case.case_id, candidate.agent_id)] = dict(result)
        benchmark.append(case)
    return tuple(benchmark), champion, challenger, results


def _ensure_evaluation_agent(
    repository: SQLiteRepository,
    candidate: CandidateIdentity,
    task_type: str,
) -> None:
    existing = repository.get_agent(candidate.agent_id)
    if existing is None:
        repository.save_agent(
            AgentProfile(candidate.agent_id, "worker", candidate.model_id, (task_type,))
        )
        return
    if (
        not existing.enabled
        or existing.role != "worker"
        or existing.model_id != candidate.model_id
        or not ("*" in existing.task_types or task_type in existing.task_types)
    ):
        raise ValueError(f"candidate identity conflicts with agent {candidate.agent_id}")


def _maintenance_goal_id(repository: str, issue: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", repository.casefold()).strip("-")
    return f"maintain-{slug}-{issue}"


def _parse_check_declarations(values: Sequence[str]) -> dict[str, tuple[str, ...]]:
    result = {}
    for declaration in values:
        name, separator, command_text = declaration.partition("=")
        name = name.strip()
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError("checks must use NAME=COMMAND with a safe short name")
        if name in result:
            raise ValueError(f"duplicate check: {name}")
        try:
            command = tuple(shlex.split(command_text, posix=True))
        except ValueError as error:
            raise ValueError(f"invalid check command for {name}: {error}") from error
        if not command:
            raise ValueError(f"check command must not be empty: {name}")
        result[name] = command
    return result


def _database_protected_paths(database: str, workspace: Path) -> tuple[str, ...]:
    database_path = Path(database).resolve()
    if not database_path.is_relative_to(workspace):
        return ()
    relative = database_path.relative_to(workspace).as_posix()
    return (relative, f"{relative}-shm", f"{relative}-wal")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-society",
        description="Run auditable goal-driven societies of specialized agents.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="run the offline quantum mug scenario")
    demo.add_argument("--db", default="agent-society.db")
    demo.add_argument("--goal-id", default="quantum-mug-demo")
    demo.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="run a JSON goal specification")
    run.add_argument("spec")
    run.add_argument("--db", default="agent-society.db")
    run.add_argument("--allow-remote", action="store_true")
    run.add_argument("--remote-timeout", type=float, default=60.0)
    run.add_argument("--remote-max-polls", type=int, default=20)
    run.add_argument("--remote-poll-interval", type=float, default=0.25)
    run.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="inspect goal state and artifacts")
    status.add_argument("goal_id")
    status.add_argument("--db", default="agent-society.db")
    status.add_argument("--json", action="store_true")

    events = commands.add_parser("events", help="inspect an ordered audit trail")
    events.add_argument("goal_id")
    events.add_argument("--db", default="agent-society.db")
    events.add_argument("--json", action="store_true")

    agents = commands.add_parser("agents", help="inspect agents and social memory")
    agents.add_argument("--db", default="agent-society.db")
    agents.add_argument("--json", action="store_true")

    evaluate = commands.add_parser(
        "evaluate", help="evaluate a challenger against a benchmark"
    )
    evaluate.add_argument("spec")
    evaluate.add_argument("--db", default="agent-society.db")
    evaluate.add_argument("--json", action="store_true")

    evaluations = commands.add_parser(
        "evaluations", help="inspect evaluation runs and case outcomes"
    )
    evaluations.add_argument("run_id", nargs="?")
    evaluations.add_argument("--db", default="agent-society.db")
    evaluations.add_argument("--json", action="store_true")

    promote = commands.add_parser(
        "promote", help="explicitly promote a recommended challenger"
    )
    promote.add_argument("run_id")
    promote.add_argument("--by", required=True)
    promote.add_argument("--db", default="agent-society.db")
    promote.add_argument("--json", action="store_true")

    deployments = commands.add_parser(
        "deployments", help="inspect active task-type champions"
    )
    deployments.add_argument("--db", default="agent-society.db")
    deployments.add_argument("--json", action="store_true")

    a2a = commands.add_parser("a2a", help="manage pinned A2A 1.0 remote agents")
    a2a_commands = a2a.add_subparsers(dest="a2a_command", required=True)
    inspect_card = a2a_commands.add_parser(
        "inspect-card", help="inspect a bounded Agent Card"
    )
    inspect_card.add_argument("url")
    inspect_card.add_argument("--allow-insecure-localhost", action="store_true")
    inspect_card.add_argument("--db", default="agent-society.db")
    inspect_card.add_argument("--json", action="store_true")
    register = a2a_commands.add_parser(
        "register", help="register an operator-pinned remote agent"
    )
    register.add_argument("agent_id")
    register.add_argument("card_url")
    register.add_argument("--sha256", required=True)
    register.add_argument("--interface", required=True)
    register.add_argument("--skill", action="append", default=[])
    register.add_argument("--auth-env", default="")
    register.add_argument(
        "--allow-context",
        action="append",
        default=[],
        choices=(
            "goal",
            "task_context",
            "dependency_artifacts",
            "review_feedback",
            "knowledge",
        ),
    )
    register.add_argument("--allow-insecure-localhost", action="store_true")
    register.add_argument("--db", default="agent-society.db")
    register.add_argument("--json", action="store_true")
    remote_agents = a2a_commands.add_parser("agents", help="list remote trust records")
    remote_agents.add_argument("--db", default="agent-society.db")
    remote_agents.add_argument("--json", action="store_true")
    delegations = a2a_commands.add_parser(
        "delegations", help="inspect durable remote delegations"
    )
    delegations.add_argument("delegation_id", nargs="?")
    delegations.add_argument("--db", default="agent-society.db")
    delegations.add_argument("--json", action="store_true")
    cancel = a2a_commands.add_parser("cancel", help="cancel a known remote task")
    cancel.add_argument("delegation_id")
    cancel.add_argument("--by", required=True)
    cancel.add_argument("--db", default="agent-society.db")
    cancel.add_argument("--json", action="store_true")

    maintain = commands.add_parser(
        "maintain", help="inspect a GitHub issue and produce a reviewed proposal"
    )
    maintain.add_argument("repository")
    maintain.add_argument("issue", type=int)
    maintain.add_argument("--workspace", required=True)
    maintain.add_argument("--goal-id")
    maintain.add_argument("--db", default="agent-society.db")
    maintain.add_argument(
        "--apply",
        action="store_true",
        help="enable approved local UTF-8 writes and named verification checks",
    )
    maintain.add_argument(
        "--publish", action="store_true",
        help="enable approved commit, push, and pull-request publication",
    )
    maintain.add_argument("--base", default="main", help="pull-request base branch")
    maintain.add_argument("--remote", default="origin", help="Git remote to push")
    maintain.add_argument(
        "--branch-prefix", default="agent-society/",
        help="required prefix for the current publication branch",
    )
    maintain.add_argument(
        "--check",
        dest="checks",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="operator-configured verification command; repeat for multiple checks",
    )
    maintain.add_argument("--json", action="store_true")

    traces = commands.add_parser("traces", help="inspect linked execution spans")
    traces.add_argument("goal_id")
    traces.add_argument("--db", default="agent-society.db")
    traces.add_argument("--json", action="store_true")

    approvals = commands.add_parser("approvals", help="inspect durable approvals")
    approvals.add_argument("goal_id")
    approvals.add_argument("--db", default="agent-society.db")
    approvals.add_argument("--json", action="store_true")

    for name in ("approve", "reject"):
        decision = commands.add_parser(name, help=f"{name} a pending tool call")
        decision.add_argument("approval_id")
        decision.add_argument("--by", required=True)
        decision.add_argument("--db", default="agent-society.db")
        decision.add_argument("--json", action="store_true")

    knowledge = commands.add_parser("knowledge", help="manage long-term knowledge")
    knowledge_commands = knowledge.add_subparsers(dest="knowledge_command", required=True)
    add = knowledge_commands.add_parser("add", help="add a knowledge item")
    add.add_argument("title")
    add.add_argument("content")
    add.add_argument("--tag", action="append", default=[])
    add.add_argument("--db", default="agent-society.db")
    add.add_argument("--json", action="store_true")
    search = knowledge_commands.add_parser("search", help="search knowledge")
    search.add_argument("query")
    search.add_argument("--tag", action="append", default=[])
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--db", default="agent-society.db")
    search.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = SQLiteRepository(args.db)
    try:
        if args.command == "demo":
            engine = build_demo_engine(repository)
            goal = repository.get_goal(args.goal_id)
            if goal is None:
                goal = engine.create_goal(
                    "Quantum Coffee Mug Launch",
                    "Create market, visual, copy, and integrated launch materials",
                    goal_id=args.goal_id,
                )
            report = engine.resume(goal.goal_id)
            _emit(
                report,
                args.json,
                f"Goal {report.goal_id}: {report.status.value} "
                f"({report.tasks_succeeded}/{report.tasks_total} tasks, {report.retries} retries)",
            )
            return 0 if report.status.value == "succeeded" else 1

        if args.command == "run":
            spec = _load_spec(args.spec)
            remote_limits = A2ALimits(
                request_timeout=min(10.0, args.remote_timeout),
                total_timeout=args.remote_timeout,
                max_polls=args.remote_max_polls,
                poll_interval=args.remote_poll_interval,
            )
            engine = _build_spec_engine(
                repository,
                spec,
                allow_remote=args.allow_remote,
                remote_limits=remote_limits,
            )
            goal_id = str(spec.get("goal_id") or "") or None
            if goal_id and repository.get_goal(goal_id) is not None:
                raise ValueError(f"goal already exists: {goal_id}")
            goal = engine.create_goal(
                str(spec["title"]), str(spec["description"]), goal_id=goal_id
            )
            report = engine.run(goal.goal_id)
            _emit(report, args.json, f"Goal {report.goal_id}: {report.status.value}")
            return 0 if report.status.value == "succeeded" else 1

        if args.command == "a2a":
            if args.a2a_command == "inspect-card":
                client = A2AHTTPClient(
                    allow_insecure_localhost=args.allow_insecure_localhost
                )
                value = client.inspect_card(args.url)
                _emit(value, args.json, f"Agent Card SHA-256: {value.sha256}")
                return 0
            if args.a2a_command == "register":
                skills = _parse_skill_declarations(args.skill)
                token = _registration_token(args.auth_env)
                client = A2AHTTPClient(
                    auth_token=token,
                    allow_insecure_localhost=args.allow_insecure_localhost,
                )
                contexts = tuple(args.allow_context) or ("review_feedback",)
                value = register_remote_agent(
                    repository,
                    client,
                    args.agent_id,
                    args.card_url,
                    args.sha256,
                    args.interface,
                    skills,
                    auth_env=args.auth_env,
                    allowed_context_sections=contexts,
                    allow_insecure_localhost=args.allow_insecure_localhost,
                )
                _emit(value, args.json, f"Registered remote agent {value.agent_id}")
                return 0
            if args.a2a_command == "agents":
                value = repository.list_remote_agents()
                _emit(value, args.json, f"{len(value)} remote agents")
                return 0
            if args.a2a_command == "delegations":
                if args.delegation_id:
                    value = repository.get_delegation(args.delegation_id)
                    if value is None:
                        raise KeyError(
                            f"delegation not found: {args.delegation_id}"
                        )
                    human = f"Delegation {value.delegation_id}: {value.status.value}"
                else:
                    value = repository.list_delegations()
                    human = f"{len(value)} delegations"
                _emit(value, args.json, human)
                return 0
            delegation = repository.get_delegation(args.delegation_id)
            if delegation is None:
                raise KeyError(f"delegation not found: {args.delegation_id}")
            registration = repository.get_remote_agent(delegation.agent_id)
            if registration is None or registration.model_id != delegation.model_id:
                raise ValueError("delegation remote identity is unavailable")
            token = _registration_token(registration.auth_env)
            limits = A2ALimits()
            executor = A2ARemoteExecutor(
                repository,
                registration,
                A2AHTTPClient(
                    auth_token=token,
                    limits=limits,
                    allow_insecure_localhost=registration.allow_insecure_localhost,
                ),
                tracer=TraceRecorder(repository),
                limits=limits,
            )
            value = executor.cancel(delegation.delegation_id, args.by)
            _emit(value, args.json, f"Delegation {value.delegation_id}: {value.status.value}")
            return 0

        if args.command == "status":
            value = _status(repository, args.goal_id)
            _emit(value, args.json, f"Goal {args.goal_id}: {value['goal'].status.value}")
            return 0

        if args.command == "events":
            if repository.get_goal(args.goal_id) is None:
                raise KeyError(f"goal not found: {args.goal_id}")
            value = repository.list_events(args.goal_id)
            human = "\n".join(
                f"{event.sequence:04d} {event.event_type}" for event in value
            )
            _emit(value, args.json, human)
            return 0

        if args.command == "agents":
            performance = repository.list_performance()
            value = [
                {
                    **_jsonable(agent),
                    "performance": [
                        _jsonable(record)
                        for record in performance
                        if record.agent_id == agent.agent_id
                    ],
                }
                for agent in repository.list_agents()
            ]
            _emit(value, args.json, f"{len(value)} registered agents")
            return 0

        if args.command == "evaluate":
            benchmark, champion, challenger, results = _load_evaluation_spec(args.spec)
            _ensure_evaluation_agent(repository, champion, benchmark[0].task_type)
            _ensure_evaluation_agent(repository, challenger, benchmark[0].task_type)
            value = BenchmarkEvaluator(
                repository,
                _ObservedBenchmarkRunner(results),
                _score_observed_result,
                PromotionPolicy(),
            ).evaluate(benchmark, champion, challenger)
            _emit(
                value,
                args.json,
                f"Evaluation {value.run_id}: "
                f"{'recommended' if value.recommended else 'rejected'}",
            )
            return 0 if value.recommended else 1

        if args.command == "evaluations":
            if args.run_id:
                run = repository.get_evaluation_run(args.run_id)
                if run is None:
                    raise KeyError(f"evaluation run not found: {args.run_id}")
                value = {
                    "run": run,
                    "outcomes": repository.list_evaluation_outcomes(args.run_id),
                }
                human = f"Evaluation {run.run_id}: {run.status.value}"
            else:
                value = repository.list_evaluation_runs()
                human = f"{len(value)} evaluation runs"
            _emit(value, args.json, human)
            return 0

        if args.command == "promote":
            value = repository.promote_evaluation(args.run_id, args.by)
            _emit(
                value,
                args.json,
                f"Deployed {value.champion_agent_id} for {value.task_type}",
            )
            return 0

        if args.command == "deployments":
            value = repository.list_deployments()
            _emit(value, args.json, f"{len(value)} active deployments")
            return 0

        if args.command == "maintain":
            provider = _provider_from_environment()
            workspace = Path(args.workspace).resolve()
            checks = _parse_check_declarations(args.checks)
            if args.apply and not checks:
                raise ValueError("--apply requires at least one --check NAME=COMMAND")
            if checks and not args.apply:
                raise ValueError("--check requires --apply")
            if args.publish and not args.apply:
                raise ValueError("--publish requires --apply")
            protected_database = _database_protected_paths(args.db, workspace)
            if args.publish and protected_database:
                raise ValueError("publication requires --db outside the workspace")
            pull_request_client = None
            if args.publish:
                pull_request_client = GitHubPullRequestClient(
                    os.environ.get("GITHUB_TOKEN", "")
                )
            engine = build_maintenance_engine(
                repository,
                provider,
                workspace,
                apply=args.apply,
                checks=checks,
                protected_paths=protected_database,
                publish=args.publish,
                pull_request_client=pull_request_client,
                github_repository=args.repository,
                base_branch=args.base,
                remote=args.remote,
                branch_prefix=args.branch_prefix,
            )
            goal_id = args.goal_id or _maintenance_goal_id(
                args.repository, args.issue
            )
            goal = repository.get_goal(goal_id)
            if goal is None:
                issue = GitHubIssueClient(
                    token=os.environ.get("GITHUB_TOKEN", "")
                ).get_issue(args.repository, args.issue)
                goal = create_maintenance_goal(
                    engine,
                    issue,
                    goal_id=goal_id,
                    apply=args.apply,
                    workspace=workspace if args.apply else None,
                    check_names=tuple(checks),
                    publish=args.publish,
                    base_branch=args.base,
                    remote=args.remote,
                    branch_prefix=args.branch_prefix,
                )
            else:
                stored_apply, stored_checks = maintenance_goal_configuration(goal)
                if stored_apply != args.apply or stored_checks != tuple(sorted(checks)):
                    raise ValueError(
                        "maintenance resume must use the original apply mode and check names"
                    )
                stored_publication = maintenance_publication_configuration(goal)
                requested_publication = {
                    "enabled": args.publish,
                    "base_branch": args.base,
                    "remote": args.remote,
                    "branch_prefix": args.branch_prefix,
                }
                if stored_publication != requested_publication:
                    raise ValueError(
                        "maintenance resume must use the original publication configuration"
                    )
            report = engine.resume(goal.goal_id)
            _emit(
                report,
                args.json,
                f"Goal {report.goal_id}: {report.status.value}",
            )
            if report.status.value == "succeeded":
                return 0
            if report.status.value == "paused":
                return 3
            return 1

        if args.command == "traces":
            if repository.get_goal(args.goal_id) is None:
                raise KeyError(f"goal not found: {args.goal_id}")
            value = repository.list_spans(args.goal_id)
            _emit(value, args.json, f"{len(value)} trace spans")
            return 0

        if args.command == "approvals":
            if repository.get_goal(args.goal_id) is None:
                raise KeyError(f"goal not found: {args.goal_id}")
            value = repository.list_approvals(args.goal_id)
            _emit(value, args.json, f"{len(value)} approval requests")
            return 0

        if args.command in {"approve", "reject"}:
            value = resolve_approval(
                repository,
                args.approval_id,
                approved=args.command == "approve",
                decided_by=args.by,
            )
            _emit(value, args.json, f"Approval {value.approval_id}: {value.status.value}")
            return 0

        memory = MemoryManager(repository)
        if args.knowledge_command == "add":
            knowledge_id = memory.add_knowledge(args.title, args.content, args.tag)
            _emit(
                {"knowledge_id": knowledge_id},
                args.json,
                f"Added knowledge {knowledge_id}",
            )
            return 0
        results = memory.search_knowledge(args.query, args.tag, limit=args.limit)
        _emit(results, args.json, f"{len(results)} matching knowledge items")
        return 0
    except (KeyError, ValueError, OSError, RuntimeError) as error:
        message = error.args[0] if isinstance(error, KeyError) else str(error)
        print(f"error: {message}", file=sys.stderr)
        return 2
    finally:
        repository.close()
