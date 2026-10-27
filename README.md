# unreal-bridge

An open MCP server that lets Claude Code (or any MCP client) drive a **running Unreal Editor 5.5**.
It needs no C++ and no engine plugin of its own: it talks to the editor's built-in **Python Editor Script Plugin** over that plugin's remote-execution protocol.

```
Claude Code ──stdio/MCP──▶ unreal-bridge ──UDP multicast + TCP──▶ Unreal Editor (Python Script Plugin)
```

- **Fast:** one persistent connection with automatic reconnect. Each tool call is one round trip, `ue_batch` runs many calls in one, and results are decoded as they stream in. Nothing polls.
- **Structured:** every tool returns JSON. Large results are trimmed to a budget, with a note saying what was cut.
- **Private:** no telemetry, and no network traffic except to the editor, over loopback by default.

> Tier 1 of a larger plan. Blueprint graph editing (Tier 2) will come from a C++ editor plugin; see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

- Unreal Engine **5.5** editor (Windows first; macOS/Linux should work too).
- Python **3.11+** on the same machine. This is a normal Python install, separate from the Python embedded in UE.
- [uv](https://docs.astral.sh/uv/) (recommended) or pip.

## Install

With uv (recommended):

```powershell
git clone https://github.com/ordocaelum/unreal-bridge H:\Tools\unreal-bridge
cd H:\Tools\unreal-bridge
uv sync
```

With pip:

```powershell
git clone https://github.com/ordocaelum/unreal-bridge H:\Tools\unreal-bridge
cd H:\Tools\unreal-bridge
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .
```

The only runtime dependency is the official `mcp` SDK. Both 1.x and 2.x are supported.

## Enable remote execution in UE 5.5

1. **Edit → Plugins**, search for **Python Editor Script Plugin**, enable it and restart the editor.
2. **Edit → Project Settings → Plugins → Python**. Under **Python Remote Execution**, tick **Enable Remote Execution?**
3. Note the other remote-execution settings. The bridge's defaults are below. If your editor shows different values, set the matching bridge variables so both sides agree:

   | UE setting | Bridge default | Bridge variable |
   |---|---|---|
   | Multicast Group Endpoint | `239.0.0.1:6766` | `UNREAL_BRIDGE_MULTICAST_GROUP` |
   | Multicast Bind Address | `127.0.0.1` | `UNREAL_BRIDGE_MULTICAST_BIND` |
   | Multicast Time-To-Live | `0` (this machine only) | `UNREAL_BRIDGE_MULTICAST_TTL` |

The checkbox is saved per project in `Config/DefaultEngine.ini`, so commit that file if your team should share it.

## Hook it up to Claude Code

Add this to `.mcp.json` in the folder you run Claude Code from (e.g. your UE project root):

```json
{
  "mcpServers": {
    "unreal": {
      "command": "uv",
      "args": ["run", "--directory", "H:/Tools/unreal-bridge", "unreal-bridge"],
      "env": {
        "UNREAL_BRIDGE_PROJECT": "Backrooms"
      }
    }
  }
}
```

With a pip venv, use `"command": "H:/Tools/unreal-bridge/.venv/Scripts/unreal-bridge.exe"` and `"args": []` instead.

Or from the command line:

```powershell
claude mcp add unreal -- uv run --directory H:/Tools/unreal-bridge unreal-bridge
```

Start the editor, run `/mcp` in Claude Code to check that `unreal` is connected, then ask Claude to call `ue_status`.
The bridge connects lazily on the first call, so it's fine to start Claude before the editor.

## Tools

Tool names use underscores (`ue_run_python`, not `ue.run_python`) because the Claude API rejects dots in tool names. Claude Code shows them as `mcp__unreal__ue_run_python`.

| Tool | Arguments | What it does |
|---|---|---|
| `ue_status` | – | Lists the reachable editors (project, engine version, node id) and which one the bridge is connected to. |
| `ue_run_python` | `code`, `mode` = `exec` \| `statement` \| `eval`, `timeout` | Runs any Python in the editor. Returns `{success, result, stdout, stderr, elapsed_ms}`; on an exception `success` is false and `result` holds the traceback. |
| `ue_list_assets` | `path="/Game"`, `class_filter`, `recursive=true`, `limit=500` | Lists assets via the Asset Registry: `{count, assets: [{path, class, package}]}`. |
| `ue_get_defaults` | `blueprint_path`, `props` | Reads class-default (CDO) values. Omit `props` to dump every readable property (best effort). |
| `ue_set_defaults` | `blueprint_path`, `values`, `compile=false`, `save=true` | Writes CDO values in one undoable transaction. Values are coerced to each property's type (see below). |
| `ue_datatable_read` | `path`, `row_names` | Reads DataTable rows in UE's JSON format (`[{"Name": ..., ...}]`). |
| `ue_datatable_write` | `path`, `rows`, `mode="merge"` \| `"replace"`, `save=true` | Writes rows (as a list with `Name`, or `{name: fields}`). `merge` keeps the rows you don't mention. |
| `ue_console_command` | `command`, `world="editor"` \| `server` \| `client[:N]` \| `pie:N` | Runs a console command in the editor world or a PIE world. |
| `ue_pie_start` | `map`, `clients=1`, `listen_server=true`, `single_process=true` | Starts Play-In-Editor. `clients=1` with a listen server means host + 1 client. Without a listen server the clients join a dedicated PIE server, and `clients=0` is standalone. |
| `ue_pie_stop` | – | Stops PIE. |
| `ue_pie_worlds` | – | Lists PIE worlds tagged `listen_server`, `dedicated_server`, `client` or `standalone`. |
| `ue_log_tail` | `n=100`, `pattern`, `file` | Tails `Saved/Logs/*.log` from disk, optionally filtered by a regex. Works even if the editor is hung. |
| `ue_batch` | `calls=[{tool, args}]`, `stop_on_error=false` | Runs several tools in **one editor round trip**, returning results in call order. |

**Value coercion** for `ue_set_defaults` (and returned values for `ue_get_defaults`):

- Enums are set and returned by name (`"Slide"`, `"SLIDE"` and `"EDoorMode::Slide"` all work).
- Structs take UE text such as `"(X=1,Y=2,Z=3)"` or a partial dict such as `{"z": 3}`, and come back as UE text.
- Object and class references take asset paths (`/Game/BP_Key.BP_Key_C` for a Blueprint class). `"None"` clears them.
- Names, Text, numbers, bools and arrays convert as you'd expect.

Property names are the Python (snake_case) names, e.g. `max_walk_speed`.

**Example batch:** set a default, read it back, and check the log in one call:

```json
{"calls": [
  {"tool": "ue_set_defaults", "args": {"blueprint_path": "/Game/Blueprints/BP_Door", "values": {"is_locked": true}}},
  {"tool": "ue_get_defaults", "args": {"blueprint_path": "/Game/Blueprints/BP_Door", "props": ["is_locked"]}},
  {"tool": "ue_log_tail", "args": {"n": 20, "pattern": "Error|Warning"}}
]}
```

Inside a batch, `run_python` runs a multi-line script. It returns its stdout, plus the value of a variable named `result` if the script sets one.

## Configuration

All settings are optional environment variables, set in the `env` block of `.mcp.json`:

| Variable | Default | Meaning |
|---|---|---|
| `UNREAL_BRIDGE_PROJECT` | – | Connect to the editor whose project name or root contains this text (useful when several editors are open). |
| `UNREAL_BRIDGE_PROJECT_DIR` | from the editor | Project folder for `ue_log_tail` (otherwise taken from the editor's discovery reply). |
| `UNREAL_BRIDGE_MULTICAST_GROUP` | `239.0.0.1:6766` | Must match UE's Multicast Group Endpoint. |
| `UNREAL_BRIDGE_MULTICAST_BIND` | `127.0.0.1` | Must match UE's Multicast Bind Address. |
| `UNREAL_BRIDGE_MULTICAST_TTL` | `0` | Multicast TTL. |
| `UNREAL_BRIDGE_COMMAND_ENDPOINT` | `127.0.0.1:0` | Where the bridge listens for the editor's command connection (port 0 = any free port). |
| `UNREAL_BRIDGE_COMMAND_TIMEOUT` | `120` | Seconds to wait for a command to finish. |
| `UNREAL_BRIDGE_DISCOVERY_TIMEOUT` | `3` | Seconds to wait for an editor to answer discovery. |
| `UNREAL_BRIDGE_CONNECT_TIMEOUT` | `5` | Seconds to wait for the editor to connect back. |
| `UNREAL_BRIDGE_HEARTBEAT` | `1` | Seconds between keep-alive pings. |
| `UNREAL_BRIDGE_MAX_OUTPUT` | `60000` | Character budget per tool result. |
| `UNREAL_BRIDGE_LOG_LEVEL` | `WARNING` | Bridge log level (written to stderr; `DEBUG` for protocol traces). |

## Troubleshooting

**"No Unreal Editor answered on 239.0.0.1:6766"**
- Check that the editor is running, the Python Editor Script Plugin is enabled, and **Enable Remote Execution?** is ticked. The setting applies once the editor has restarted.
- The multicast group and bind address must match on both sides. If you changed UE's *Multicast Bind Address* (e.g. to `0.0.0.0`), set `UNREAL_BRIDGE_MULTICAST_BIND` to the same value.
- **Windows multicast quirks:** with VPN, Hyper-V, WSL or Docker network adapters installed, Windows can route multicast through the wrong interface. Keeping *both* sides on bind address `127.0.0.1` with TTL `0` pins the traffic to loopback and avoids this. If it still fails, temporarily disconnect the VPN to confirm, then check `route print` for a `224.0.0.0` route on a virtual adapter.
- Another program holding UDP 6766 exclusively, without address reuse, blocks discovery. Look for it with `netstat -ano -p udp | findstr 6766`.

**"Editor … did not connect back"**
The editor heard the bridge but couldn't open the TCP command connection, which is almost always a firewall or antivirus issue. The bridge listens on `127.0.0.1` only. Allow `UnrealEditor.exe` and your `python.exe` on *private* networks if Windows Defender Firewall prompted you, or check your antivirus's "network protection" rules.

**Commands time out or the editor freezes**
Editor Python runs on the game thread, so a long script freezes the editor until it finishes. When a call times out, the bridge drops the connection (a late reply would otherwise be read as the answer to the next call), and the next call reconnects. Raise `UNREAL_BRIDGE_COMMAND_TIMEOUT` for long jobs.

**A modal dialog appeared**
Commands are sent in *unattended* mode, which suppresses most dialogs. Some editor actions still prompt anyway, for example loading a map while the current one has unsaved changes. Save first, or click through once.

**Multiple editors open**
Set `UNREAL_BRIDGE_PROJECT` to pick one, and run `ue_status` to see them all.

**Debugging**
Set `UNREAL_BRIDGE_LOG_LEVEL=DEBUG` and check the MCP server logs (Claude Code: `claude --debug`).

## Security

Remote execution means *any process on this machine* that speaks the protocol can run Python in your editor. That is Unreal's own model, and the bridge doesn't widen it: discovery uses TTL 0 (the packets never leave the machine), and the command channel listens on loopback only. Don't change the bind address or TTL to expose the editor to a network you don't control.

## Development

```powershell
uv sync --extra test
uv run pytest
```

The tests run against a fake editor (`tests/fake_editor.py`) that speaks the same protocol and executes commands against a stub `unreal` module, so the full stack runs without Unreal. CI runs on Linux and Windows with Python 3.11 and 3.13, and with both MCP SDK generations.

## License

MIT. See [LICENSE](LICENSE).
