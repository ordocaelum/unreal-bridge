"""Editor-side runtime. This file is NOT imported by the MCP server.

Its source is sent to the Unreal Editor once per connection and installed as
the module ``_unreal_bridge_rt`` in the editor's ``sys.modules``. After that
every tool call is a single ``EvaluateStatement``:

    __import__('sys').modules['_unreal_bridge_rt'].call_json('list_assets', '{"path": "/Game"}')

whose ``result`` is the JSON envelope ``{"ok": true, "result": ...}`` or
``{"ok": false, "error": ..., "traceback": ...}``. One round trip per call,
and nothing written to the editor's stdout.

Targets UE 5.5's Python API. Keep it dependency-free (stdlib + ``unreal``).
"""

import contextlib
import io
import json
import traceback

import unreal

MAX_LIST = 5000


# --------------------------------------------------------------------------- #
# Value conversion
# --------------------------------------------------------------------------- #


def _ue_types(*names):
    return tuple(t for t in (getattr(unreal, n, None) for n in names) if isinstance(t, type))


def to_json(value, depth=0):
    """Convert an Unreal value to something json.dumps accepts."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth > 8:
        return str(value)
    if isinstance(value, _ue_types("Name", "Text", "FieldPath")):
        return str(value)
    if isinstance(value, _ue_types("EnumBase")):
        return value.name
    if isinstance(value, _ue_types("Object")):
        return value.get_path_name()
    if isinstance(value, _ue_types("StructBase")):
        export = getattr(value, "export_text", None)
        if export is not None:
            try:
                return export()
            except Exception:
                pass
        return str(value)
    if isinstance(value, (dict,) + _ue_types("Map")):
        return {str(to_json(k, depth + 1)): to_json(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset) + _ue_types("Array", "Set", "FixedArray")):
        return [to_json(v, depth + 1) for v in value]
    return str(value)


def _load_object_ref(value):
    if value in ("", "None", None):
        return None
    obj = unreal.load_object(None, value)
    if obj is None:
        obj = unreal.load_asset(value)
    if obj is None:
        raise ValueError("could not load object %r" % (value,))
    return obj


def from_json(current, value):
    """Coerce a JSON value to the type of a property's current value."""
    if isinstance(current, _ue_types("Name")):
        return unreal.Name(str(value))
    if isinstance(current, _ue_types("Text")):
        return unreal.Text(str(value))
    if isinstance(current, _ue_types("EnumBase")) and isinstance(value, str):
        enum_type = type(current)
        key = value.split("::")[-1]
        for candidate in (key, key.upper()):
            member = getattr(enum_type, candidate, None)
            if member is not None:
                return member
        raise ValueError("%s has no member %r" % (enum_type.__name__, value))
    if isinstance(current, _ue_types("StructBase")):
        if isinstance(value, str):
            struct = type(current)()
            struct.import_text(value)
            return struct
        if isinstance(value, dict):
            for key, field_value in value.items():
                current.set_editor_property(key, from_json(current.get_editor_property(key), field_value))
            return current
    if (current is None or isinstance(current, _ue_types("Object"))) and isinstance(value, str):
        return _load_object_ref(value)
    if isinstance(value, list) and isinstance(current, (list,) + _ue_types("Array", "Set")):
        sample = next(iter(current), None)
        return [from_json(sample, v) if sample is not None else v for v in value]
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int) and not isinstance(value, bool) and isinstance(value, (int, float, str)):
        return int(value)
    if isinstance(current, float) and isinstance(value, (int, float, str)):
        return float(value)
    return value


@contextlib.contextmanager
def _transaction(description):
    scoped = getattr(unreal, "ScopedEditorTransaction", None)
    if scoped is None:
        yield
        return
    with scoped(description):
        yield


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _editor_world():
    try:
        return unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    except Exception:
        return unreal.EditorLevelLibrary.get_editor_world()


def _level_editor():
    return unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)


def _pie_worlds():
    try:
        return list(unreal.EditorLevelLibrary.get_pie_worlds(True))
    except TypeError:
        return list(unreal.EditorLevelLibrary.get_pie_worlds())


