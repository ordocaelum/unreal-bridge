"""Spawn the real server over stdio, as Claude Code does, and drive it with the MCP client."""

import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _field(obj, *names):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    raise AttributeError(names)


async def test_stdio_end_to_end(editor):
    host, port = editor.address
    env = {
        **os.environ,
        "UNREAL_BRIDGE_MULTICAST_GROUP": f"{host}:{port}",
        "UNREAL_BRIDGE_DISCOVERY_TIMEOUT": "2",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "unreal_bridge"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"ue_run_python", "ue_list_assets", "ue_batch", "ue_pie_start", "ue_log_tail"} <= tools

            res = await session.call_tool("ue_list_assets", {"path": "/Game/Data"})
            assert not _field(res, "is_error", "isError")
            payload = json.loads(res.content[0].text)
            assert payload["assets"][0]["path"] == "/Game/Data/DT_Items.DT_Items"

            res = await session.call_tool("ue_get_defaults", {"blueprint_path": "/Game/Missing"})
            assert _field(res, "is_error", "isError")
            assert "asset not found" in res.content[0].text
