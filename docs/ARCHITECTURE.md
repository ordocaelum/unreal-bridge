# Architecture

```
┌──────────────┐ stdio  ┌───────────────────────── unreal-bridge ─────────────────────────┐        ┌──────────── Unreal Editor 5.5 ────────────┐
│ Claude Code  │◀──────▶│ server.py (MCP tools) ─▶ Bridge ─▶ remote_exec.RemoteExecClient │◀─UDP──▶│ Python Script Plugin remote execution     │
│ (MCP client) │  MCP   │        logs.py (reads Saved/Logs directly)   util.fit (budget)  │◀─TCP──▶│   └ _unreal_bridge_rt (editor_runtime.py) │
└──────────────┘        └─────────────────────────────────────────────────────────────────┘        └───────────────────────────────────────────┘
```

## Protocol notes (UE Python remote execution)

Every message on both channels is a JSON object
`{"version": 1, "magic": "ue_py", "type", "source", "dest"?, "data"?}`. `source` and `dest` are node ids (UUIDs).

| Channel | Message | Direction | `data` |
|---|---|---|---|
| UDP multicast `239.0.0.1:6766`, TTL 0 | `ping` | client → all | – |
| | `pong` | editor → all | `user, machine, engine_version, engine_root, project_root, project_name` |
| | `open_connection` | client → editor | `command_ip, command_port` |
| | `close_connection` | client → editor | – |
| TCP | `command` | client → editor | `command, unattended, exec_mode` |
| | `command_result` | editor → client | `success, command, result, output: [{type, output}]` |

- **The editor connects to the client.** The client opens a listening socket and announces it in `open_connection`. The bridge listens on `127.0.0.1` with an ephemeral port, so several clients never collide.
- TCP messages are **bare JSON with no framing**. `JsonStreamDecoder` parses the stream incrementally with `raw_decode` and yields each message the moment its last byte arrives, however the bytes were split across reads.
- `exec_mode`:
  - `ExecuteFile` runs a file path or a literal multi-line script.
  - `ExecuteStatement` runs one statement and prints its value.
  - `EvaluateStatement` evaluates one expression and returns its `repr` in `result`.
- **Keep-alive:** like Epic's reference client, the bridge pings once a second while it runs, so the editor keeps seeing it as a live node and its node list stays fresh. This is a single datagram, not a poll for results.
- **Windows binding:** on Windows the discovery socket binds to the bind address, as UE does. Elsewhere it binds to the wildcard address, because a socket bound to an interface address misses datagrams sent to the group. Both sides set `SO_REUSEADDR`, because the editor and the bridge share port 6766.
- **Unicast mode:** if the group address isn't multicast, discovery sends pings straight to that address. The test suite uses this to stay independent of the host's multicast routing; a separate test covers real multicast.

## Performance choices

- **Persistent connection.** Discovery and connect happen once. Each later call is a single TCP write and read, and `TCP_NODELAY` is set.
- **Editor-side runtime.** On each new connection the bridge installs `editor_runtime.py` into the editor's `sys.modules` as `_unreal_bridge_rt`. That is one `ExecuteFile`, and it's skipped when the same version is already loaded. After that every tool is a single `EvaluateStatement` that calls `call_json(name, kwargs_json)`:
  - Results come back as JSON in `result`, not printed to stdout, so the Output Log stays clean and there's no stdout scraping.
  - Arguments travel as a JSON string literal, so there's no code generation from user input.
- **Batching.** `ue_batch` sends one `batch_json` expression for any number of editor calls. Local tools (`ue_log_tail`, `ue_status`) run in the server process after that round trip.
- **Reconnect without double execution.**
  - If a send fails, nothing reached the editor, so the command is resent once on a fresh connection.
  - If a failure happens after the send, the command is *not* retried, because editor code is rarely idempotent. The connection is dropped and the next call reconnects.
  - A reader already at EOF (the editor closed the socket while the bridge was idle) is detected before sending, so an editor restart costs no failed call.
  - If the runtime has vanished from the editor, it's reinstalled once and the call retried.
- **Timeouts drop the connection.** A late reply would otherwise be read as the answer to the next command.
- **Local log tailing.** `logs.tail` reads the log backwards in 64 KB blocks and stops as soon as it has `n` matching lines.
- **Output budget.** `util.fit` keeps results under `UNREAL_BRIDGE_MAX_OUTPUT` characters by repeatedly halving the largest list or string, and adds a `_truncated` note describing what was cut. For stdout and log lines it keeps the tail, which is where the latest output is.

## Where Tier 2 plugs in

Blueprint *graph* editing (nodes, pins, wiring) isn't reachable from UE's Python API in 5.5, so Tier 2 adds a C++ editor plugin, based on the MIT-licensed `chongdashu/unreal-mcp`. The plan:

1. The C++ plugin exposes `UFUNCTION(BlueprintCallable)` graph operations on a `UBlueprintFunctionLibrary` (or an editor subsystem). UE reflects these into Python automatically as `unreal.<LibraryName>.<function>`.
2. Its Python surface is called from new functions in `editor_runtime.py` and registered in `TOOLS`. `server.py` gains matching thin `ue_*` tools.

The transport, connection management, batching, truncation and tests stay as they are, and graph edits become batchable with everything else. If the plugin later wants its own socket for speed, add a second transport class beside `RemoteExecClient` with the same `execute` contract, and `Bridge` can choose between them per tool.

## Files

| File | Role |
|---|---|
| `unreal_bridge/remote_exec.py` | Protocol client: discovery, connect-back, commands, reconnect, timeouts. |
| `unreal_bridge/editor_runtime.py` | Runs **inside** the editor. Holds the tool implementations against the `unreal` API, plus value conversion. |
| `unreal_bridge/server.py` | MCP tools, the `Bridge` (runtime install, call and batch), and the entry point. |
| `unreal_bridge/logs.py` | `Saved/Logs` tailing. |
| `unreal_bridge/util.py` | Result-size budget. |
| `tests/fake_editor.py`, `tests/fake_unreal.py` | Protocol-level fake editor and a stub `unreal` module. |
