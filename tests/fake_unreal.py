"""A tiny stand-in for UE 5.5's ``unreal`` Python module.

Only the API surface used by ``unreal_bridge/editor_runtime.py`` is modelled,
with the same names and call shapes, so the runtime runs unmodified inside the
fake editor. ``reset()`` rebuilds a small sample project.
"""

from __future__ import annotations

import json
import re


class Name(str):
    pass


class Text(str):
    pass


class FieldPath(str):
    pass


class Array(list):
    pass


class Map(dict):
    pass


class Set(set):
    pass


class EnumBase:
    def __init__(self, name: str, value: int) -> None:
        self.name = name
        self.value = value

    def __repr__(self) -> str:
        return f"<{type(self).__name__}.{self.name}: {self.value}>"

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and other.value == self.value  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash((type(self).__name__, self.value))


def _enum(cls: type, *names: str) -> None:
    for i, n in enumerate(names):
        setattr(cls, n, cls(n, i))


class _PlayNetMode(EnumBase):
    """EPlayNetMode. UE 5.5 does not export it as unreal.PlayNetMode; tests that
    model an engine which does can expose it with monkeypatch."""


_enum(_PlayNetMode, "PIE_STANDALONE", "PIE_LISTEN_SERVER", "PIE_CLIENT")


class DoorMode(EnumBase):
    pass


_enum(DoorMode, "SWING", "SLIDE")


class StructBase:
    _fields: tuple[tuple[str, object], ...] = ()

    def __init__(self, *args: object, **kw: object) -> None:
        for (name, default), value in zip(self._fields, list(args) + [None] * len(self._fields)):
            setattr(self, name, kw.get(name, default if value is None else value))

    def get_editor_property(self, name: str) -> object:
        if name not in dict(self._fields):
            raise Exception(f"Failed to find property '{name}' on '{type(self).__name__}'")
        return getattr(self, name)

    def set_editor_property(self, name: str, value: object) -> None:
        self.get_editor_property(name)
        setattr(self, name, value)

    def export_text(self) -> str:
        return "(" + ",".join(f"{n.upper()}={float(getattr(self, n)):.6f}" for n, _ in self._fields) + ")"

    def import_text(self, text: str) -> None:
        for key, value in re.findall(r"(\w+)=([-\d.eE+]+)", text):
            setattr(self, key.lower(), float(value))

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and all(getattr(self, n) == getattr(other, n) for n, _ in self._fields)


class Vector(StructBase):
    _fields = (("x", 0.0), ("y", 0.0), ("z", 0.0))


class Object:
    def __init__(self, path: str, props: dict | None = None) -> None:
        self._path = path
        self._props = dict(props or {})
        self._class: Class | None = None

    def get_name(self) -> str:
        return self._path.rsplit(".", 1)[-1].rsplit("/", 1)[-1]

    def get_path_name(self) -> str:
        return self._path

    def get_class(self) -> "Class":
        return self._class or Class(f"/Script/CoreUObject.{type(self).__name__}")

    def modify(self, always_mark_dirty: bool = True) -> bool:
        STATE["modified"].append(self._path)
        return True

    def get_editor_property(self, name: str) -> object:
        if name not in self._props:
            raise Exception(f"Failed to find property '{name}' on '{self.get_name()}'")
        return self._props[name]

    def set_editor_property(self, name: str, value: object) -> None:
        current = self.get_editor_property(name)
        if isinstance(current, Array) and isinstance(value, list):
            value = Array(value)
        elif isinstance(current, float) and isinstance(value, int):
            value = float(value)
        elif current is not None and not isinstance(value, type(current)) and not (
            isinstance(current, Object) and value is None
        ):
            raise TypeError(f"{name}: expected {type(current).__name__}, got {type(value).__name__}")
        self._props[name] = value

    def __dir__(self) -> list[str]:
        return sorted(set(object.__dir__(self)) | set(self._props))


class Class(Object):
    def __init__(self, path: str, cdo_props: dict | None = None) -> None:
        super().__init__(path)
        self.cdo = Object(path.rsplit(".", 1)[0] + ".Default__" + path.rsplit(".", 1)[-1], cdo_props)
        self.cdo._class = self


