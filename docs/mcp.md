# MCP Tool Integration

Agent Society Loop v0.5 supports the stable MCP `2025-11-25` protocol over
stdio. The client implements `initialize`, `notifications/initialized`,
paginated `tools/list`, and `tools/call`.

## Trust boundary

- The operator supplies the executable and arguments. Model output cannot alter
  the command.
- The process starts with `shell=False` and a minimal environment that excludes
  model and GitHub credentials.
- Requests have a deadline and both inbound and outbound JSON messages have a
  byte limit.
- Only explicitly supported stable protocol revisions are accepted.
- Every remote tool needs a local `ToolRisk` mapping. Unclassified tools are not
  registered.
- Server annotations never reduce the locally assigned risk.
- Adapted tools use `mcp.NAMESPACE.NAME` and pass through the normal schema,
  approval, trace, and action-budget controls.

## Library example

```python
from agent_society_loop.domain import ToolRisk
from agent_society_loop.mcp import MCPStdioClient, discover_mcp_tools
from agent_society_loop.tools import ToolRegistry

with MCPStdioClient(("python", "my_server.py")) as client:
    client.initialize()
    tools = discover_mcp_tools(
        client,
        "local",
        {"search": ToolRisk.READ, "update": ToolRisk.WRITE},
        allowed_names=("search", "update"),
    )
    registry = ToolRegistry(tools)
```

The caller must keep the client alive while adapted tools are in use. HTTP
transport, resources, sampling, elicitation, and experimental task methods are
outside the v0.5 scope.
