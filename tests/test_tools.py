"""Every MCP tool, end to end: tool function → client → fake editor → editor_runtime → fake unreal."""

import asyncio
import sys

import pytest

import fake_unreal as ue
from unreal_bridge import server
from unreal_bridge._compat import ToolError


async def test_status(bridge):
    st = await server.ue_status()
    assert st["editors"][0]["project_name"] == "Backrooms"
    assert st["connected"] is False
    await server.ue_pie_worlds()
    st = await server.ue_status()
    assert st["connected"] is True


async def test_run_python_modes(bridge):
    res = await server.ue_run_python("import unreal\nprint(unreal.load_asset('/Game/Blueprints/BP_Door').get_name())")
    assert res["success"] and res["stdout"] == "BP_Door"
    res = await server.ue_run_python("2 ** 10", mode="eval")
    assert res["result"] == "1024"
    res = await server.ue_run_python("x = ", mode="exec")
    assert res["success"] is False and "SyntaxError" in res["result"]


async def test_runtime_installed_once_per_connection(bridge, editor):
    for _ in range(3):
        await server.ue_pie_worlds()
    installs = [c for c in editor.commands if "_ub_types.ModuleType" in c["command"]]
    assert len(installs) == 1
    assert all(c["exec_mode"] == "EvaluateStatement" for c in editor.commands[1:])


async def test_runtime_reinstalled_if_it_disappears(bridge):
    await server.ue_pie_worlds()
    sys.modules.pop("_unreal_bridge_rt")  # e.g. someone reset the interpreter
    assert (await server.ue_pie_worlds())["worlds"] == []


async def test_runtime_survives_editor_restart(bridge, editor):
    await server.ue_pie_worlds()
    editor.restart()
    await asyncio.sleep(0.1)
    bridge.client.nodes.clear()
    assert (await server.ue_list_assets(path="/Game/Data"))["count"] == 1


async def test_list_assets(bridge):
    res = await server.ue_list_assets()
    assert res["count"] == 4
    assert [a["path"] for a in res["assets"]][:2] == ["/Game/Blueprints/BP_Door.BP_Door", "/Game/Blueprints/BP_Key.BP_Key"]
    res = await server.ue_list_assets(path="/Game", class_filter="blueprint")
    assert {a["class"] for a in res["assets"]} == {"Blueprint"}
    res = await server.ue_list_assets(path="/Game", class_filter=["DataTable", "World"], limit=1)
    assert res["count"] == 2 and len(res["assets"]) == 1 and "truncated" in res
    res = await server.ue_list_assets(path="/Game", recursive=False)
    assert res["count"] == 0


async def test_get_defaults_selected_and_all(bridge):
    res = await server.ue_get_defaults("/Game/Blueprints/BP_Door", ["is_locked", "offset", "mode", "door_name", "nope"])
    assert res["values"] == {
        "is_locked": False,
        "offset": "(X=1.000000,Y=2.000000,Z=3.000000)",
        "mode": "SWING",
        "door_name": "Front",
    }
    assert "nope" in res["errors"]
    assert res["class"] == "/Game/Blueprints/BP_Door.BP_Door_C"
    everything = await server.ue_get_defaults("/Game/Blueprints/BP_Door.BP_Door")
    assert everything["values"]["tags"] == ["door"]
    assert everything["values"]["open_speed"] == 1.5
    assert "errors" not in everything