class Blueprint(Object):
    def __init__(self, path: str, cdo_props: dict) -> None:
        super().__init__(path)
        self.generated = Class(path + "_C", cdo_props)


class DataTable(Object):
    def __init__(self, path: str, columns: dict[str, object], rows: list[dict]) -> None:
        super().__init__(path, {"row_struct": Object("/Game/Data/S_Item.S_Item")})
        self.columns = columns  # column -> default value
        self.rows = rows


class World(Object):
    def __init__(self, path: str, role: str) -> None:
        super().__init__(path)
        self.role = role


class _LevelEditorPlaySettings(Object):
    """Not exported to Python in UE 5.5; reachable only through find_object by path."""


class TopLevelAssetPath:
    def __init__(self, package_name: str, asset_name: str) -> None:
        self.package_name = Name(package_name)
        self.asset_name = Name(asset_name)


class AssetData:
    def __init__(self, obj: Object, class_name: str) -> None:
        package = obj.get_path_name().split(".", 1)[0]
        self.package_name = Name(package)
        self.package_path = Name(package.rsplit("/", 1)[0])
        self.asset_name = Name(obj.get_name())
        self.asset_class_path = TopLevelAssetPath("/Script/Engine", class_name)


STATE: dict = {}


def reset() -> None:
    STATE.clear()
    door = Blueprint(
        "/Game/Blueprints/BP_Door.BP_Door",
        {
            "is_locked": False,
            "open_speed": 1.5,
            "door_name": Name("Front"),
            "offset": Vector(1.0, 2.0, 3.0),
            "mode": DoorMode.SWING,
            "key_class": None,
            "tags": Array([Name("door")]),
        },
    )
    key = Blueprint("/Game/Blueprints/BP_Key.BP_Key", {"value": 10})
    items = DataTable(
        "/Game/Data/DT_Items.DT_Items",
        {"Damage": 0, "Label": ""},
        [{"Name": "Sword", "Damage": 10, "Label": "Sword"}, {"Name": "Axe", "Damage": 14, "Label": "Axe"}],
    )
    level = World("/Game/Maps/Level0.Level0", "editor")
    STATE.update(
        assets={
            "/Game/Blueprints/BP_Door": (door, "Blueprint"),
            "/Game/Blueprints/BP_Key": (key, "Blueprint"),
            "/Game/Data/DT_Items": (items, "DataTable"),
            "/Game/Maps/Level0": (level, "World"),
        },
        editor_world=level,
        pie_worlds=[],
        console=[],
        saved=[],
        compiled=[],
        modified=[],
        transactions=[],
        loaded_levels=[],
        play_settings=_LevelEditorPlaySettings(
            "/Script/UnrealEd.Default__LevelEditorPlaySettings",
            # play_net_mode: a non-exported enum reads back as its integer value
            {"play_net_mode": 0, "play_number_of_clients": 1, "run_under_one_process": True},
        ),
    )


def _lookup(path: str) -> Object | None:
    if path.endswith("_C"):
        bp = _lookup(path[:-2])
        return bp.generated if isinstance(bp, Blueprint) else None
    package = path.split(".", 1)[0]
    entry = STATE["assets"].get(package)
    return entry[0] if entry else None


def load_asset(path: str) -> Object | None:
    return _lookup(path)


def load_object(outer: object, path: str) -> Object | None:
    return find_object(outer, path)


def find_object(outer: object, name: str) -> Object | None:
    if name == STATE["play_settings"].get_path_name():
        return STATE["play_settings"]
    return _lookup(name)


def get_default_object(cls: type | Class) -> Object:
    if cls is _LevelEditorPlaySettings:
        return STATE["play_settings"]
    assert isinstance(cls, Class)
    return cls.cdo


class _Registry:
    def get_assets_by_path(self, path: str, recursive: bool = False) -> Array:
        prefix = path.rstrip("/") + "/"
        out = Array()
        for package, (obj, cls) in STATE["assets"].items():
            if not package.startswith(prefix):
                continue
            if not recursive and "/" in package[len(prefix):]:
                continue
            out.append(AssetData(obj, cls))
        return out