def _world_role(world):
    lib = unreal.SystemLibrary
    if lib.is_dedicated_server(world):
        return "dedicated_server"
    if lib.is_standalone(world):
        return "standalone"
    if lib.is_server(world):
        return "listen_server"
    return "client"


def _asset_class(data):
    path = getattr(data, "asset_class_path", None)
    if path is not None:
        return str(path.asset_name)
    return str(getattr(data, "asset_class", ""))


def _blueprint_cdo(path):
    """Return (asset, cdo) for a Blueprint asset path or a class path."""
    asset = unreal.load_asset(path)
    if asset is None:
        asset = unreal.load_object(None, path)
    if asset is None:
        raise ValueError("asset not found: %s" % path)
    if isinstance(asset, unreal.Blueprint):
        cls = unreal.BlueprintEditorLibrary.generated_class(asset)
        if cls is None:
            raise ValueError("%s has no generated class (compile it first)" % path)
    elif isinstance(asset, unreal.Class):
        cls = asset
    else:
        raise TypeError("%s is a %s, not a Blueprint or class" % (path, type(asset).__name__))
    return asset, unreal.get_default_object(cls)


def _readable_props(obj):
    names = []
    cls = type(obj)
    for name in dir(obj):
        if name.startswith("_"):
            continue
        attr = getattr(cls, name, None)
        if attr is not None and callable(attr):
            continue
        names.append(name)
    return names


def _load_datatable(path):
    table = unreal.load_asset(path)
    if table is None:
        raise ValueError("asset not found: %s" % path)
    if not isinstance(table, unreal.DataTable):
        raise TypeError("%s is a %s, not a DataTable" % (path, type(table).__name__))
    return table


def _datatable_rows(table):
    lib = unreal.DataTableFunctionLibrary
    export = getattr(lib, "export_data_table_to_json_string", None)
    if export is not None:
        return json.loads(export(table) or "[]")
    export = getattr(table, "export_to_json_string", None)
    if export is not None:
        return json.loads(export() or "[]")
    # Fallback: column-wise string export (values stay as UE text).
    names = [str(n) for n in lib.get_data_table_row_names(table)]
    rows = [{"Name": n} for n in names]
    for column in lib.get_data_table_column_names(table):
        values = list(lib.get_data_table_column_as_string(table, column))
        for row, value in zip(rows, values[-len(rows):] if rows else []):
            row[str(column)] = value
    return rows


def _normalise_rows(rows):
    if isinstance(rows, dict):
        return [dict(fields, Name=name) for name, fields in rows.items()]
    out = []
    for row in rows:
        if not isinstance(row, dict) or "Name" not in row:
            raise ValueError("each row needs a 'Name' key (or pass rows as {name: fields})")
        out.append(dict(row))
    return out


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


def list_assets(path="/Game", class_filter=None, recursive=True, limit=500):
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    wanted = {c.lower() for c in ([class_filter] if isinstance(class_filter, str) else class_filter or [])}
    assets, total = [], 0
    for data in registry.get_assets_by_path(path, recursive=recursive):
        cls = _asset_class(data)
        if wanted and cls.lower() not in wanted:
            continue
        total += 1
        if len(assets) < limit:
            package = str(data.package_name)
            assets.append({"path": "%s.%s" % (package, data.asset_name), "class": cls, "package": package})
    assets.sort(key=lambda a: a["path"])
    out = {"path": path, "count": total, "assets": assets}
    if total > len(assets):
        out["truncated"] = "showing %d of %d; raise `limit` or narrow `path`/`class_filter`" % (len(assets), total)
    return out


def get_defaults(blueprint_path, props=None):
    _, cdo = _blueprint_cdo(blueprint_path)
    names = props or _readable_props(cdo)
    values, errors = {}, {}
    for name in names[:MAX_LIST]:
        try:
            values[name] = to_json(cdo.get_editor_property(name))
        except Exception as exc:
            if props:
                errors[name] = str(exc)
    out = {"blueprint": blueprint_path, "class": cdo.get_class().get_path_name(), "values": values}
    if errors:
        out["errors"] = errors
    return out