async def test_set_defaults_coerces_types(bridge):
    res = await server.ue_set_defaults(
        "/Game/Blueprints/BP_Door",
        {
            "is_locked": True,
            "open_speed": 3,
            "door_name": "Back",
            "mode": "Slide",
            "offset": {"z": 9},
            "key_class": "/Game/Blueprints/BP_Key.BP_Key_C",
            "tags": ["a", "b"],
            "missing": 1,
        },
        compile=True,
    )
    assert res["changed"]["is_locked"] is True
    assert res["changed"]["mode"] == "SLIDE"
    assert res["changed"]["offset"] == "(X=1.000000,Y=2.000000,Z=9.000000)"
    assert res["changed"]["key_class"] == "/Game/Blueprints/BP_Key.BP_Key_C"
    assert "missing" in res["errors"]
    assert res["compiled"] and res["saved"]
    cdo = ue.STATE["assets"]["/Game/Blueprints/BP_Door"][0].generated.cdo
    assert cdo._props["open_speed"] == 3.0 and isinstance(cdo._props["door_name"], ue.Name)
    assert ue.STATE["saved"] == ["/Game/Blueprints/BP_Door.BP_Door"]
    assert ue.STATE["compiled"] == ["/Game/Blueprints/BP_Door.BP_Door"]
    assert ue.STATE["transactions"]


async def test_set_defaults_struct_from_text(bridge):
    res = await server.ue_set_defaults("/Game/Blueprints/BP_Door", {"offset": "(X=5,Y=6,Z=7)"}, save=False)
    assert res["changed"]["offset"] == "(X=5.000000,Y=6.000000,Z=7.000000)"
    assert res["saved"] is False


async def test_defaults_errors_are_tool_errors(bridge):
    with pytest.raises(ToolError, match="asset not found"):
        await server.ue_get_defaults("/Game/Nope")
    with pytest.raises(ToolError, match="not a Blueprint"):
        await server.ue_get_defaults("/Game/Data/DT_Items")


async def test_datatable_read(bridge):
    res = await server.ue_datatable_read("/Game/Data/DT_Items")
    assert res["row_count"] == 2
    assert res["rows"][0] == {"Name": "Sword", "Damage": 10, "Label": "Sword"}
    assert res["row_struct"] == "/Game/Data/S_Item.S_Item"
    res = await server.ue_datatable_read("/Game/Data/DT_Items", row_names=["Axe"])
    assert [r["Name"] for r in res["rows"]] == ["Axe"]


async def test_datatable_write_merge_and_replace(bridge):
    res = await server.ue_datatable_write("/Game/Data/DT_Items", {"Axe": {"Damage": 20}, "Bow": {"Damage": 5, "Label": "Bow"}})
    assert res == {"path": "/Game/Data/DT_Items", "mode": "merge", "written": 2, "row_count": 3, "saved": True}
    rows = (await server.ue_datatable_read("/Game/Data/DT_Items"))["rows"]
    assert [(r["Name"], r["Damage"]) for r in rows] == [("Sword", 10), ("Axe", 20), ("Bow", 5)]
    res = await server.ue_datatable_write("/Game/Data/DT_Items", [{"Name": "Only", "Damage": 1}], mode="replace", save=False)
    assert res["row_count"] == 1 and res["saved"] is False
    with pytest.raises(ToolError, match="fill_data_table_from_json_string failed"):
        await server.ue_datatable_write("/Game/Data/DT_Items", [{"Name": "X", "Bogus": 1}])
    with pytest.raises(ToolError, match="Name"):
        await server.ue_datatable_write("/Game/Data/DT_Items", [{"Damage": 1}])


async def test_console_command_editor_and_pie_worlds(bridge):
    res = await server.ue_console_command("stat fps")
    assert res["world"] == "/Game/Maps/Level0.Level0"
    with pytest.raises(ToolError, match="no PIE session"):
        await server.ue_console_command("stat unit", world="client")
    await server.ue_pie_start(clients=2)
    await server.ue_console_command("stat net", world="server")
    await server.ue_console_command("stat net", world="client:1")
    assert ue.STATE["console"] == [
        ("/Game/Maps/Level0.Level0", "stat fps"),
        ("/Game/Maps/UEDPIE_0_Level0.Level0", "stat net"),
        ("/Game/Maps/UEDPIE_2_Level0.Level0", "stat net"),
    ]


