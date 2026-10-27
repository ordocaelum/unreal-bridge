"""A fake Unreal Editor speaking the Python remote-execution protocol.

It runs in a background thread with plain blocking sockets (deliberately not
sharing code with the client) and executes commands with the real ``exec`` /
``eval`` against whatever ``unreal`` module is in ``sys.modules``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import selectors
import socket
import sys
import threading
import time
import traceback
import uuid

MAGIC = "ue_py"


def _msg(type_: str, source: str, dest: str | None = None, data: dict | None = None) -> bytes:
    m: dict = {"version": 1, "magic": MAGIC, "type": type_, "source": source}
    if dest:
        m["dest"] = dest
    if data is not None:
        m["data"] = data
    return json.dumps(m).encode()


class FakeEditor:
    def __init__(
        self,
        project_root: str = "C:/Projects/Fake",
        project_name: str = "FakeProject",
        multicast_group: tuple[str, int] | None = None,
    ) -> None:
        self.node_id = str(uuid.uuid4())
        self.project_root = project_root
        self.project_name = project_name
        self.multicast_group = multicast_group
        self.globals: dict = {"__name__": "__main__"}
        self.commands: list[dict] = []  # every command message's data
        self.pings = 0
        self.opens = 0
        self.closes = 0
        self.delay = 0.0  # seconds to sleep before answering a command
        self.chunk_size: int | None = None  # split replies into chunks this big
        self.respond_to_pings = True
        self.connect_back = True
        self._sel = selectors.DefaultSelector()
        self._conns: list[socket.socket] = []
        self._buffers: dict[socket.socket, bytes] = {}
        self._stop = threading.Event()
        self._drop_requests: list[threading.Event] = []
        self._thread: threading.Thread | None = None
        self.udp = self._make_udp()
        self._sel.register(self.udp, selectors.EVENT_READ, "udp")

    def _make_udp(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        if self.multicast_group is None:
            s.bind(("127.0.0.1", 0))
        else:
            group, port = self.multicast_group
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("127.0.0.1" if sys.platform == "win32" else "", port))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
            s.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(group) + socket.inet_aton("127.0.0.1"),
            )
        s.setblocking(False)
        return s

    @property
    def address(self) -> tuple[str, int]:
        return self.multicast_group or self.udp.getsockname()

    # -- control ---------------------------------------------------------- #

    def start(self) -> "FakeEditor":
        self._thread = threading.Thread(target=self._run, name="fake-editor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
        self._drop_now()
        self.udp.close()

    def drop_connections(self) -> None:
        """Close all command connections (done on the editor thread, like UE)."""
        if self._thread is None or not self._thread.is_alive() or threading.current_thread() is self._thread:
            self._drop_now()
            return
        done = threading.Event()
        self._drop_requests.append(done)
        done.wait(2)

    def _drop_now(self) -> None:
        for c in self._conns:
            with contextlib.suppress(Exception):
                self._sel.unregister(c)
            with contextlib.suppress(OSError):
                c.shutdown(socket.SHUT_RDWR)
            c.close()
        self._conns.clear()
        self._buffers.clear()

    def restart(self) -> None:
        """Simulate an editor restart: new node id, fresh interpreter, no connections."""
        self.drop_connections()
        self.node_id = str(uuid.uuid4())
        self.globals = {"__name__": "__main__"}
        sys.modules.pop("_unreal_bridge_rt", None)

    @property
    def connection_count(self) -> int:
        return len(self._conns)

    # -- loop ------------------------------------------------------------- #

    def _run(self) -> None:
        while not self._stop.is_set():
            events = self._sel.select(0.02)
            while self._drop_requests:
                self._drop_now()
                self._drop_requests.pop().set()
            for key, _ in events:
                try:
                    if key.data == "udp":
                        self._on_udp()
                    else:
                        self._on_tcp(key.fileobj)  # type: ignore[arg-type]
                except (OSError, ValueError):
                    pass

    def _on_udp(self) -> None:
        data, addr = self.udp.recvfrom(65536)
        msg = json.loads(data)
        if msg.get("magic") != MAGIC or msg.get("source") == self.node_id:
            return
        reply_to = self.multicast_group or addr
        if msg["type"] == "ping":
            self.pings += 1
            if self.respond_to_pings:
                pong = {
                    "user": "tester",
                    "machine": "fakebox",
                    "engine_version": "5.5.4-0+++UE5+Release-5.5",
                    "engine_root": "H:/Games/UE_5.5",
                    "project_root": self.project_root,
                    "project_name": self.project_name,
                }
                self.udp.sendto(_msg("pong", self.node_id, msg["source"], pong), reply_to)
        elif msg["type"] == "open_connection" and msg.get("dest") == self.node_id:
            self.opens += 1
            if self.connect_back:
                d = msg["data"]
                conn = socket.create_connection((d["command_ip"], d["command_port"]), timeout=2)
                conn.setblocking(False)
                self._conns.append(conn)
                self._buffers[conn] = b""
                self._sel.register(conn, selectors.EVENT_READ, "tcp")
        elif msg["type"] == "close_connection" and msg.get("dest") == self.node_id:
            self.closes += 1
            self.drop_connections()

    def _on_tcp(self, conn: socket.socket) -> None:
        chunk = conn.recv(65536)
        if not chunk:
            self._sel.unregister(conn)
            self._conns.remove(conn)
            conn.close()
            return
        buf = self._buffers.get(conn, b"") + chunk
        decoder = json.JSONDecoder()
        text = buf.decode()
        while text.strip():
            try:
                obj, end = decoder.raw_decode(text.lstrip())
            except json.JSONDecodeError:
                break
            text = text.lstrip()[end:]
            if obj.get("type") == "command":
                self._answer(conn, obj)
        self._buffers[conn] = text.encode()

    def _answer(self, conn: socket.socket, msg: dict) -> None:
        data = msg["data"]
        self.commands.append(data)
        result = self._execute(data["command"], data["exec_mode"])
        if self.delay:
            time.sleep(self.delay)
        payload = _msg("command_result", self.node_id, msg["source"], {"command": data["command"], **result})
        conn.setblocking(True)
        try:
            if self.chunk_size:
                for i in range(0, len(payload), self.chunk_size):
                    conn.sendall(payload[i : i + self.chunk_size])
                    time.sleep(0.001)
            else:
                conn.sendall(payload)
        finally:
            conn.setblocking(False)

    def _execute(self, command: str, mode: str) -> dict:
        buf = io.StringIO()
        success, result = True, "None"
        try:
            with contextlib.redirect_stdout(buf):
                if mode == "ExecuteFile":
                    code = command
                    if "\n" not in command and os.path.isfile(command):
                        with open(command, encoding="utf-8") as fh:
                            code = fh.read()
                    exec(compile(code, "<remote>", "exec"), self.globals)
                elif mode == "ExecuteStatement":
                    exec(compile(command, "<remote>", "single"), self.globals)
                elif mode == "EvaluateStatement":
                    result = repr(eval(compile(command, "<remote>", "eval"), self.globals))
                else:
                    raise ValueError(f"bad exec_mode {mode}")
        except Exception:
            success, result = False, traceback.format_exc()
        output = [{"type": "Info", "output": line} for line in buf.getvalue().splitlines()]
        if not success:
            output.append({"type": "Error", "output": result})
        return {"success": success, "result": result, "output": output}
