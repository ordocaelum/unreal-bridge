"""Real multicast discovery (skipped where the host has no multicast loopback)."""

import random
import socket
import sys

import pytest

from fake_editor import FakeEditor
from unreal_bridge.remote_exec import RemoteExecClient, RemoteExecConfig

GROUP = "239.0.0.1"


def _multicast_works(port: int) -> bool:
    socks = []
    try:
        for _ in range(2):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            socks.append(s)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("127.0.0.1" if sys.platform == "win32" else "", port))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + socket.inet_aton("127.0.0.1"))
            s.settimeout(1)
        socks[0].sendto(b"probe", (GROUP, port))
        return socks[1].recv(16) == b"probe"
    except OSError:
        return False
    finally:
        for s in socks:
            s.close()


@pytest.fixture
def mc_port():
    port = random.randint(20000, 40000)
    if not _multicast_works(port):
        pytest.skip("multicast loopback not available on this host")
    return port


async def test_multicast_discovery_with_two_editors(unreal_module, mc_port):
    a = FakeEditor(project_name="Backrooms", project_root="H:/Projects/Backrooms", multicast_group=(GROUP, mc_port)).start()
    b = FakeEditor(project_name="OtherGame", project_root="H:/Projects/OtherGame", multicast_group=(GROUP, mc_port)).start()
    try:
        cfg = RemoteExecConfig(multicast_group=(GROUP, mc_port), project_filter="othergame", discovery_timeout=2, heartbeat_interval=0.2)
        async with RemoteExecClient(cfg) as client:
            nodes = await client.discover()
            assert {n.project_name for n in nodes} == {"Backrooms", "OtherGame"}
            assert (await client.run("print('via multicast')"))["stdout"] == "via multicast"
            assert client.remote.node_id == b.node_id
            assert b.opens == 1 and a.opens == 0
    finally:
        a.stop()
        b.stop()