async def test_pie_listen_server_flow(bridge, unreal_module):
    res = await server.ue_pie_start(map="/Game/Maps/Level0", clients=1, listen_server=True)
    assert res["started"] is True
    assert res["settings"] == {"play_net_mode": "PIE_LISTEN_SERVER", "play_number_of_clients": 2, "run_under_one_process": True}
    # UE 5.5 cannot pythonize PlayNetMode, so it goes through the engine's `set` command.
    assert res["applied_via_console_set"] == ["play_net_mode"]
    assert unreal_module.STATE["set_commands"] == ["set LevelEditorPlaySettings PlayNetMode PIE_ListenServer"]
    assert ue.STATE["loaded_levels"] == ["/Game/Maps/Level0"]
    worlds = await server.ue_pie_worlds()
    assert worlds["in_pie"] is True
    assert [w["role"] for w in worlds["worlds"]] == ["listen_server", "client"]
    again = await server.ue_pie_start()
    assert again["started"] is False
    assert (await server.ue_pie_stop()) == {"stopping": True}
    assert (await server.ue_pie_worlds())["worlds"] == []
    assert (await server.ue_pie_stop()) == {"stopping": False}


async def test_pie_dedicated_and_standalone(bridge):
    await server.ue_pie_start(clients=2, listen_server=False)
    assert [w["role"] for w in (await server.ue_pie_worlds())["worlds"]] == ["dedicated_server", "client", "client"]
    await server.ue_pie_stop()
    res = await server.ue_pie_start(clients=0, listen_server=False)
    assert res["settings"]["play_net_mode"] == "PIE_STANDALONE"
    assert [w["role"] for w in (await server.ue_pie_worlds())["worlds"]] == ["standalone"]


async def test_pie_start_on_engines_that_export_the_play_settings(bridge, unreal_module, monkeypatch):
    # UE 5.5 exports neither name (the fake's default); other versions may export both.
    monkeypatch.setattr(unreal_module, "PlayNetMode", unreal_module._PlayNetMode, raising=False)
    monkeypatch.setattr(unreal_module, "LevelEditorPlaySettings", unreal_module._LevelEditorPlaySettings, raising=False)
    unreal_module.STATE["play_settings"]._props["PlayNetMode"] = unreal_module._PlayNetMode.PIE_STANDALONE
    res = await server.ue_pie_start(clients=1, listen_server=True)
    assert res["started"] is True and "settings_not_applied" not in res
    assert unreal_module.STATE["play_settings"]._props["PlayNetMode"] == unreal_module._PlayNetMode.PIE_LISTEN_SERVER
    assert [w["role"] for w in (await server.ue_pie_worlds())["worlds"]] == ["listen_server", "client"]


async def test_pie_start_still_plays_when_a_setting_cannot_be_applied(bridge, unreal_module, monkeypatch):
    real_exec = unreal_module.SystemLibrary.execute_console_command

    def exec_without_set(ctx, command, player=None):
        if command.lower().startswith("set "):
            raise RuntimeError("set is disabled")
        real_exec(ctx, command, player)

    monkeypatch.setattr(unreal_module.SystemLibrary, "execute_console_command", staticmethod(exec_without_set))
    res = await server.ue_pie_start(clients=1)
    assert res["started"] is True
    assert "Cannot pythonize" in res["settings_not_applied"]["play_net_mode"]
    assert "set is disabled" in res["settings_not_applied"]["play_net_mode"]
    assert res["settings"] == {"play_number_of_clients": 2, "run_under_one_process": True}
    assert (await server.ue_pie_worlds())["in_pie"] is True


async def test_pie_start_bad_map(bridge):
    with pytest.raises(ToolError, match="could not load map"):
        await server.ue_pie_start(map="/Game/Maps/Nope")


