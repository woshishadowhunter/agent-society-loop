"""Bounded MCP stdio tool discovery and invocation."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .domain import ToolRisk


MCP_PROTOCOL_VERSION = "2025-11-25"


class MCPError(RuntimeError):
    """Base class for sanitized MCP failures."""


class MCPProtocolError(MCPError):
    """Raised when a peer violates the supported MCP contract."""


class MCPTimeoutError(MCPError):
    """Raised when an MCP request exceeds its configured deadline."""


class MCPStdioClient:
    """A synchronous, single-flight JSON-RPC client for operator-run servers."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path | None = None,
        request_timeout: float = 10.0,
        max_message_bytes: int = 1_048_576,
        max_tool_pages: int = 100,
        protocol_versions: Sequence[str] = (MCP_PROTOCOL_VERSION,),
    ):
        normalized = tuple(str(part) for part in command)
        if not normalized or any(not part for part in normalized):
            raise ValueError("MCP server command must not be empty")
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if max_message_bytes < 256:
            raise ValueError("max_message_bytes must be at least 256")
        if max_tool_pages < 1:
            raise ValueError("max_tool_pages must be positive")
        versions = tuple(str(version) for version in protocol_versions)
        if not versions or any(not version for version in versions):
            raise ValueError("protocol_versions must not be empty")

        self.command = normalized
        self.request_timeout = float(request_timeout)
        self.max_message_bytes = int(max_message_bytes)
        self.max_tool_pages = int(max_tool_pages)
        self.protocol_versions = versions
        self._responses: queue.Queue[dict[str, Any] | Exception] = queue.Queue()
        self._request_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._initialized = False
        self._closed = False
        self._process = subprocess.Popen(
            normalized,
            cwd=str(cwd) if cwd is not None else None,
            env=_minimal_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def __enter__(self) -> MCPStdioClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            raise MCPProtocolError("MCP client is already initialized")
        result = self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_versions[0],
                "capabilities": {},
                "clientInfo": {"name": "agent-society-loop", "version": "0.6.0"},
            },
        )
        if not isinstance(result, dict):
            raise MCPProtocolError("invalid initialize result")
        negotiated = result.get("protocolVersion")
        if negotiated not in self.protocol_versions:
            raise MCPProtocolError("unsupported protocol negotiated by MCP server")
        self.notify("notifications/initialized", {})
        self._initialized = True
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        self._require_initialized()
        discovered: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(self.max_tool_pages):
            params = {"cursor": cursor} if cursor is not None else {}
            result = self.request("tools/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise MCPProtocolError("invalid tools/list result")
            for tool in result["tools"]:
                if not isinstance(tool, dict):
                    raise MCPProtocolError("invalid tool definition")
                discovered.append(tool)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return discovered
            if not isinstance(next_cursor, str) or not next_cursor:
                raise MCPProtocolError("invalid tools/list cursor")
            if next_cursor in seen_cursors:
                raise MCPProtocolError("MCP tool pagination cursor repeated")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise MCPProtocolError("MCP tool pagination exceeded configured limit")

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        self._require_initialized()
        if not name.strip() or not isinstance(arguments, Mapping):
            raise ValueError("tool name and arguments are required")
        result = self.request(
            "tools/call", {"name": name, "arguments": dict(arguments)}
        )
        if not isinstance(result, dict):
            raise MCPProtocolError("invalid tools/call result")
        if result.get("isError") is True:
            raise MCPError("MCP tool failed")
        if "structuredContent" in result:
            return result["structuredContent"]
        return result.get("content", [])

    def request(self, method: str, params: Mapping[str, Any]) -> Any:
        if self._closed:
            raise MCPError("MCP client is closed")
        if not method.strip() or not isinstance(params, Mapping):
            raise ValueError("method and params are required")
        with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            try:
                response = self._responses.get(timeout=self.request_timeout)
            except queue.Empty:
                self.close()
                raise MCPTimeoutError("MCP request timed out") from None
            if isinstance(response, Exception):
                raise response
            if response.get("id") != request_id:
                raise MCPProtocolError("MCP response id mismatch")
            if "error" in response:
                raise MCPError("MCP server returned a JSON-RPC error")
            if "result" not in response:
                raise MCPProtocolError("MCP response has no result")
            return response["result"]

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write(
            {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            self._process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=1.0)
        self._reader.join(timeout=1.0)
        if self._process.stdout is not None:
            self._process.stdout.close()

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise MCPProtocolError("MCP client is not initialized")

    def _write(self, message: dict[str, Any]) -> None:
        payload = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(payload) > self.max_message_bytes:
            raise MCPProtocolError("MCP message size exceeds configured limit")
        if self._process.stdin is None:
            raise MCPError("MCP server input is unavailable")
        with self._write_lock:
            try:
                self._process.stdin.write(payload)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError):
                raise MCPError("MCP server connection closed") from None

    def _read_stdout(self) -> None:
        if self._process.stdout is None:
            self._responses.put(MCPError("MCP server output is unavailable"))
            return
        while True:
            line = self._process.stdout.readline(self.max_message_bytes + 1)
            if not line:
                if not self._closed:
                    self._responses.put(MCPError("MCP server connection closed"))
                return
            if len(line) > self.max_message_bytes:
                self._responses.put(
                    MCPProtocolError("MCP message size exceeds configured limit")
                )
                return
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._responses.put(MCPProtocolError("invalid MCP JSON message"))
                continue
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                self._responses.put(MCPProtocolError("invalid MCP JSON-RPC message"))
                continue
            if "id" in message and ("result" in message or "error" in message):
                self._responses.put(message)
            elif "id" in message and "method" in message:
                self._write_server_error(message["id"])

    def _write_server_error(self, request_id: Any) -> None:
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not supported"},
                }
            )
        except MCPError:
            return