def set_defaults(blueprint_path, values, compile=False, save=True):
    asset, cdo = _blueprint_cdo(blueprint_path)
    changed, errors = {}, {}
    with _transaction("unreal-bridge: set defaults on %s" % blueprint_path):
        cdo.modify()
        for name, value in values.items():
            try:
                cdo.set_editor_property(name, from_json(cdo.get_editor_property(name), value))
                changed[name] = to_json(cdo.get_editor_property(name))
            except Exception as exc:
                errors[name] = str(exc)
    if changed and compile and isinstance(asset, unreal.Blueprint):
        unreal.BlueprintEditorLibrary.compile_blueprint(asset)
    saved = False
    if changed and save:
        saved = bool(unreal.EditorAssetLibrary.save_loaded_asset(asset, False))
    out = {"blueprint": blueprint_path, "changed": changed, "compiled": bool(changed and compile), "saved": saved}
    if errors:
        out["errors"] = errors
    return out


def datatable_read(path, row_names=None):
    table = _load_datatable(path)
    rows = _datatable_rows(table)
    if row_names:
        wanted = set(row_names)
        rows = [r for r in rows if r.get("Name") in wanted]
    out = {"path": path, "row_count": len(rows), "rows": rows}
    try:
        out["row_struct"] = to_json(table.get_editor_property("row_struct"))
    except Exception:
        pass
    return out


def datatable_write(path, rows, mode="merge", save=True):
    if mode not in ("merge", "replace"):
        raise ValueError("mode must be 'merge' or 'replace'")
    table = _load_datatable(path)
    incoming = _normalise_rows(rows)
    if mode == "merge":
        merged = _datatable_rows(table)
        index = {r.get("Name"): r for r in merged}
        for row in incoming:
            if row["Name"] in index:
                index[row["Name"]].update(row)
            else:
                merged.append(row)
                index[row["Name"]] = row
        final = merged
    else:
        final = incoming
    with _transaction("unreal-bridge: write %s" % path):
        table.modify()
        ok = unreal.DataTableFunctionLibrary.fill_data_table_from_json_string(table, json.dumps(final))
    if not ok:
        raise RuntimeError("fill_data_table_from_json_string failed; check the Output Log for the field that did not import")
    saved = bool(unreal.EditorAssetLibrary.save_loaded_asset(table, False)) if save else False
    return {"path": path, "mode": mode, "written": len(incoming), "row_count": len(final), "saved": saved}


def _resolve_world(world):
    if world in (None, "", "editor"):
        return _editor_world()
    pie = [(w, _world_role(w)) for w in _pie_worlds()]
    if not pie:
        raise RuntimeError("no PIE session is running")
    if world == "server":
        for w, role in pie:
            if role in ("listen_server", "dedicated_server", "standalone"):
                return w
        raise RuntimeError("no server PIE world")
    if world.startswith("client"):
        clients = [w for w, role in pie if role == "client"]
        index = int(world.partition(":")[2] or 0)
        if index >= len(clients):
            raise RuntimeError("client %d not found (%d client worlds)" % (index, len(clients)))
        return clients[index]
    if world.startswith("pie"):
        index = int(world.partition(":")[2] or 0)
        return pie[index][0]
    raise ValueError("world must be 'editor', 'server', 'client[:N]' or 'pie[:N]'")


def console_command(command, world="editor"):
    target = _resolve_world(world)
    unreal.SystemLibrary.execute_console_command(target, command, None)
    return {"command": command, "world": target.get_path_name() if target is not None else None}


_PLAY_NET_MODES = ("PIE_STANDALONE", "PIE_LISTEN_SERVER", "PIE_CLIENT")  # EPlayNetMode order
_PLAY_SETTINGS_CDO = "/Script/UnrealEd.Default__LevelEditorPlaySettings"


def _play_settings():
    """The LevelEditorPlaySettings CDO, which is what PIE reads. UE 5.5 does not
    export the class to Python, so look the object up by path; editor-property
    access still works on it through reflection."""
    cls = getattr(unreal, "LevelEditorPlaySettings", None)
    if cls is not None:
        return unreal.get_default_object(cls)
    settings = unreal.find_object(None, _PLAY_SETTINGS_CDO)
    if settings is None:
        settings = unreal.load_object(None, _PLAY_SETTINGS_CDO)
    if settings is None:
        raise RuntimeError("could not find %s" % _PLAY_SETTINGS_CDO)
    return settings