async def test_log_tail_uses_project_root_from_discovery(bridge, editor, tmp_path):
    logs = tmp_path / "Backrooms" / "Saved" / "Logs"
    logs.mkdir(parents=True)
    (logs / "Backrooms.log").write_text("LogInit: start\nLogNet: Warning: lag\nLogTemp: hi\n", encoding="utf-8")
    res = await server.ue_log_tail(n=10, pattern="warning")
    assert res["lines"] == ["LogNet: Warning: lag"]
    assert res["file"].endswith("Backrooms.log")


async def test_log_tail_without_project(bridge, editor):
    editor.respond_to_pings = False
    bridge.client.nodes.clear()
    with pytest.raises(ToolError, match="UNREAL_BRIDGE_PROJECT_DIR"):
        await server.ue_log_tail()


async def test_batch_one_round_trip(bridge, editor, tmp_path):
    logs = tmp_path / "Backrooms" / "Saved" / "Logs"
    logs.mkdir(parents=True)
    (logs / "Backrooms.log").write_text("a\nb\n", encoding="utf-8")
    await server.ue_pie_worlds()  # connect + install runtime first
    before = len(editor.commands)
    res = await server.ue_batch(
        [
            {"tool": "ue_set_defaults", "args": {"blueprint_path": "/Game/Blueprints/BP_Door", "values": {"is_locked": True}, "save": False}},
            {"tool": "get_defaults", "args": {"blueprint_path": "/Game/Blueprints/BP_Door", "props": ["is_locked"]}},
            {"tool": "ue.log_tail", "args": {"n": 1}},
            {"tool": "run_python", "args": {"code": "print('hi')\nresult = {'n': len(unreal.STATE['saved'])}"}},
            {"tool": "ue_datatable_read", "args": {"path": "/Game/Nope"}},
            {"tool": "ue_batch", "args": {}},
        ]
    )
    assert len(editor.commands) == before + 1
    assert res["editor_round_trips"] == 1
    r = res["results"]
    assert r[0]["ok"] and r[1]["result"]["values"] == {"is_locked": True}
    assert r[2]["tool"] == "log_tail" and r[2]["result"]["lines"] == ["b"]
    assert r[3]["result"] == {"stdout": "hi\n", "result": {"n": 0}}
    assert r[4]["ok"] is False and "asset not found" in r[4]["error"]
    assert r[5]["ok"] is False and "non-batchable" in r[5]["error"]


async def test_batch_stop_on_error(bridge):
    res = await server.ue_batch(
        [
            {"tool": "pie_stop"},
            {"tool": "get_defaults", "args": {"blueprint_path": "/Game/Nope"}},
            {"tool": "pie_worlds"},
        ],
        stop_on_error=True,
    )
    assert [x["ok"] for x in res["results"]] == [True, False, False]
    assert res["results"][2]["skipped"] is True


async def test_large_results_are_truncated(bridge, unreal_module):
    bridge.max_output = 2000
    for i in range(300):
        unreal_module.STATE["assets"][f"/Game/Many/Asset_{i:03}"] = (unreal_module.Object(f"/Game/Many/Asset_{i:03}.Asset_{i:03}"), "Texture2D")
    res = await server.ue_list_assets(path="/Game/Many", limit=1000)
    assert res["count"] == 300
    assert len(res["assets"]) < 300
    assert "assets: kept" in res["_truncated"]
    res = await server.ue_run_python("print('z' * 50000)")
    assert len(res["stdout"]) < 2000 and "stdout" in res["_truncated"]


async def test_editor_down_is_a_tool_error(unreal_module):
    from unreal_bridge.remote_exec import RemoteExecConfig

    b = server.Bridge(RemoteExecConfig(multicast_group=("127.0.0.1", 9), discovery_timeout=0.2, heartbeat_interval=0))
    server.set_bridge(b)
    try:
        with pytest.raises(ToolError, match="No Unreal Editor"):
            await server.ue_list_assets()
        with pytest.raises(ToolError, match="No Unreal Editor"):
            await server.ue_run_python("1")
    finally:
        await b.close()
        server.set_bridge(None)
