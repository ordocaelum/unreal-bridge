from __future__ import annotations

import sys

import pytest

import fake_unreal
from fake_editor import FakeEditor
from unreal_bridge import server
from unreal_bridge.remote_exec import RemoteExecClient, RemoteExecConfig


def config_for(editor: FakeEditor, **overrides) -> RemoteExecConfig:
    kw = dict(
        multicast_group=editor.address,
        multicast_bind_address="127.0.0.1",
        discovery_timeout=1.0,
        connect_timeout=2.0,
        command_timeout=5.0,
        heartbeat_interval=0.2,
    )
    kw.update(overrides)
    return RemoteExecConfig(**kw)


@pytest.fixture
def unreal_module():
    fake_unreal.reset()
    sys.modules["unreal"] = fake_unreal
    sys.modules.pop("_unreal_bridge_rt", None)
    yield fake_unreal
    sys.modules.pop("unreal", None)
    sys.modules.pop("_unreal_bridge_rt", None)


@pytest.fixture
def editor(unreal_module, tmp_path):
    ed = FakeEditor(project_root=str(tmp_path / "Backrooms"), project_name="Backrooms").start()
    yield ed
    ed.stop()


@pytest.fixture
async def client(editor):
    async with RemoteExecClient(config_for(editor)) as c:
        yield c


@pytest.fixture
async def bridge(editor, monkeypatch):
    monkeypatch.delenv("UNREAL_BRIDGE_PROJECT_DIR", raising=False)
    b = server.Bridge(config_for(editor), max_output=60000)
    server.set_bridge(b)
    yield b
    await b.close()
    server.set_bridge(None)
