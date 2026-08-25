"""MCP stdio bridge exposing the Agent Society runtime to DeepSeek Harness.

Zero-dependency JSON-RPC 2.0 server over stdio. When registered as a
`@deepseek-ai/dsh-mcp-client` plugin instance, each tool below appears to the
harness model as `mcp__society__<name>`.

Design rules (sila):
- The server never uses a shell: every CLI argument is an argv element built
  from validated JSON input.
- It only dispatches to the installed `seed-society` CLI / package entry
  point; it holds no secrets and opens no network sockets.
- Approval-gated operations stay approval-gated: `society_approve`/`reject`
  still require the operator identity just like the CLI does.

Run manually for a smoke test:
    python -m seed_society.mcp_server
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

SERVER_NAME = "society"
SERVER_VERSION = "1.3.0"

_PROJECT_DIR_ENV = "SOCIETY_PROJECT_DIR"

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "society_plugins",
        "description": (
            "List the eight-consciousness plugin manifest of the agent society "
            "(alaya/manas/mano/panca/sila) or render the full doctrine map."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "consciousness": {
                    "type": "string",
                    "enum": ["alaya", "manas", "mano", "panca", "sila"],
                },
                "describe": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "society_demo",
        "description": (
            "Run the offline quantum mug double-loop demo and return the "
            "auditable outcome."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "db": {"type": "string"},
                "goal_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "society_run_spec",
        "description": (
            "Execute a deterministic JSON goal specification through the "
            "reviewed double loop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string", "description": "Path to the goal-spec JSON file."},
                "db": {"type": "string"},
                "allow_remote": {"type": "boolean"},
            },
            "required": ["spec"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_enqueue",
        "description": "Plan and persist a validated task graph without executing it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["spec"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_status",
        "description": "Inspect goal state, tasks, reviews, and artifacts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["goal_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_events",
        "description": "Read the ordered append-only audit trail of a goal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["goal_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_agents",
        "description": "Inspect agent profiles and task-type performance (social memory).",
        "inputSchema": {
            "type": "object",
            "properties": {"db": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "society_knowledge_add",
        "description": "Plant a long-term seed knowledge item with tags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "db": {"type": "string"},
            },
            "required": ["title", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_knowledge_search",
        "description": "Retrieve seed knowledge ranked by query-term and tag overlap.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "db": {"type": "string"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_genome_set",
        "description": "Save an auditable agent seed genome (identity, self-model, traits, risk policy).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "file": {"type": "string", "description": "Path to the genome JSON file."},
                "db": {"type": "string"},
            },
            "required": ["agent_id", "file"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_genome_show",
        "description": "Inspect an agent genome: role seed, self-model, traits, lineage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["agent_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_genome_recombine",
        "description": (
            "Create an auditable child genome candidate from two or more parents "
            "for one task type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "child_id": {"type": "string"},
                "parents": {"type": "array", "items": {"type": "string"}},
                "task_type": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["child_id", "parents", "task_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_experience_distill",
        "description": "Distill reviewed attempts of a goal into reusable experience lessons.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["goal_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_experience_list",
        "description": "Inspect accumulated task lessons by agent, task type, or goal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "task_type": {"type": "string"},
                "goal_id": {"type": "string"},
                "db": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "society_evaluate",
        "description": (
            "Compare a challenger with a champion on an immutable benchmark; "
            "produces a recommendation and never changes routing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["spec"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_promote",
        "description": "Explicitly promote a recommended challenger with operator identity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "by": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["run_id", "by"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_deployments",
        "description": "Inspect active task-type champions.",
        "inputSchema": {
            "type": "object",
            "properties": {"db": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "society_approve",
        "description": "Approve a paused write or execute tool call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string"},
                "by": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["approval_id", "by"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_reject",
        "description": "Reject a paused write or execute tool call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approval_id": {"type": "string"},
                "by": {"type": "string"},
                "db": {"type": "string"},
            },
            "required": ["approval_id", "by"],
            "additionalProperties": False,
        },
    },
    {
        "name": "society_scheduler_self_test",
        "description": "Prove five scheduler safety invariants on two independent connections.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "society_product_self_test",
        "description": "Run the install-time product readiness campaign.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "society_health",
        "description": "Inspect database readiness and optional worker liveness.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "db": {"type": "string"},
                "worker_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "society_metrics",
        "description": "Collect bounded operational counters.",
        "inputSchema": {
            "type": "object",
            "properties": {"db": {"type": "string"}},
            "additionalProperties": False,
        },
    },
]


def society_tools() -> list[dict[str, Any]]:
    return _TOOLS


def _cli_environment() -> dict[str, str]:
    env = dict(os.environ)
    project_dir = env.get(_PROJECT_DIR_ENV, "")
    if project_dir:
        src = os.path.join(project_dir, "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
    return env


def _run_cli(arguments: list[str], *, project_env: bool = False) -> dict[str, Any]:
    command = [sys.executable, "-m", "seed_society", *arguments, "--json"]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=_cli_environment() if project_env else None,
        timeout=300,
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _db(arguments: dict[str, Any], base: list[str]) -> list[str]:
    if arguments.get("db"):
        base.extend(["--db", str(arguments["db"])])
    return base


def _handle(name: str, arguments: dict[str, Any]) -> str:
    if name == "society_plugins":
        if arguments.get("describe"):
            base = ["plugins", "--describe"]
        else:
            base = ["plugins"]
            if arguments.get("consciousness"):
                base.extend(["--consciousness", str(arguments["consciousness"])])
        return json.dumps(_run_cli(base, project_env=True), ensure_ascii=False)

    if name == "society_demo":
        base = ["demo"]
        if arguments.get("goal_id"):
            base.extend(["--goal-id", str(arguments["goal_id"])])
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name in {"society_run_spec", "society_enqueue"}:
        base = [{"society_run_spec": "run", "society_enqueue": "enqueue"}[name], str(arguments["spec"])]
        if name == "society_run_spec" and arguments.get("allow_remote"):
            base.append("--allow-remote")
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name in {"society_status", "society_events"}:
        base = [{"society_status": "status", "society_events": "events"}[name], str(arguments["goal_id"])]
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_agents":
        return json.dumps(_run_cli(_db(arguments, ["agents"]), project_env=True), ensure_ascii=False)

    if name == "society_knowledge_add":
        base = ["knowledge", "add", str(arguments["title"]), str(arguments["content"])]
        for tag in arguments.get("tags") or []:
            base.extend(["--tag", str(tag)])
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_knowledge_search":
        base = ["knowledge", "search", str(arguments["query"])]
        for tag in arguments.get("tags") or []:
            base.extend(["--tag", str(tag)])
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_genome_set":
        base = ["genome", "set", str(arguments["agent_id"]), str(arguments["file"])]
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_genome_show":
        base = ["genome", "show", str(arguments["agent_id"])]
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_genome_recombine":
        base = [
            "genome",
            "recombine",
            str(arguments["child_id"]),
            "--parents",
            *[str(item) for item in arguments["parents"]],
            "--task-type",
            str(arguments["task_type"]),
        ]
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_experience_distill":
        base = ["experience", "distill", str(arguments["goal_id"])]
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_experience_list":
        base = ["experience", "list"]
        if arguments.get("agent_id"):
            base.extend(["--agent-id", str(arguments["agent_id"])])
        if arguments.get("task_type"):
            base.extend(["--task-type", str(arguments["task_type"])])
        if arguments.get("goal_id"):
            base.extend(["--goal-id", str(arguments["goal_id"])])
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_evaluate":
        base = ["evaluate", str(arguments["spec"])]
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_promote":
        base = ["promote", str(arguments["run_id"]), "--by", str(arguments["by"])]
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_deployments":
        return json.dumps(_run_cli(_db(arguments, ["deployments"]), project_env=True), ensure_ascii=False)

    if name in {"society_approve", "society_reject"}:
        verb = {"society_approve": "approve", "society_reject": "reject"}[name]
        base = [verb, str(arguments["approval_id"]), "--by", str(arguments["by"])]
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_scheduler_self_test":
        return json.dumps(_run_cli(["scheduler", "self-test"], project_env=True), ensure_ascii=False)

    if name == "society_product_self_test":
        return json.dumps(_run_cli(["product", "self-test"], project_env=True), ensure_ascii=False)

    if name == "society_health":
        base = ["health"]
        if arguments.get("worker_id"):
            base.extend(["--worker-id", str(arguments["worker_id"])])
        return json.dumps(_run_cli(_db(arguments, base), project_env=True), ensure_ascii=False)

    if name == "society_metrics":
        return json.dumps(_run_cli(_db(arguments, ["metrics"]), project_env=True), ensure_ascii=False)

    raise ValueError(f"unknown society tool: {name}")


def serve() -> None:
    """Run the MCP stdio JSON-RPC loop until stdin closes."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            result = {"tools": society_tools()}
        elif method == "tools/call":
            tool_name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                text = _handle(tool_name, arguments)
                result = {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                }
            except Exception as error:
                result = {
                    "content": [
                        {"type": "text", "text": f"{type(error).__name__}: {error}"}
                    ],
                    "isError": True,
                }
        elif method == "ping":
            result = {}
        else:
            result = {"error": {"code": -32601, "message": f"method not found: {method}"}}

        response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    serve()
