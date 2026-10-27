import asyncio

import pytest

from conftest import config_for
from unreal_bridge.remote_exec import (
    EVALUATE_STATEMENT,
    EXECUTE_STATEMENT,
    CommandTimeout,
    EditorNotFound,
    RemoteExecClient,
    RemoteExecConfig,
)


async def test_discovery_reports_editor(client, editor):
    nodes = await client.discover()
    assert len(nodes) == 1
    node = nodes[0]
    assert node.node_id == editor.node_id
    assert node.project_name == "Backrooms"
    assert node.engine_version.startswith("5.5")


async def test_run_returns_structured_output(client, editor):
    res = await client.run("import sys\nprint('hello')\nprint('world')")
    assert res["success"] is True
    assert res["stdout"] == "hello\nworld"
    assert res["stderr"] == ""
    sent = editor.commands[-1]
    assert sent["exec_mode"] == "ExecuteFile"
    assert sent["unattended"] is True


async def test_evaluate_and_statement_modes(client):
    res = await client.evaluate("1 + 2")
    assert res.success and res.result == "3"
    res = await client.execute("40 + 2", EXECUTE_STATEMENT)
    assert res.stdout == "42"


async def test_errors_come_back_as_unsuccessful_results(client):
    res = await client.run("raise ValueError('boom')")
    assert res["success"] is False
    assert "ValueError: boom" in res["result"]
    assert "boom" in res["stderr"]


async def test_connection_is_persistent(client, editor):
    for i in range(5):
        assert (await client.evaluate(str(i))).result == str(i)
    assert editor.opens == 1
    assert client.generation == 1


async def test_reconnects_after_editor_drops_connection(client, editor):
    await client.evaluate("1")
    editor.drop_connections()
    await asyncio.sleep(0.1)
    assert (await client.evaluate("2")).result == "2"
    assert editor.opens == 2


async def test_reconnects_after_editor_restart(client, editor):
    first = editor.node_id
    await client.evaluate("1")
    editor.restart()
    await asyncio.sleep(0.1)
    client.nodes.clear()  # the old node would expire after node_timeout anyway
    assert (await client.evaluate("'back'")).result == "'back'"
    assert client.remote.node_id == editor.node_id != first


async def test_timeout_resets_connection_and_recovers(client, editor):
    editor.delay = 0.6
    with pytest.raises(CommandTimeout):
        await client.execute("1", EVALUATE_STATEMENT, timeout=0.2)
    assert not client.connected
    editor.delay = 0
    await asyncio.sleep(0.6)  # let the fake finish the stale reply
    assert (await client.evaluate("7")).result == "7"


async def test_chunked_and_large_replies(client, editor):
    editor.chunk_size = 7
    assert (await client.evaluate("'x' * 50")).result == repr("x" * 50)
    editor.chunk_size = None
    res = await client.run("print('y' * 2_000_000)")
    assert len(res["stdout"]) == 2_000_000


async def test_concurrent_calls_are_serialised(client):
    results = await asyncio.gather(*(client.evaluate(f"{i} * 2") for i in range(20)))
    assert [r.result for r in results] == [str(i * 2) for i in range(20)]


async def test_heartbeat_pings_while_running(client, editor):
    await client.start()
    before = editor.pings
    await asyncio.sleep(0.7)
    assert editor.pings >= before + 2


async def test_disconnect_sends_close_connection(client, editor):
    await client.evaluate("1")
    await client.disconnect()
    await asyncio.sleep(0.1)
    assert editor.closes == 1


async def test_no_editor_raises_quickly():
    cfg = RemoteExecConfig(multicast_group=("127.0.0.1", 9), discovery_timeout=0.3, heartbeat_interval=0)
    async with RemoteExecClient(cfg) as c:
        with pytest.raises(EditorNotFound, match="Remote Execution"):
            await c.run("print(1)")


async def test_editor_that_never_connects_back(editor):
    editor.connect_back = False
    async with RemoteExecClient(config_for(editor, connect_timeout=0.3)) as c:
        with pytest.raises(EditorNotFound, match="connect back"):
            await c.run("print(1)")


async def test_project_filter_excludes_other_projects(editor):
    async with RemoteExecClient(config_for(editor, project_filter="SomethingElse", discovery_timeout=0.3)) as c:
        with pytest.raises(EditorNotFound, match="SomethingElse"):
            await c.run("print(1)")
    async with RemoteExecClient(config_for(editor, project_filter="backrooms")) as c:
        assert (await c.run("print(1)"))["stdout"] == "1"