@dataclass(frozen=True, slots=True)
class MCPToolAdapter:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: ToolRisk
    remote_name: str
    client: MCPStdioClient

    def invoke(self, arguments: dict[str, Any]) -> Any:
        return self.client.call_tool(self.remote_name, arguments)


def discover_mcp_tools(
    client: MCPStdioClient,
    namespace: str,
    risk_by_name: Mapping[str, ToolRisk],
    *,
    allowed_names: Sequence[str] | None = None,
) -> list[MCPToolAdapter]:
    """Adapt only explicitly allowed and locally risk-classified MCP tools."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", namespace):
        raise ValueError("MCP namespace must be a safe short name")
    allowed = set(allowed_names) if allowed_names is not None else None
    adapted: list[MCPToolAdapter] = []
    seen: set[str] = set()
    for definition in client.list_tools():
        remote_name = definition.get("name")
        description = definition.get("description")
        schema = definition.get("inputSchema")
        if not isinstance(remote_name, str) or not remote_name.strip():
            raise MCPProtocolError("MCP tool name must not be empty")
        if remote_name in seen:
            raise MCPProtocolError("duplicate MCP tool name")
        seen.add(remote_name)
        if allowed is not None and remote_name not in allowed:
            continue
        risk = risk_by_name.get(remote_name)
        if risk is None:
            continue
        if not isinstance(risk, ToolRisk):
            raise ValueError(f"tool risk must be a ToolRisk: {remote_name}")
        if not isinstance(description, str) or not description.strip():
            raise MCPProtocolError("MCP tool description must not be empty")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise MCPProtocolError("MCP tool input schema must describe an object")
        adapted.append(
            MCPToolAdapter(
                name=f"mcp.{namespace}.{remote_name}",
                description=description.strip(),
                input_schema=dict(schema),
                risk=risk,
                remote_name=remote_name,
                client=client,
            )
        )
    return adapted


def _minimal_environment() -> dict[str, str]:
    allowed = (
        "COMSPEC",
        "HOME",
        "LANG",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    return {name: os.environ[name] for name in allowed if name in os.environ}
