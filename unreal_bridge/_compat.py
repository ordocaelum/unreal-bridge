"""Support both MCP SDK generations: 1.x (``FastMCP``) and 2.x (``MCPServer``)."""

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

__all__ = ["FastMCP", "ToolError"]
