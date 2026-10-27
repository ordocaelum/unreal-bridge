"""MCP server (stdio) exposing Unreal Editor tools over Python remote execution.

Tool names use underscores (``ue_run_python``) rather than dots: the Claude API
only accepts ``[a-zA-Z0-9_-]`` in tool names.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import __version__, logs
from ._compat import FastMCP, ToolError
from .remote_exec import (
    EVALUATE_STATEMENT,
    EXECUTE_FILE,
    EXECUTE_STATEMENT,
    RemoteExecClient,
    RemoteExecConfig,
    RemoteExecError,
)
from .util import fit

log = logging.getLogger("unreal_bridge")

RUNTIME_MODULE = "_unreal_bridge_rt"
EDITOR_TOOLS = frozenset(
    {
        "list_assets",
        "get_defaults",
        "set_defaults",
        "datatable_read",
        "datatable_write",
        "console_command",
        "pie_start",
        "pie_stop",
        "pie_worlds",
        "run_python",
    }
)
LOCAL_TOOLS = frozenset({"log_tail", "status"})

_BOOTSTRAP = """\
import sys as _ub_sys, types as _ub_types
_ub_m = _ub_sys.modules.get({module!r})
if getattr(_ub_m, '__ub_version__', None) != {version!r}:
    _ub_m = _ub_types.ModuleType({module!r})
    _ub_m.__ub_version__ = {version!r}
    exec(compile({source!r}, '<unreal_bridge_rt>', 'exec'), _ub_m.__dict__)
    _ub_sys.modules[{module!r}] = _ub_m