def _play_net_mode(settings, name):
    """Return an EPlayNetMode value. The enum is an old-style UENUM that UE 5.5
    does not always export as ``unreal.PlayNetMode``, so fall back to the type of
    the property's current value, then to the raw integer."""
    enum = getattr(unreal, "PlayNetMode", None)
    if enum is None:
        current = settings.get_editor_property("play_net_mode")
        if isinstance(current, _ue_types("EnumBase")):
            enum = type(current)
    member = getattr(enum, name, None) if enum is not None else None
    return member if member is not None else _PLAY_NET_MODES.index(name)


def pie_start(map=None, clients=1, listen_server=True, single_process=True):
    editor = _level_editor()
    if editor.is_in_play_in_editor():
        return {"started": False, "reason": "a PIE session is already running; call ue_pie_stop first"}
    if map:
        if not editor.load_level(map):
            raise RuntimeError("could not load map %s" % map)
    if listen_server:
        net_mode, players = "PIE_LISTEN_SERVER", clients + 1
    elif clients > 0:
        net_mode, players = "PIE_CLIENT", clients
    else:
        net_mode, players = "PIE_STANDALONE", 1
    applied, failed = {}, {}
    try:
        settings = _play_settings()
    except Exception as exc:
        settings = None
        failed["level_editor_play_settings"] = "%s: %s" % (type(exc).__name__, exc)
    if settings is not None:
        # Values are built lazily so a failure on one setting never blocks the others or PIE.
        for prop, make_value in (
            ("play_net_mode", lambda: _play_net_mode(settings, net_mode)),
            ("play_number_of_clients", lambda: players),
            ("run_under_one_process", lambda: bool(single_process)),
        ):
            try:
                value = make_value()
                settings.set_editor_property(prop, value)
                applied[prop] = net_mode if prop == "play_net_mode" else to_json(value)
            except Exception as exc:
                failed[prop] = "%s: %s" % (type(exc).__name__, exc)
    editor.editor_request_begin_play()
    out = {
        "started": True,
        "settings": applied,
        "note": "PIE begins on the next editor tick; call ue_pie_worlds to see the worlds.",
    }
    if failed:
        out["settings_not_applied"] = failed
    return out


def pie_stop():
    editor = _level_editor()
    was_running = bool(editor.is_in_play_in_editor())
    if was_running:
        editor.editor_request_end_play()
    return {"stopping": was_running}


def pie_worlds():
    worlds = []
    for index, world in enumerate(_pie_worlds()):
        worlds.append({"index": index, "name": world.get_name(), "path": world.get_path_name(), "role": _world_role(world)})
    return {"in_pie": bool(_level_editor().is_in_play_in_editor()), "worlds": worlds}


def run_python(code):
    buf = io.StringIO()
    namespace = {"__name__": "__unreal_bridge__", "unreal": unreal}
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<unreal-bridge>", "exec"), namespace)
    result = namespace.get("result")
    out = {"stdout": buf.getvalue()}
    if result is not None:
        out["result"] = to_json(result)
    return out


TOOLS = {
    "list_assets": list_assets,
    "get_defaults": get_defaults,
    "set_defaults": set_defaults,
    "datatable_read": datatable_read,
    "datatable_write": datatable_write,
    "console_command": console_command,
    "pie_start": pie_start,
    "pie_stop": pie_stop,
    "pie_worlds": pie_worlds,
    "run_python": run_python,
}


def call(name, kwargs):
    fn = TOOLS.get(name)
    if fn is None:
        return {"ok": False, "error": "unknown tool %r" % (name,)}
    try:
        return {"ok": True, "result": fn(**(kwargs or {}))}
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "traceback": traceback.format_exc(limit=6)}


def call_json(name, kwargs_json):
    return json.dumps(call(name, json.loads(kwargs_json)), default=str)


def batch_json(calls_json, stop_on_error=False):
    results = []
    for item in json.loads(calls_json):
        if stop_on_error and results and not results[-1]["ok"]:
            results.append({"ok": False, "error": "skipped after an earlier error", "skipped": True})
            continue
        results.append(call(item["tool"], item.get("args") or {}))
    return json.dumps(results, default=str)
