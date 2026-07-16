import sys
import textwrap
import unittest

from agent_society_loop.domain import ToolRisk
from agent_society_loop.mcp import (
    MCPError,
    MCPProtocolError,
    MCPStdioClient,
    MCPTimeoutError,
    discover_mcp_tools,
)
from agent_society_loop.storage import SQLiteRepository
from agent_society_loop.tools import (
    ApprovalRequired,
    DefaultToolPolicy,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
)


FAKE_SERVER = textwrap.dedent(
    r"""
    import json
    import sys
    import time

    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake", "version": "1.0"},
            }
        elif method == "tools/list":
            cursor = message.get("params", {}).get("cursor")
            if cursor is None:
                result = {
                    "tools": [{
                        "name": "echo",
                        "description": "Echo text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    }],
                    "nextCursor": "page-2",
                }
            else:
                result = {
                    "tools": [{
                        "name": "dangerous",
                        "description": "Perform a state change",
                        "inputSchema": {"type": "object", "properties": {}},
                        "annotations": {"readOnlyHint": True},
                    }]
                }
        elif method == "tools/call":
            params = message.get("params", {})
            if params.get("name") == "fail":
                result = {"isError": True, "content": [{"type": "text", "text": "private"}]}
            else:
                result = {"structuredContent": {"echo": params.get("arguments", {}).get("text")}}
        elif method == "ignore":
            continue
        elif method == "large":
            result = {"value": "x" * 10000}
        else:
            result = {}
        if request_id is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
    """
)


class MCPStdioClientTests(unittest.TestCase):
    def client(self, script=FAKE_SERVER, **kwargs):
        return MCPStdioClient((sys.executable, "-u", "-c", script), **kwargs)

    def test_initializes_and_lists_all_tool_pages(self):
        with self.client() as client:
            server = client.initialize()
            tools = client.list_tools()

        self.assertEqual(server["protocolVersion"], "2025-11-25")
        self.assertEqual([tool["name"] for tool in tools], ["echo", "dangerous"])

    def test_calls_tool_and_returns_structured_content(self):
        with self.client() as client:
            client.initialize()
            result = client.call_tool("echo", {"text": "hello"})

        self.assertEqual(result, {"echo": "hello"})

    def test_tool_error_is_sanitized(self):
        with self.client() as client:
            client.initialize()
            with self.assertRaisesRegex(MCPError, "MCP tool failed") as caught:
                client.call_tool("fail", {})

        self.assertNotIn("private", str(caught.exception))

    def test_rejects_unsupported_negotiated_protocol(self):
        script = FAKE_SERVER.replace('"2025-11-25"', '"2024-11-05"', 1)
        with self.client(script) as client:
            with self.assertRaisesRegex(MCPProtocolError, "unsupported protocol"):
                client.initialize()

    def test_request_timeout_is_bounded(self):
        with self.client(request_timeout=0.05) as client:
            client.initialize()
            with self.assertRaises(MCPTimeoutError):
                client.request("ignore", {})
            with self.assertRaisesRegex(MCPError, "closed"):
                client.request("tools/list", {})

    def test_response_size_is_bounded(self):
        with self.client(max_message_bytes=512) as client:
            client.initialize()
            with self.assertRaisesRegex(MCPProtocolError, "message size"):
                client.request("large", {})

    def test_tool_pagination_is_bounded(self):
        script = textwrap.dedent(
            r"""
            import json
            import sys
            for line in sys.stdin:
                message = json.loads(line)
                if "id" not in message:
                    continue
                if message["method"] == "initialize":
                    result = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "serverInfo": {"name": "loop", "version": "1"},
                    }
                else:
                    result = {"tools": [], "nextCursor": "same"}
                print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
            """
        )
        with self.client(script, max_tool_pages=2) as client:
            client.initialize()
            with self.assertRaisesRegex(MCPProtocolError, "pagination"):
                client.list_tools()


class MCPToolAdapterTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")

    def tearDown(self):
        self.repository.close()

    def test_only_operator_classified_tools_are_registered(self):
        with MCPStdioClient((sys.executable, "-u", "-c", FAKE_SERVER)) as client:
            client.initialize()
            tools = discover_mcp_tools(
                client,
                "demo",
                {"echo": ToolRisk.READ},
            )
            registry = ToolRegistry(tools)
            result = ToolExecutor(
                registry, DefaultToolPolicy(), self.repository
            ).execute(
                "mcp.demo.echo",
                {"text": "hello"},
                ToolContext("goal", "task", "agent"),
            )

        self.assertEqual([schema["name"] for schema in registry.schemas()], ["mcp.demo.echo"])
        self.assertEqual(result.output, {"echo": "hello"})

    def test_local_write_risk_overrides_read_only_server_annotation(self):
        with MCPStdioClient((sys.executable, "-u", "-c", FAKE_SERVER)) as client:
            client.initialize()
            registry = ToolRegistry(
                discover_mcp_tools(
                    client,
                    "demo",
                    {"dangerous": ToolRisk.WRITE},
                )
            )
            executor = ToolExecutor(registry, DefaultToolPolicy(), self.repository)

            with self.assertRaises(ApprovalRequired):
                executor.execute(
                    "mcp.demo.dangerous",
                    {},
                    ToolContext("goal", "task", "agent"),
                )

    def test_invalid_namespace_is_rejected(self):
        with MCPStdioClient((sys.executable, "-u", "-c", FAKE_SERVER)) as client:
            client.initialize()
            with self.assertRaisesRegex(ValueError, "namespace"):
                discover_mcp_tools(client, "../demo", {"echo": ToolRisk.READ})


if __name__ == "__main__":
    unittest.main()