del _ub_sys, _ub_types, _ub_m
"""


def _parse_eval_result(text: str) -> Any:
    """EvaluateStatement returns the repr of the value; our value is a JSON string."""
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        value = text
    return json.loads(value) if isinstance(value, str) else value


class Bridge:
    """Owns the remote-exec client and the editor-side runtime."""

    def __init__(self, config: RemoteExecConfig | None = None, *, max_output: int | None = None) -> None:
        self.client = RemoteExecClient(config or RemoteExecConfig.from_env())
        self.max_output = max_output if max_output is not None else int(os.environ.get("UNREAL_BRIDGE_MAX_OUTPUT", "60000"))
        self.project_dir = os.environ.get("UNREAL_BRIDGE_PROJECT_DIR") or None
        source = (Path(__file__).with_name("editor_runtime.py")).read_text(encoding="utf-8")
        self.runtime_version = hashlib.sha1(source.encode()).hexdigest()[:12]
        self._bootstrap = _BOOTSTRAP.format(module=RUNTIME_MODULE, version=self.runtime_version, source=source)
        self._runtime_generation = -1

    async def close(self) -> None:
        await self.client.close()

    async def _install_runtime(self) -> None:
        res = await self.client.execute(self._bootstrap, EXECUTE_FILE)
        if not res.success:
            raise ToolError(f"could not install the bridge runtime in the editor:\n{res.result}\n{res.stderr}".strip())
        self._runtime_generation = self.client.generation

    async def _evaluate(self, expression: str, timeout: float | None) -> Any:
        try:
            for attempt in (1, 2):
                if not self.client.connected:
                    await self.client.connect()
                if self._runtime_generation != self.client.generation:
                    await self._install_runtime()
                res = await self.client.execute(expression, EVALUATE_STATEMENT, timeout=timeout)
                if res.success:
                    return _parse_eval_result(res.result)
                if attempt == 1 and RUNTIME_MODULE in res.result:
                    self._runtime_generation = -1  # runtime vanished (editor restarted?); reinstall once
                    continue
                raise ToolError(f"editor error: {res.result}\n{res.stderr}".strip())
        except RemoteExecError as exc:
            raise ToolError(str(exc)) from None
        raise AssertionError("unreachable")

    async def call(self, name: str, args: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        """Run one editor-side tool in a single round trip."""
        expr = (
            f"__import__('sys').modules[{RUNTIME_MODULE!r}].call_json("
            f"{name!r}, {json.dumps(args, ensure_ascii=False)!r})"
        )
        envelope = await self._evaluate(expr, timeout)
        if not envelope.get("ok"):
            detail = envelope.get("traceback") or ""
            raise ToolError(f"{envelope.get('error')}\n{detail}".strip())
        return fit(envelope["result"], self.max_output)

    async def run_python(self, code: str, mode: str, timeout: float | None) -> dict[str, Any]:
        exec_mode = {"exec": EXECUTE_FILE, "statement": EXECUTE_STATEMENT, "eval": EVALUATE_STATEMENT}[mode]
        try:
            res = await self.client.execute(code, exec_mode, timeout=timeout)
        except RemoteExecError as exc:
            raise ToolError(str(exc)) from None
        return fit(res.to_dict(), self.max_output)

    async def status(self) -> dict[str, Any]:
        try:
            nodes = await self.client.discover()
        except OSError as exc:
            raise ToolError(f"discovery socket error: {exc}") from None
        cfg = self.client.config
        remote = self.client.remote if self.client.connected else None
        return {
            "bridge_version": __version__,
            "connected": remote is not None,
            "connected_to": remote.to_dict() if remote else None,
            "editors": [n.to_dict() for n in nodes],
            "multicast_group": f"{cfg.multicast_group[0]}:{cfg.multicast_group[1]}",
            "multicast_bind_address": cfg.multicast_bind_address,
            "project_filter": cfg.project_filter,
        }

    async def resolve_project_dir(self) -> str:
        if self.project_dir:
            return self.project_dir
        remote = self.client.remote
        if remote is None or not remote.project_root:
            nodes = [n for n in await self.client.discover(timeout=1.0) if n.matches(self.client.config.project_filter)]
            remote = nodes[0] if nodes else None
        if remote is None or not remote.project_root:
            raise ToolError(
                "unknown project directory: start the editor (it reports its project root) "
                "or set UNREAL_BRIDGE_PROJECT_DIR"
            )
        return remote.project_root

    async def log_tail(self, n: int, pattern: str | None, file: str | None) -> dict[str, Any]:
        directory = logs.logs_dir(await self.resolve_project_dir())
        try:
            path = logs.pick_log(directory, file)
            result = await asyncio.to_thread(logs.tail, path, n, pattern)
        except (OSError, ValueError) as exc:  # re.error is a ValueError
            raise ToolError(str(exc)) from None
        return fit(result, self.max_output)

    async def batch(self, calls: list[dict[str, Any]], stop_on_error: bool) -> dict[str, Any]:
        started = time.perf_counter()
        results: list[dict[str, Any] | None] = [None] * len(calls)
        editor_calls: list[tuple[int, dict[str, Any]]] = []
        local_calls: list[tuple[int, str, dict[str, Any]]] = []
        for i, item in enumerate(calls):
            name = _normalise_tool_name(str(item.get("tool", "")))
            args = item.get("args") or {}
            if name in EDITOR_TOOLS:
                editor_calls.append((i, {"tool": name, "args": args}))
            elif name in LOCAL_TOOLS:
                local_calls.append((i, name, args))
            else:
                results[i] = {"tool": name, "ok": False, "error": f"unknown or non-batchable tool {item.get('tool')!r}"}
        round_trips = 0
        if editor_calls:
            expr = (
                f"__import__('sys').modules[{RUNTIME_MODULE!r}].batch_json("
                f"{json.dumps([c for _, c in editor_calls], ensure_ascii=False)!r}, {bool(stop_on_error)!r})"
            )
            envelopes = await self._evaluate(expr, None)
            round_trips = 1
            for (i, c), env in zip(editor_calls, envelopes):
                results[i] = {"tool": c["tool"], **env}
        for i, name, args in local_calls:  # run after the editor round trip
            try:
                if name == "log_tail":
                    value = await self.log_tail(int(args.get("n", 100)), args.get("pattern"), args.get("file"))
                else:
                    value = await self.status()
                results[i] = {"tool": name, "ok": True, "result": value}
            except ToolError as exc:
                results[i] = {"tool": name, "ok": False, "error": str(exc)}
        return fit(
            {
                "results": results,
                "editor_round_trips": round_trips,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            },
            self.max_output,
        )


def _normalise_tool_name(name: str) -> str:
    for prefix in ("ue_", "ue."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


_bridge: Bridge | None = None


def get_bridge() -> Bridge:
    global _bridge
    if _bridge is None:
        _bridge = Bridge()
    return _bridge


def set_bridge(bridge: Bridge | None) -> None:
    """Swap the process-wide bridge (used by tests)."""
    global _bridge
    _bridge = bridge


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

mcp = FastMCP(
    "unreal-bridge",
    instructions=(
        "Controls a running Unreal Editor 5.5 through its Python remote execution. "
        "Asset paths are package paths like /Game/Blueprints/BP_Door. Prefer the "
        "dedicated tools; use ue_run_python for anything else (the `unreal` module is "
        "available). Use ue_batch to run several calls in one editor round trip. "
        "Editor calls run on the game thread, so long scripts stall the editor."
    ),
)

AssetPath = Annotated[str, Field(description="Asset path, e.g. /Game/Blueprints/BP_Door (the .BP_Door suffix is optional)")]
Timeout = Annotated[float | None, Field(description="Seconds to wait for the editor (default 120)", gt=0)]


@mcp.tool()
async def ue_status() -> dict[str, Any]:
    """Show which Unreal Editors are reachable and which one the bridge is connected to."""
    return await get_bridge().status()


@mcp.tool()
async def ue_run_python(
    code: Annotated[str, Field(description="Python source to run in the editor (`import unreal` to use the API)")],
    mode: Annotated[
        Literal["exec", "statement", "eval"],
        Field(description="exec: multi-line script (default). statement: one statement, its value is printed. eval: one expression, its repr is returned in `result`"),
    ] = "exec",
    timeout: Timeout = None,
) -> dict[str, Any]:
    """Run arbitrary Python inside the Unreal Editor.

    Returns {success, result, stdout, stderr, elapsed_ms}. On an exception
    `success` is false and `result` holds the traceback.
    """
    return await get_bridge().run_python(code, mode, timeout)


@mcp.tool()
async def ue_list_assets(
    path: Annotated[str, Field(description="Content folder to list, e.g. /Game or /Game/Maps")] = "/Game",
    class_filter: Annotated[
        str | list[str] | None,
        Field(description="Only these asset classes, e.g. Blueprint, DataTable, World, Material (case-insensitive)"),
    ] = None,
    recursive: Annotated[bool, Field(description="Include subfolders")] = True,
    limit: Annotated[int, Field(description="Maximum assets returned", ge=1, le=20000)] = 500,
) -> dict[str, Any]:
    """List assets under a content folder via the Asset Registry.

    Returns {path, count, assets: [{path, class, package}]}; `count` is the total
    before `limit` is applied.
    """
    return await get_bridge().call(
        "list_assets", {"path": path, "class_filter": class_filter, "recursive": recursive, "limit": limit}
    )


@mcp.tool()
async def ue_get_defaults(
    blueprint_path: AssetPath,
    props: Annotated[
        list[str] | None,
        Field(description="Property names. Blueprint variables use their name exactly as in the Blueprint (IsLocked); C++ properties use snake_case (base_eye_height). Omit to dump every readable property (best effort; may miss Blueprint variables)"),
    ] = None,
) -> dict[str, Any]:
    """Read class-default (CDO) property values of a Blueprint or class.

    Objects come back as path strings, enums as names, structs as UE text such
    as "(X=1.0,Y=2.0,Z=3.0)".
    """
    return await get_bridge().call("get_defaults", {"blueprint_path": blueprint_path, "props": props})


@mcp.tool()
async def ue_set_defaults(
    blueprint_path: AssetPath,
    values: Annotated[
        dict[str, Any],
        Field(description="{property: value}, named as in ue_get_defaults (Blueprint variables as in the Blueprint, e.g. IsLocked; C++ properties in snake_case). Values are coerced to the property's type: enums by name, structs from UE text or a {field: value} dict, objects/classes from an asset path"),
    ],
    compile: Annotated[bool, Field(description="Compile the Blueprint afterwards")] = False,
    save: Annotated[bool, Field(description="Save the asset afterwards")] = True,
) -> dict[str, Any]:
    """Set class-default (CDO) property values on a Blueprint, in one undoable transaction.

    Returns {changed: {prop: new value}, errors: {prop: reason}, compiled, saved}.
    """
    return await get_bridge().call(
        "set_defaults", {"blueprint_path": blueprint_path, "values": values, "compile": compile, "save": save}
    )


@mcp.tool()
async def ue_datatable_read(
    path: AssetPath,
    row_names: Annotated[list[str] | None, Field(description="Only these rows")] = None,
) -> dict[str, Any]:
    """Read a DataTable's rows as JSON (UE's export format: a list of objects, each with a "Name" key)."""
    return await get_bridge().call("datatable_read", {"path": path, "row_names": row_names})


@mcp.tool()
async def ue_datatable_write(
    path: AssetPath,
    rows: Annotated[
        list[dict[str, Any]] | dict[str, dict[str, Any]],
        Field(description='Rows as [{"Name": "Row1", "Field": 1}, ...] or {"Row1": {"Field": 1}}. Field names match ue_datatable_read output'),
    ],
    mode: Annotated[
        Literal["merge", "replace"],
        Field(description="merge: update or add the given rows and keep the others. replace: the table becomes exactly these rows"),
    ] = "merge",
    save: Annotated[bool, Field(description="Save the asset afterwards")] = True,
) -> dict[str, Any]:
    """Write DataTable rows (in an undoable transaction)."""
    return await get_bridge().call("datatable_write", {"path": path, "rows": rows, "mode": mode, "save": save})


@mcp.tool()
async def ue_console_command(
    command: Annotated[str, Field(description="Console command, e.g. 'stat fps' or 'r.ScreenPercentage 50'")],
    world: Annotated[
        str,
        Field(description="'editor' (default), 'server' (PIE server world), 'client' / 'client:N' (Nth PIE client), or 'pie:N'"),
    ] = "editor",
) -> dict[str, Any]:
    """Run a console command in the editor world or a PIE world.

    The command's own output goes to the log; read it with ue_log_tail.
    """
    return await get_bridge().call("console_command", {"command": command, "world": world})


@mcp.tool()
async def ue_pie_start(
    map: Annotated[str | None, Field(description="Map to load first, e.g. /Game/Maps/Level0. Omit to play the open map")] = None,
    clients: Annotated[
        int,
        Field(description="Client players. With listen_server the host is extra (clients=1 means host + 1 client). Without it, clients connect to a dedicated PIE server; 0 means standalone", ge=0, le=16),
    ] = 1,
    listen_server: Annotated[bool, Field(description="Host as a listen server (net mode Play As Listen Server)")] = True,
    single_process: Annotated[bool, Field(description="Run every player in the editor process")] = True,
) -> dict[str, Any]:
    """Start Play-In-Editor, setting the editor's player count to match.

    UE 5.5 limitation: Python cannot set the PIE net mode, so PIE runs with the Net
    Mode chosen in the editor's Play options menu (set it once by hand to Play As
    Listen Server; it is remembered). The result's settings_not_applied says so.

    Returns immediately; PIE begins on the next editor tick, so follow up with ue_pie_worlds.
    """
    return await get_bridge().call(
        "pie_start", {"map": map, "clients": clients, "listen_server": listen_server, "single_process": single_process}
    )


@mcp.tool()
async def ue_pie_stop() -> dict[str, Any]:
    """Stop the running Play-In-Editor session."""
    return await get_bridge().call("pie_stop", {})


@mcp.tool()
async def ue_pie_worlds() -> dict[str, Any]:
    """List PIE worlds, each tagged with its role: listen_server, dedicated_server, client or standalone."""
    return await get_bridge().call("pie_worlds", {})


@mcp.tool()
async def ue_log_tail(
    n: Annotated[int, Field(description="Number of lines", ge=1, le=5000)] = 100,
    pattern: Annotated[str | None, Field(description="Case-insensitive regex; only matching lines are returned, e.g. 'Error|Warning' or 'LogNet'")] = None,
    file: Annotated[str | None, Field(description="Log file name in Saved/Logs (default: the newest)")] = None,
) -> dict[str, Any]:
    """Tail the project's Saved/Logs log file.

    Reads from disk, so it works even while the editor is busy or has crashed.
    """
    return await get_bridge().log_tail(n, pattern, file)


@mcp.tool()
async def ue_batch(
    calls: Annotated[
        list[dict[str, Any]],
        Field(description='[{"tool": "ue_get_defaults", "args": {...}}, ...]. Any tool except ue_batch; tool names with or without the ue_ prefix'),
    ],
    stop_on_error: Annotated[bool, Field(description="Skip the remaining editor calls after the first failure")] = False,
) -> dict[str, Any]:
    """Run several tools in ONE editor round trip, in order.

    Returns {results: [{tool, ok, result | error}]} in call order. Editor calls run
    first, in one round trip; ue_log_tail/ue_status run after it. Inside a batch
    ue_run_python runs as a multi-line script, returning its stdout plus the value of a
    variable named `result` if the script sets one.
    """
    return await get_bridge().batch(calls, stop_on_error)


def main() -> None:
    # stdout carries the MCP protocol; log to stderr only.
    logging.basicConfig(
        level=os.environ.get("UNREAL_BRIDGE_LOG_LEVEL", "WARNING").upper(),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    mcp.run("stdio")


if __name__ == "__main__":
    main()
