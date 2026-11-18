"""3D layout of the house for the digital twin: config/layout.json (versioned), validated here.

Coordinates in metres. x to the right, y up, z towards the viewer. Each room is a box whose
corner (x, z) is its minimum corner; device positions are RELATIVE to that corner, so moving a
room in the file moves its devices with it. Wall sides:
    north = z 0 (back)   south = z depth (front)   west = x 0   east = x width
An opening's `offset` is the distance from the start of its wall (min x for north/south,
min z for west/east) to the opening's centre.

Rooms are keyed by the MCP room id and devices by the MCP device id. The layout only says WHERE
things are; WHAT exists always comes from get_home:
  - MCP rooms/devices missing from the layout get a default place and a warning (never hidden);
  - layout entries for ids the MCP does not report are ignored;
  - if the MCP says a device is in another room than the layout, the MCP wins.
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

log = logging.getLogger(__name__)

Side = Literal["north", "south", "east", "west"]
Vec3 = tuple[float, float, float]
SIDES: tuple[Side, ...] = ("north", "south", "west", "east")

# defaults for rooms that are not in the layout and for scripts/layout_init.py
ROOM_W, ROOM_D, ROOM_H = 5.0, 4.0, 2.6
TOLERANCE = 0.3  # devices may sit slightly outside the box (on the outer face of a wall)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Opening(_Model):
    id: str = Field(min_length=1)
    type: Literal["door", "window"]
    offset: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    sill: float = Field(0.0, ge=0)  # height of the bottom edge (0 for doors)


class Wall(_Model):
    side: Side
    openings: list[Opening] = Field(default_factory=list)


def _four_walls() -> list[Wall]:
    return [Wall(side=s) for s in SIDES]


class RoomLayout(_Model):
    name: str = ""  # informative only; the name shown comes from the MCP
    x: float
    z: float
    width: float = Field(gt=0)
    depth: float = Field(gt=0)
    height: float = Field(ROOM_H, gt=0)
    walls: list[Wall] = Field(default_factory=_four_walls)  # a missing side = open plan

    @model_validator(mode="after")
    def _check(self):
        sides = [w.side for w in self.walls]
        if len(set(sides)) != len(sides):
            raise ValueError(f"divisão {self.name!r}: parede repetida")
        for w in self.walls:
            length = self.width if w.side in ("north", "south") else self.depth
            for o in w.openings:
                if o.offset - o.width / 2 < -1e-6 or o.offset + o.width / 2 > length + 1e-6:
                    raise ValueError(f"abertura {o.id!r} sai da parede {w.side} ({length} m)")
                if o.sill + o.height > self.height + 1e-6:
                    raise ValueError(f"abertura {o.id!r} é mais alta do que a divisão")
        return self

    def opening_ids(self) -> set[str]:
        return {o.id for w in self.walls for o in w.openings}


class DeviceLayout(_Model):
    room_id: str
    name: str = ""  # informative only
    position: Vec3  # relative to the room corner (x, y, z)
    rotation_y: float = 0.0  # degrees, 0 = facing +z (south)
    mount: Literal["ceiling", "wall", "floor", "window", "door", "corner"] = "wall"
    opening: str | None = None  # contact sensor: id of the window/door it watches
    target: Vec3 | None = None  # camera: point it looks at, relative to the room corner


class Display(_Model):
    """How values are drawn (not what exists)."""
    plug_active_w: float = Field(20.0, ge=0)  # plug glows above this power
    heat_min_c: float = 16.0  # floor heat map: blue
    heat_mid_c: float = 21.0  # green
    heat_max_c: float = 26.0  # red
    camera_active_s: float = Field(60.0, gt=0)  # motion/sound highlighted for this long

    @model_validator(mode="after")
    def _order(self):
        if not self.heat_min_c < self.heat_mid_c < self.heat_max_c:
            raise ValueError("heat_min_c < heat_mid_c < heat_max_c")
        return self


class Layout(_Model):
    version: int = 1
    notes: str = ""
    display: Display = Field(default_factory=Display)
    rooms: dict[str, RoomLayout] = Field(default_factory=dict)
    devices: dict[str, DeviceLayout] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self):
        ids = [o.id for r in self.rooms.values() for w in r.walls for o in w.openings]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"ids de aberturas repetidos: {sorted(dup)}")
        for dev_id, d in self.devices.items():
            room = self.rooms.get(d.room_id)
            if room is None:
                raise ValueError(f"dispositivo {dev_id}: divisão {d.room_id} não está no layout")
            for p in (d.position, d.target):
                if p is None:
                    continue
                x, y, z = p
                if not (-TOLERANCE <= x <= room.width + TOLERANCE and -TOLERANCE <= y <= room.height + TOLERANCE
                        and -TOLERANCE <= z <= room.depth + TOLERANCE):
                    raise ValueError(f"dispositivo {dev_id} ({d.name}): posição {p} fora da divisão")
            if d.opening and d.opening not in room.opening_ids():
                raise ValueError(f"dispositivo {dev_id}: abertura {d.opening!r} não existe na divisão")
        return self


class LayoutError(ValueError):
    pass


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> Layout:
    try:
        return Layout.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError) as e:
        raise LayoutError(f"{path} inválido:\n{e}") from None


def load_layout(path: Path | str | None) -> Layout:
    """Layout at `path` (re-read when the file changes); empty layout if it does not exist."""
    if path is None or not Path(path).exists():
        return Layout()
    p = Path(path)
    return _load(str(p), p.stat().st_mtime)


# ------------------------------------------------------------ automatic layout

def _slug(text: str) -> str:
    from ..home_config import slug
    return slug(text) or "divisao"


def auto_room(room: dict, x: float, z: float = 0.0) -> tuple[RoomLayout, dict[str, DeviceLayout]]:
    """Fixed-size room with devices at the typical place of their type.
    room: a get_home room ({id, name, devices: [{id, name, type}]})."""
    w, d, h = ROOM_W, ROOM_D, ROOM_H
    slug = _slug(room.get("name", ""))
    devices = room.get("devices") or []
    contacts = [dv for dv in devices if dv.get("type") == "contactSensor"]
    windows = [Opening(id=f"janela-{slug}" + (f"-{k + 1}" if len(contacts) > 1 else ""), type="window",
                       offset=round(w * (k + 1) / (len(contacts) + 1), 2), width=1.2, height=1.2, sill=0.9)
               for k in range(len(contacts))]
    door = Opening(id=f"porta-{slug}", type="door", offset=1.0, width=0.9, height=2.1, sill=0.0)
    walls = [Wall(side="north", openings=windows), Wall(side="south", openings=[door]),
             Wall(side="west"), Wall(side="east")]
    layout = RoomLayout(name=room.get("name", ""), x=x, z=z, width=w, depth=d, height=h, walls=walls)

    placed: dict[str, DeviceLayout] = {}
    seen: dict[str, int] = {}
    for dv in devices:
        t = dv.get("type", "")
        k = seen[t] = seen.get(t, -1) + 1
        shift = 0.4 * k
        common = {"room_id": room["id"], "name": dv.get("name", "")}
        if t == "bulb":
            spec = {"position": (w / 2 + shift, h - 0.08, d / 2), "mount": "ceiling"}
        elif t == "smokeSensor":
            spec = {"position": (w * 0.35 + shift, h - 0.04, d * 0.4), "mount": "ceiling"}
        elif t == "temperatureSensor":
            spec = {"position": (w - 0.06, 1.5, d * 0.35 + shift), "rotation_y": -90, "mount": "wall"}
        elif t == "plug":
            spec = {"position": (0.06, 0.3, d * 0.8 - shift), "rotation_y": 90, "mount": "wall"}
        elif t == "contactSensor":
            win = windows[k]
            spec = {"position": (win.offset + win.width / 2 + 0.06, win.sill + win.height / 2, 0.08),
                    "mount": "window", "opening": win.id}
        elif t == "gateway":
            spec = {"position": (w - 0.4 - shift, 0.9, 0.1), "mount": "wall"}
        elif t == "camera":
            spec = {"position": (0.25 + shift, h - 0.25, 0.25), "rotation_y": 45, "mount": "corner",
                    "target": (door.offset, 1.0, d)}
        else:
            spec = {"position": (w / 2 + shift, 1.0, d / 2), "mount": "floor"}
        placed[dv["id"]] = DeviceLayout(**common, **spec)
    return layout, placed


def auto_layout(rooms: list[dict]) -> Layout:
    """Rooms side by side along x, fixed size, devices at typical positions (scripts/layout_init.py)."""
    out = Layout(notes="Gerado por scripts/layout_init.py a partir do get_home; ajustar à mão. "
                       "Coordenadas em metros relativas ao canto (x, z) de cada divisão; "
                       "north = fundo (z 0), south = frente (z depth).")
    prev: RoomLayout | None = None
    for i, r in enumerate(rooms):
        room, devices = auto_room(r, x=i * ROOM_W)
        if prev is not None:
            # shared wall: keep only the previous room's east wall, with a door between the two
            room.walls = [w for w in room.walls if w.side != "west"]
            east = next(w for w in prev.walls if w.side == "east")
            east.openings.append(Opening(id=f"porta-{_slug(prev.name)}-{_slug(room.name)}", type="door",
                                         offset=ROOM_D / 2, width=0.9, height=2.1))
        out.rooms[r["id"]] = room
        out.devices.update(devices)
        prev = room
    return Layout.model_validate(out.model_dump())  # re-validate the whole thing


def rooms_from_get_home(home: dict) -> list[dict]:
    """get_home (new rooms[].devices format, or the old flat devices list) -> rooms."""
    rooms = [{"id": r["id"], "name": r.get("name", ""), "type": r.get("type", ""),
              "devices": [{"id": d["id"], "name": d.get("name", ""), "type": d.get("type", "")}
                          for d in r.get("devices") or []]}
             for r in home.get("rooms") or [] if r.get("id")]
    flat = home.get("devices") or []
    if flat and not rooms:
        from ..perception.state import _slug as state_slug  # same id as normalize() gives it
        name = flat[0].get("roomName") or home.get("name", "")
        rooms = [{"id": f"room-{state_slug(name) or 'casa'}", "name": name, "type": "",
                  "devices": [{"id": d["id"], "name": d.get("name", ""), "type": d.get("type", "")} for d in flat]}]
    return rooms


# ------------------------------------------------------------------- resolve

_warned: set[str] = set()


def _warn(key: str, msg: str, warnings: list[str]) -> None:
    warnings.append(msg)
    if key not in _warned:  # log once per process, the UI gets them every time
        _warned.add(key)
        log.warning(msg)


def resolve_layout(layout: Layout, state) -> dict:
    """Absolute 3D placement of every MCP room and device in `state` (HomeState).
    Rooms marked simulated (config/home.json) are not part of the twin."""
    from ..perception.state import de

    warnings: list[str] = []
    rooms: dict[str, dict] = {}
    next_x = max((r.x + r.width for r in layout.rooms.values()), default=0.0)
    for r in state.rooms.values():
        if r.simulated:
            continue
        rl = layout.rooms.get(r.id)
        if rl is None:
            gap = 0.5 if next_x else 0.0
            rl = RoomLayout(name=r.name, x=next_x + gap, z=0.0, width=ROOM_W, depth=ROOM_D)
            next_x = rl.x + rl.width
            _warn(f"room:{r.id}", f"A divisão {r.name} não está no layout: colocada ao lado das outras "
                                  f"(corre scripts/layout_init.py ou acrescenta-a a config/layout.json).", warnings)
        rooms[r.id] = {"id": r.id, "name": r.name, "type": r.type, "x": rl.x, "z": rl.z,
                       "width": rl.width, "depth": rl.depth, "height": rl.height,
                       "walls": [w.model_dump() for w in rl.walls]}

    devices: dict[str, dict] = {}
    defaults_in_room: dict[str, int] = {}
    for d in state.devices.values():
        room = rooms.get(d.room_id)
        if room is None:  # simulated room
            continue
        dl = layout.devices.get(d.id)
        if dl is not None and dl.room_id != d.room_id:
            other = layout.rooms[dl.room_id].name or dl.room_id
            _warn(f"moved:{d.id}:{d.room_id}", f"{d.name}: o layout põe-no em {other}, mas o MCP diz "
                                               f"{d.room_name}; colocado no centro {de(d.room_name)}.", warnings)
            dl = None
        elif dl is None:
            _warn(f"device:{d.id}", f"{d.name} {de(d.room_name)} não está no layout: colocado no centro "
                                    f"da divisão.", warnings)
        if dl is None:
            k = defaults_in_room[d.room_id] = defaults_in_room.get(d.room_id, -1) + 1
            rel, rot, mount, opening, target = (room["width"] / 2 + 0.35 * k, 1.0, room["depth"] / 2), 0.0, \
                "floor", None, None
            placed = "default"
        else:
            rel, rot, mount, opening, target = dl.position, dl.rotation_y, dl.mount, dl.opening, dl.target
            placed = "layout"

        def absolute(p):
            return None if p is None else [round(room["x"] + p[0], 3), round(p[1], 3), round(room["z"] + p[2], 3)]

        devices[d.id] = {"id": d.id, "room": d.room_id, "position": absolute(rel), "rotation_y": rot,
                         "mount": mount, "opening": opening, "target": absolute(target), "placed": placed}
    return {"rooms": list(rooms.values()), "devices": devices, "warnings": warnings}
