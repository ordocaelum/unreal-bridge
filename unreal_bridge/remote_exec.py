"""Client for the Unreal Editor's Python remote-execution protocol.

Reimplemented from the protocol (not vendored). The protocol has two channels:

* **Discovery** – UDP multicast (default ``239.0.0.1:6766``, TTL 0 so it never
  leaves the machine). Clients send ``ping``; every editor with Remote Execution
  enabled answers ``pong`` with its node id and project info. ``open_connection``
  and ``close_connection`` are also sent here, addressed to one editor node.
* **Commands** – TCP. The *client* listens and the *editor connects back* to the
  ``command_ip``/``command_port`` carried by ``open_connection``. Over that
  socket the client sends ``command`` messages and receives ``command_result``.

Every message is a JSON object::

    {"version": 1, "magic": "ue_py", "type": "...", "source": "<node id>",
     "dest": "<node id>", "data": {...}}

TCP messages are bare JSON objects with no length prefix or delimiter, so the
reader decodes the byte stream incrementally (:class:`JsonStreamDecoder`) and
yields each object the moment its closing brace arrives.

If the group address is *not* a multicast address, discovery runs in unicast
mode (pings go straight to that ``host:port``). The tests use this mode to talk
to a fake editor without depending on multicast routing.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import ipaddress
import json
import logging
import os
import socket
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
PROTOCOL_MAGIC = "ue_py"

TYPE_PING = "ping"
TYPE_PONG = "pong"
TYPE_OPEN_CONNECTION = "open_connection"
TYPE_CLOSE_CONNECTION = "close_connection"
TYPE_COMMAND = "command"
TYPE_COMMAND_RESULT = "command_result"

# exec_mode values understood by the Python Script Plugin.
EXECUTE_FILE = "ExecuteFile"  # a file path, or a literal multi-statement script
EXECUTE_STATEMENT = "ExecuteStatement"  # one statement; expression results are printed
EVALUATE_STATEMENT = "EvaluateStatement"  # one expression; its repr comes back as `result`
EXEC_MODES = (EXECUTE_FILE, EXECUTE_STATEMENT, EVALUATE_STATEMENT)

DEFAULT_MULTICAST_GROUP = ("239.0.0.1", 6766)
DEFAULT_MULTICAST_BIND_ADDRESS = "127.0.0.1"
DEFAULT_COMMAND_HOST = "127.0.0.1"


class RemoteExecError(Exception):
    """Base class for remote-execution failures."""


class EditorNotFound(RemoteExecError):
    """No editor answered discovery, or the chosen editor never connected back."""


class ConnectionLost(RemoteExecError):
    """The command connection dropped; the next call reconnects automatically."""


class CommandTimeout(RemoteExecError):
    """The editor did not answer in time. The connection is reset because a late
    reply would otherwise be read as the answer to the next command."""


# --------------------------------------------------------------------------- #
# Wire format
# --------------------------------------------------------------------------- #


def encode_message(
    type_: str, source: str, dest: str | None = None, data: Mapping[str, Any] | None = None
) -> bytes:
    msg: dict[str, Any] = {
        "version": PROTOCOL_VERSION,
        "magic": PROTOCOL_MAGIC,
        "type": type_,
        "source": source,
    }
    if dest:
        msg["dest"] = dest
    if data is not None:
        msg["data"] = dict(data)
    return json.dumps(msg, ensure_ascii=False).encode("utf-8")


def decode_message(obj: Any) -> dict[str, Any] | None:
    """Return the message if it is a well-formed protocol message, else None."""
    if not isinstance(obj, dict):
        return None
    if obj.get("version") != PROTOCOL_VERSION or obj.get("magic") != PROTOCOL_MAGIC:
        return None
    if not isinstance(obj.get("type"), str) or not isinstance(obj.get("source"), str):
        return None
    return obj


class JsonStreamDecoder:
    """Incrementally split a byte stream into concatenated JSON values.

    Handles UTF-8 sequences and JSON values split across reads, and tolerates
    whitespace or NUL separators between values.
    """

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._json = json.JSONDecoder()

    def feed(self, data: bytes) -> list[Any]:
        self._buf += self._utf8.decode(data)
        out: list[Any] = []
        pos = 0
        n = len(self._buf)
        while True:
            while pos < n and self._buf[pos] in " \t\r\n\x00":
                pos += 1
            if pos >= n:
                break
            try:
                value, end = self._json.raw_decode(self._buf, pos)
            except json.JSONDecodeError:
                if self._buf[pos] not in "{[":
                    # Garbage that can never become valid JSON: skip to the next object.
                    nxt = self._buf.find("{", pos + 1)
                    pos = n if nxt < 0 else nxt
                    continue
                break  # incomplete; wait for more bytes
            out.append(value)
            pos = end
        self._buf = self._buf[pos:]
        return out

    @property
    def pending(self) -> int:
        return len(self._buf)


# --------------------------------------------------------------------------- #
# Configuration and data types
# --------------------------------------------------------------------------- #


def _parse_endpoint(value: str, default_port: int) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep:
        return value, default_port
    return host, int(port)


@dataclass(frozen=True)
class RemoteExecConfig:
    """Settings; the defaults match UE 5.5's Python plugin defaults.

    ``multicast_group`` and ``multicast_bind_address`` must match the editor's
    Project Settings → Plugins → Python → Python Remote Execution values.
    """

    multicast_group: tuple[str, int] = DEFAULT_MULTICAST_GROUP
    multicast_bind_address: str = DEFAULT_MULTICAST_BIND_ADDRESS
    multicast_ttl: int = 0
    command_host: str = DEFAULT_COMMAND_HOST
    command_port: int = 0  # 0 = ephemeral port, so several clients never collide
    discovery_timeout: float = 3.0
    connect_timeout: float = 5.0
    command_timeout: float = 120.0
    heartbeat_interval: float = 1.0  # editors drop command connections whose client stops pinging
    node_timeout: float = 5.0
    project_filter: str | None = None  # pick the editor whose project name/root contains this

    @property
    def is_multicast(self) -> bool:
        try:
            return ipaddress.ip_address(self.multicast_group[0]).is_multicast
        except ValueError:
            return False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RemoteExecConfig":
        env = os.environ if env is None else env
        kw: dict[str, Any] = {}
        if v := env.get("UNREAL_BRIDGE_MULTICAST_GROUP"):
            kw["multicast_group"] = _parse_endpoint(v, DEFAULT_MULTICAST_GROUP[1])
        if v := env.get("UNREAL_BRIDGE_MULTICAST_BIND"):
            kw["multicast_bind_address"] = v
        if v := env.get("UNREAL_BRIDGE_MULTICAST_TTL"):
            kw["multicast_ttl"] = int(v)
        if v := env.get("UNREAL_BRIDGE_COMMAND_ENDPOINT"):
            kw["command_host"], kw["command_port"] = _parse_endpoint(v, 0)
        for name, key in (
            ("discovery_timeout", "UNREAL_BRIDGE_DISCOVERY_TIMEOUT"),
            ("connect_timeout", "UNREAL_BRIDGE_CONNECT_TIMEOUT"),
            ("command_timeout", "UNREAL_BRIDGE_COMMAND_TIMEOUT"),
            ("heartbeat_interval", "UNREAL_BRIDGE_HEARTBEAT"),
        ):
            if v := env.get(key):
                kw[name] = float(v)
        if v := env.get("UNREAL_BRIDGE_PROJECT"):
            kw["project_filter"] = v
        return cls(**kw)


@dataclass
class EditorNode:
    """An editor instance seen during discovery (from its ``pong``)."""

    node_id: str
    user: str = ""
    machine: str = ""
    engine_version: str = ""
    engine_root: str = ""
    project_root: str = ""
    project_name: str = ""
    last_seen: float = field(default=0.0, compare=False)

    @classmethod
    def from_pong(cls, source: str, data: Mapping[str, Any] | None) -> "EditorNode":
        data = data or {}
        return cls(
            node_id=source,
            user=str(data.get("user", "")),
            machine=str(data.get("machine", "")),
            engine_version=str(data.get("engine_version", "")),
            engine_root=str(data.get("engine_root", "")),
            project_root=str(data.get("project_root", "")),
            project_name=str(data.get("project_name", "")),
            last_seen=time.monotonic(),
        )

    def matches(self, needle: str | None) -> bool:
        if not needle:
            return True
        n = needle.lower()
        return n in self.project_name.lower() or n in self.project_root.lower()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("last_seen")
        return d


@dataclass
class CommandResult:
    success: bool
    result: str
    output: list[dict[str, str]]
    exec_mode: str
    elapsed_ms: float

    # UE terminates each output entry with "\r\n"; strip it so entries join cleanly.
    @property
    def stdout(self) -> str:
        return "\n".join(o.get("output", "").rstrip("\r\n") for o in self.output if o.get("type") == "Info")

    @property
    def stderr(self) -> str:
        return "\n".join(o.get("output", "").rstrip("\r\n") for o in self.output if o.get("type") != "Info")

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "result": self.result,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


# --------------------------------------------------------------------------- #
# Sockets
# --------------------------------------------------------------------------- #


def _make_discovery_socket(cfg: RemoteExecConfig) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        if not cfg.is_multicast:
            s.bind((cfg.multicast_bind_address, 0))
            s.setblocking(False)
            return s
        # The editor binds the same port on the same machine, so share it.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if sys.platform != "win32" and hasattr(socket, "SO_REUSEPORT"):
            with contextlib.suppress(OSError):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        group, port = cfg.multicast_group
        # Windows cannot bind to a multicast address and delivers group traffic to a
        # socket bound on the interface address (this matches what UE does).
        # Elsewhere a socket bound to an interface address would miss datagrams
        # addressed to the group, so bind the wildcard address.
        s.bind((cfg.multicast_bind_address if sys.platform == "win32" else "", port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, cfg.multicast_ttl)
        iface = socket.inet_aton(cfg.multicast_bind_address)
        with contextlib.suppress(OSError):
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + iface)
        except OSError:
            s.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(group) + socket.inet_aton("0.0.0.0"),
            )
        s.setblocking(False)
        return s
    except Exception:
        s.close()
        raise


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, client: "RemoteExecClient") -> None:
        self._client = client

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            obj = json.loads(data.decode("utf-8", errors="replace"))
        except ValueError:
            return
        msg = decode_message(obj)
        if msg is not None:
            self._client._on_discovery_message(msg)

    def error_received(self, exc: Exception) -> None:  # pragma: no cover - platform noise
        log.debug("discovery socket error: %s", exc)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class RemoteExecClient:
    """Async client with a persistent command connection and automatic reconnect.

    Commands are serialised (the protocol is strictly request/response). Any
    failure drops the connection; the next command rediscovers and reconnects,
    so an editor restart costs one failed call at most.
    """

    def __init__(self, config: RemoteExecConfig | None = None) -> None:
        self.config = config or RemoteExecConfig()
        self.node_id = str(uuid.uuid4())
        self.nodes: dict[str, EditorNode] = {}
        self.remote: EditorNode | None = None
        self.generation = 0  # bumped on every successful connect
        self._udp: asyncio.DatagramTransport | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._node_seen = asyncio.Event()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._decoder = JsonStreamDecoder()
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        if self._udp is not None:
            return
        loop = asyncio.get_running_loop()
        sock = _make_discovery_socket(self.config)
        self._udp, _ = await loop.create_datagram_endpoint(lambda: _DiscoveryProtocol(self), sock=sock)
        if self.config.heartbeat_interval > 0:
            self._heartbeat = asyncio.create_task(self._heartbeat_loop(), name="unreal-bridge-heartbeat")

    async def close(self) -> None:
        await self.disconnect()
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
            self._heartbeat = None
        if self._udp is not None:
            self._udp.close()
            self._udp = None

    async def __aenter__(self) -> "RemoteExecClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        # at_eof() catches an editor that closed the socket while we were idle,
        # so the next command reconnects instead of failing.
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and self._reader is not None
            and not self._reader.at_eof()
        )

    # -- discovery ---------------------------------------------------------- #

    def _send_udp(self, type_: str, dest: str | None = None, data: Mapping[str, Any] | None = None) -> None:
        if self._udp is None:
            raise RemoteExecError("client not started")
        self._udp.sendto(encode_message(type_, self.node_id, dest, data), self.config.multicast_group)

    def _on_discovery_message(self, msg: dict[str, Any]) -> None:
        if msg["source"] == self.node_id or msg["type"] != TYPE_PONG:
            return  # our own looped-back packets, and other clients' pings
        if msg.get("dest") not in (None, self.node_id):
            return
        node = EditorNode.from_pong(msg["source"], msg.get("data"))
        self.nodes[node.node_id] = node
        self._node_seen.set()

    def _prune_nodes(self) -> None:
        cutoff = time.monotonic() - self.config.node_timeout
        for nid in [n for n, node in self.nodes.items() if node.last_seen < cutoff]:
            del self.nodes[nid]

    async def _heartbeat_loop(self) -> None:
        while True:
            with contextlib.suppress(OSError, RemoteExecError):
                self._send_udp(TYPE_PING)
            self._prune_nodes()
            await asyncio.sleep(self.config.heartbeat_interval)

    async def discover(self, timeout: float | None = None) -> list[EditorNode]:
        """Return the editors currently answering pings (fresh ones are reused)."""
        await self.start()
        self._prune_nodes()
        if not self.nodes:
            self._node_seen.clear()
            self._send_udp(TYPE_PING)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._node_seen.wait(),
                    self.config.discovery_timeout if timeout is None else timeout,
                )
            if self.nodes:
                await asyncio.sleep(0.05)  # let other editors' pongs land too
        return sorted(self.nodes.values(), key=lambda n: (n.project_name, n.node_id))

    # -- command connection ------------------------------------------------- #

    async def connect(self, node: EditorNode | None = None) -> EditorNode:
        """Open the command connection (rediscovering if needed)."""
        await self.disconnect()
        if node is None:
            candidates = [n for n in await self.discover() if n.matches(self.config.project_filter)]
            if not candidates:
                where = f"{self.config.multicast_group[0]}:{self.config.multicast_group[1]}"
                hint = f" with a project matching {self.config.project_filter!r}" if self.config.project_filter else ""
                raise EditorNotFound(
                    f"No Unreal Editor{hint} answered on {where}. Is the editor running with "
                    "Python Remote Execution enabled, and do the multicast settings match?"
                )
            node = candidates[0]

        loop = asyncio.get_running_loop()
        accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = loop.create_future()

        def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if accepted.done():
                writer.close()
            else:
                accepted.set_result((reader, writer))

        server = await asyncio.start_server(
            on_connect, self.config.command_host, self.config.command_port, limit=2**20
        )
        try:
            port = server.sockets[0].getsockname()[1]
            self._send_udp(
                TYPE_OPEN_CONNECTION,
                node.node_id,
                {"command_ip": self.config.command_host, "command_port": port},
            )
            try:
                reader, writer = await asyncio.wait_for(accepted, self.config.connect_timeout)
            except asyncio.TimeoutError:
                self.nodes.pop(node.node_id, None)
                raise EditorNotFound(
                    f"Editor {node.project_name or node.node_id} did not connect back to "
                    f"{self.config.command_host}:{port} within {self.config.connect_timeout}s "
                    "(firewall blocking the editor's outbound TCP?)"
                ) from None
        finally:
            server.close()  # stop listening; the accepted connection stays open

        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._reader, self._writer = reader, writer
        self._decoder = JsonStreamDecoder()
        self.remote = node
        self.generation += 1
        log.info("connected to %s (%s)", node.project_name, node.node_id)
        return node

    async def disconnect(self) -> None:
        writer, remote = self._writer, self.remote
        self._reader = self._writer = None
        if writer is None:
            return
        if remote is not None and self._udp is not None:
            with contextlib.suppress(OSError, RemoteExecError):
                self._send_udp(TYPE_CLOSE_CONNECTION, remote.node_id)
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), 1.0)

    async def _drop(self) -> None:
        writer = self._writer
        self._reader = self._writer = None
        if writer is not None:
            writer.close()

    # -- commands ----------------------------------------------------------- #

    async def execute(
        self,
        command: str,
        exec_mode: str = EXECUTE_FILE,
        *,
        unattended: bool = True,
        timeout: float | None = None,
    ) -> CommandResult:
        """Send one command and wait for its result.

        ``unattended`` asks the editor to suppress modal dialogs, which would
        otherwise block the game thread until someone clicks them.
        """
        if exec_mode not in EXEC_MODES:
            raise ValueError(f"exec_mode must be one of {EXEC_MODES}")
        timeout = self.config.command_timeout if timeout is None else timeout
        async with self._lock:
            for attempt in (1, 2):
                if not self.connected:
                    await self.connect()
                assert self._writer is not None and self.remote is not None
                payload = encode_message(
                    TYPE_COMMAND,
                    self.node_id,
                    self.remote.node_id,
                    {"command": command, "unattended": unattended, "exec_mode": exec_mode},
                )
                started = time.perf_counter()
                try:
                    self._writer.write(payload)
                    await self._writer.drain()
                except (ConnectionError, OSError):
                    # Nothing was delivered, so resending is safe.
                    await self._drop()
                    if attempt == 2:
                        raise ConnectionLost("could not send command to the editor") from None
                    continue
                msg = await self._read_result(timeout)
                data = msg.get("data") or {}
                return CommandResult(
                    success=bool(data.get("success")),
                    result=str(data.get("result", "")),
                    output=[
                        {"type": str(o.get("type", "Info")), "output": str(o.get("output", ""))}
                        for o in data.get("output") or []
                        if isinstance(o, dict)
                    ],
                    exec_mode=exec_mode,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
        raise AssertionError("unreachable")

    async def _read_result(self, timeout: float) -> dict[str, Any]:
        assert self._reader is not None
        try:
            async with asyncio.timeout(timeout):
                while True:
                    chunk = await self._reader.read(65536)
                    if not chunk:
                        raise ConnectionLost("the editor closed the command connection")
                    for obj in self._decoder.feed(chunk):
                        msg = decode_message(obj)
                        if msg is not None and msg["type"] == TYPE_COMMAND_RESULT:
                            return msg
        except TimeoutError:
            await self._drop()
            raise CommandTimeout(f"no reply from the editor within {timeout}s") from None
        except (ConnectionError, OSError) as exc:
            await self._drop()
            raise ConnectionLost(f"command connection failed: {exc}") from None
        except ConnectionLost:
            await self._drop()
            raise

    async def run(self, code: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Run a multi-line script; returns success/result/stdout/stderr."""
        return (await self.execute(code, EXECUTE_FILE, timeout=timeout)).to_dict()

    async def evaluate(self, expression: str, *, timeout: float | None = None) -> CommandResult:
        return await self.execute(expression, EVALUATE_STATEMENT, timeout=timeout)