class AssetRegistryHelpers:
    @staticmethod
    def get_asset_registry() -> _Registry:
        return _Registry()


class BlueprintEditorLibrary:
    @staticmethod
    def generated_class(bp: Blueprint) -> Class:
        return bp.generated

    @staticmethod
    def compile_blueprint(bp: Blueprint) -> None:
        STATE["compiled"].append(bp.get_path_name())


class EditorAssetLibrary:
    @staticmethod
    def save_loaded_asset(asset: Object, only_if_is_dirty: bool = True) -> bool:
        STATE["saved"].append(asset.get_path_name())
        return True


class DataTableFunctionLibrary:
    @staticmethod
    def export_data_table_to_json_string(table: DataTable) -> str:
        return json.dumps(table.rows)

    @staticmethod
    def fill_data_table_from_json_string(table: DataTable, json_string: str, import_row_struct: object = None) -> bool:
        rows = json.loads(json_string)
        for row in rows:
            if any(k != "Name" and k not in table.columns for k in row):
                return False
        table.rows = [{"Name": r["Name"], **{c: r.get(c, d) for c, d in table.columns.items()}} for r in rows]
        return True

    @staticmethod
    def get_data_table_row_names(table: DataTable) -> Array:
        return Array(Name(r["Name"]) for r in table.rows)


class SystemLibrary:
    @staticmethod
    def execute_console_command(world_context_object: object, command: str, specific_player: object = None) -> None:
        STATE["console"].append((world_context_object.get_path_name() if world_context_object else None, command))

    @staticmethod
    def is_dedicated_server(world: World) -> bool:
        return world.role == "dedicated_server"

    @staticmethod
    def is_standalone(world: World) -> bool:
        return world.role in ("standalone", "editor")

    @staticmethod
    def is_server(world: World) -> bool:
        return world.role != "client"


class UnrealEditorSubsystem:
    def get_editor_world(self) -> World:
        return STATE["editor_world"]


class LevelEditorSubsystem:
    def is_in_play_in_editor(self) -> bool:
        return bool(STATE["pie_worlds"])

    def load_level(self, path: str) -> bool:
        world = _lookup(path)
        if not isinstance(world, World):
            return False
        STATE["editor_world"] = world
        STATE["loaded_levels"].append(path)
        return True

    def editor_request_begin_play(self) -> None:
        settings = STATE["play_settings"]._props
        mode = getattr(settings["play_net_mode"], "value", settings["play_net_mode"])  # enum or raw int
        players = settings["play_number_of_clients"]
        base = STATE["editor_world"].get_name()
        worlds = []
        if mode == 0:  # PIE_STANDALONE
            worlds.append(World(f"/Game/Maps/UEDPIE_0_{base}.{base}", "standalone"))
        else:
            if mode == 1:  # PIE_LISTEN_SERVER
                worlds.append(World(f"/Game/Maps/UEDPIE_0_{base}.{base}", "listen_server"))
                clients = players - 1
            else:
                worlds.append(World(f"/Game/Maps/UEDPIE_0_{base}.{base}", "dedicated_server"))
                clients = players
            for i in range(clients):
                worlds.append(World(f"/Game/Maps/UEDPIE_{i + 1}_{base}.{base}", "client"))
        STATE["pie_worlds"] = worlds

    def editor_request_end_play(self) -> None:
        STATE["pie_worlds"] = []


_SUBSYSTEMS: dict[type, object] = {}


def get_editor_subsystem(cls: type) -> object:
    return _SUBSYSTEMS.setdefault(cls, cls())


class EditorLevelLibrary:
    @staticmethod
    def get_pie_worlds(include_dedicated_server: bool) -> Array:
        return Array(w for w in STATE["pie_worlds"] if include_dedicated_server or w.role != "dedicated_server")

    @staticmethod
    def get_editor_world() -> World:
        return STATE["editor_world"]


class ScopedEditorTransaction:
    def __init__(self, description: str) -> None:
        self.description = description

    def __enter__(self) -> "ScopedEditorTransaction":
        STATE["transactions"].append(self.description)
        return self

    def __exit__(self, *exc: object) -> None:
        return None


reset()
