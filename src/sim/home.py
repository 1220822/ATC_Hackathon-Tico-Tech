"""Simulated home with exactly the same JSON shape as the MCP (get_home + alarm history).

State lives in a JSON file (var/sim_home.json by default) so that scripts/simulate.py in one
terminal can inject events into the app running in another. It is seeded from the real dump
in docs/capabilities/get_home.json, so the simulated path exercises the real normaliser.

Devices live in home.rooms[].devices (MCP format since 2026-11-18). A state file still in the
old flat format (home.devices) is reset automatically on load.

Extra simulated rooms come from config/home.json ("simulated_rooms"). Their devices copy the
capability layout of the real device of the same type, with ids sim-<room>-<role>. On load,
the state file is synced with the config: new simulated rooms are added and removed ones dropped,
without touching the other rooms.
"""

import copy
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import ROOT

if TYPE_CHECKING:
    from ..config import Settings
    from ..home_config import SimRoom

SEED_FILE = ROOT / "docs" / "capabilities" / "get_home.json"
TYPE_BY_ROLE = {"light": "bulb", "plug": "plug", "contact": "contactSensor", "smoke": "smokeSensor",
                "temperature": "temperatureSensor", "hub": "gateway", "camera": "camera"}
SIM_NAMES = {"temperature": "Sensor de temperatura", "contact": "Sensor de contacto", "plug": "Tomada",
             "light": "Luz", "smoke": "Detetor de fumo", "camera": "Câmara", "hub": "Central"}

log = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def all_devices(home: dict) -> list[dict]:
    """Every device of every room (plus any old-format top-level devices)."""
    in_rooms = [d for r in home.get("rooms") or [] for d in r.get("devices") or []]
    return in_rooms + list(home.get("devices") or [])


def _is_old_format(data: dict) -> bool:
    home = data.get("home") or {}
    return "rooms" not in home or "devices" in home


def _room_matches(room: dict, key: str) -> bool:
    from ..home_config import slug
    k = key.casefold()
    return k in (room.get("id", "").casefold(), room.get("name", "").casefold()) or \
        slug(key) == slug(room.get("name", ""))


def _reset_values(d: dict) -> None:
    """Neutral starting values: closed, normal, off, 21 C, white light at full brightness."""
    d["lastCommunicationTime"] = _now_ms()  # everything online in simulation (incl. Light)
    for c in d["capabilities"]:
        t = c["type"]
        if d["type"] == "contactSensor" and t in ("sensorState", "tamperState"):
            c["value"] = "normal"
        elif d["type"] == "gateway" and t == "alarmState":
            c["value"] = "normal"
        elif d["type"] == "temperatureSensor" and t == "temperatureValue":
            c["value"] = 21.0
        elif d["type"] == "temperatureSensor" and t == "humidityValue":
            c["value"] = 60.0
        elif d["type"] == "plug" and t == "power":
            c["value"] = "off"
        elif d["type"] == "plug" and t in ("instantPower", "todayConsumption"):
            c["value"] = 0
        elif d["type"] == "bulb" and t == "power":
            c["value"] = "on"
        elif d["type"] == "bulb" and t == "brightness":
            c["value"] = 100  # the 18/11 dump has brightness 1
        elif d["type"] == "bulb" and t == "color_rgb":
            c.update(value="255,255,255", redValue=255, greenValue=255, blueValue=255)
        elif d["type"] == "camera" and t in ("motionDetected", "soundDetected"):
            c["value"] = ""


def _seed_home() -> dict:
    raw = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    home = copy.deepcopy(raw.get("data", raw))
    home["security"].update({"status": "DISARMED", "isAlarmed": False, "alarmType": ""})
    for d in all_devices(home):
        _reset_values(d)
    return home


def _sim_room(cfg: "SimRoom", templates: dict[str, dict]) -> dict:
    """A room in get_home format for a simulated room of config/home.json."""
    devices = []
    for role in cfg.devices:
        dtype = TYPE_BY_ROLE[role]
        dev = copy.deepcopy(templates.get(dtype) or {"type": dtype, "capabilities": []})
        dev.update(id=cfg.device_id(role), name=SIM_NAMES[role], type=dtype, roomName=cfg.name)
        _reset_values(dev)
        if dtype == "temperatureSensor":
            SimHome._set(dev, "temperatureValue", cfg.initial_temperature_c)
            SimHome._set(dev, "humidityValue", cfg.initial_humidity_pct)
        devices.append(dev)
    return {"id": cfg.id, "name": cfg.name, "type": cfg.type, "devices": devices}


class SimHome:
    HEATER_W = 1500  # nominal power of the heater on the plug

    def __init__(self, path: Path, sim_rooms: "list[SimRoom] | None" = None):
        self.path = Path(path)
        self.sim_rooms = list(sim_rooms or [])

    # -- persistence -------------------------------------------------------
    def _seed(self) -> dict:
        home = _seed_home()
        templates = {}
        for d in all_devices(home):
            templates.setdefault(d["type"], d)
        home["rooms"] += [_sim_room(r, templates) for r in self.sim_rooms]
        return {"home": home, "alarm_history": []}

    def load(self) -> dict:
        if not self.path.exists():
            self.reset()
        data = self._read()
        if _is_old_format(data):
            log.warning("%s is in the old flat format (no rooms); resetting from the seed", self.path)
            self.reset()
            data = self._read()
        if self._sync_sim_rooms(data):
            self.save(data)
        return data

    def _sync_sim_rooms(self, data: dict) -> bool:
        """Add simulated rooms that are new in the config, drop the ones that were removed."""
        rooms = data["home"]["rooms"]
        wanted = {r.id for r in self.sim_rooms}
        have = {r["id"] for r in rooms}
        stale = [r for r in rooms if r["id"].startswith("sim-") and r["id"] not in wanted]
        missing = [r for r in self.sim_rooms if r.id not in have]
        if not stale and not missing:
            return False
        templates = {}
        for d in all_devices(_seed_home()):
            templates.setdefault(d["type"], d)
        data["home"]["rooms"] = [r for r in rooms if r not in stale] + \
            [_sim_room(r, templates) for r in missing]
        return True

    def _read(self) -> dict:
        for _ in range(5):  # the other process may be mid-replace on Windows
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (PermissionError, json.JSONDecodeError):
                time.sleep(0.05)
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        for _ in range(20):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                time.sleep(0.05)
        os.replace(tmp, self.path)

    def reset(self) -> None:
        self.save(self._seed())

    @contextmanager
    def edit(self):
        data = self.load()
        yield data
        self.save(data)

    def snapshot(self) -> dict:
        return self.load()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _device(data: dict, dtype: str, room: str | None = None) -> dict:
        """First device of this type, in `room` (id or name) if given, else in any room."""
        rooms = data["home"].get("rooms") or []
        if room:
            rooms = [r for r in rooms if _room_matches(r, room)]
            if not rooms:
                raise KeyError(f"divisão desconhecida: {room}")
        dev = next((d for r in rooms for d in r.get("devices") or [] if d["type"] == dtype), None)
        if dev is None:
            raise KeyError(f"não há dispositivo do tipo {dtype}" + (f" na divisão {room}" if room else ""))
        return dev

    def room_ids(self) -> list[str]:
        return [r["id"] for r in self.load()["home"]["rooms"]]

    @staticmethod
    def _set(dev: dict, cap: str, value, **extra) -> None:
        for c in dev["capabilities"]:
            if c["type"] == cap:
                c["value"] = value
                c.update(extra)
                break
        else:
            dev["capabilities"].append({"name": "", "type": cap, "value": value, **extra})
        dev["lastCommunicationTime"] = _now_ms()

    @staticmethod
    def _get(dev: dict, cap: str):
        return next((c.get("value") for c in dev["capabilities"] if c["type"] == cap), None)

    def _alarm(self, data: dict, event_type: str, detail_type: str = "", device: dict | None = None):
        ts = _now_ms()
        alarm_id = uuid.uuid4().hex if event_type == "ALARM" else ""
        dev = {"id": "", "name": "", "roomId": "", "roomName": "", "type": ""}
        if device:
            dev.update(id=device["id"], name=device["name"], type=device["type"],
                       roomName=device.get("roomName", ""))
        data["alarm_history"].insert(0, {
            "alarmEventDetail": {"device": dev, "timestamp": ts if event_type == "ALARM" else 0,
                                 "type": detail_type},
            "alarmId": alarm_id, "eventTimestamp": ts, "eventType": event_type,
            "intrusionId": alarm_id, "isPre": False, "securitySettings": None,
        })
        del data["alarm_history"][50:]

    # -- sensor injections (used by scripts/simulate.py) ---------------------
    # Each takes an optional room (id or name); by default, the first room that has the device.
    # They return the device that was changed.
    def set_contact(self, open_: bool, room: str | None = None) -> dict:
        with self.edit() as d:
            contact = self._device(d, "contactSensor", room)
            self._set(contact, "sensorState", "alarm" if open_ else "normal")
            sec = d["home"]["security"]
            if open_ and sec["status"] in ("STAY", "AWAY"):  # the real hub raises an intrusion
                sec["isAlarmed"] = True
                self._set(self._device(d, "gateway"), "alarmState", "alarm")
                self._alarm(d, "ALARM", "INTRUSION_WARNING", contact)  # as the real hub reports it
            return contact

    def set_temperature(self, value: float, room: str | None = None) -> dict:
        with self.edit() as d:
            dev = self._device(d, "temperatureSensor", room)
            self._set(dev, "temperatureValue", round(value, 1))
            return dev

    def set_humidity(self, value: float, room: str | None = None) -> dict:
        with self.edit() as d:
            dev = self._device(d, "temperatureSensor", room)
            self._set(dev, "humidityValue", round(value, 1))
            return dev

    def get_temperature(self, room: str | None = None) -> float:
        return float(self._get(self._device(self.load(), "temperatureSensor", room), "temperatureValue"))

    def smoke(self, room: str | None = None) -> dict | None:
        # Real account: ALARM / EMERGENCY with an empty device. With a room, the alarm names that
        # room's smoke sensor (needed when there is more than one smoke sensor).
        with self.edit() as d:
            dev = self._device(d, "smokeSensor", room) if room else None
            d["home"]["security"]["isAlarmed"] = True
            self._set(self._device(d, "gateway"), "alarmState", "alarm")
            self._alarm(d, "ALARM", "EMERGENCY", dev)
            return dev

    def cancel_alarm(self) -> None:
        with self.edit() as d:
            d["home"]["security"]["isAlarmed"] = False
            self._set(self._device(d, "gateway"), "alarmState", "normal")
            self._alarm(d, "CANCEL")

    def set_security(self, mode: str) -> None:
        mode = {"disarm": "DISARMED", "stay": "STAY", "away": "AWAY"}.get(mode.lower(), mode.upper())
        with self.edit() as d:
            d["home"]["security"]["status"] = mode
            if mode == "DISARMED":
                d["home"]["security"]["isAlarmed"] = False
            self._alarm(d, mode)  # the real history also lists mode changes (eventType AWAY/STAY/DISARMED)

    def camera(self, motion: bool = True, sound: bool = False, room: str | None = None) -> dict:
        with self.edit() as d:
            cam = self._device(d, "camera", room)
            self._set(cam, "motionDetected", str(_now_ms()) if motion else "")
            self._set(cam, "soundDetected", str(_now_ms()) if sound else "")
            return cam

    def set_plug(self, on: bool, power_w: float | None = None, room: str | None = None) -> dict:
        with self.edit() as d:
            plug = self._device(d, "plug", room)
            self._set(plug, "power", "on" if on else "off")
            self._set(plug, "instantPower", (self.HEATER_W if on else 0) if power_w is None else power_w)
            return plug

    def set_device_online(self, dtype: str, online: bool, room: str | None = None) -> dict:
        with self.edit() as d:
            dev = self._device(d, dtype, room)
            dev["lastCommunicationTime"] = _now_ms() if online else 1
            return dev

    def _set_scaled_colour(self, dev: dict, base: list[int]) -> None:
        # Real server behaviour: reported colour = requested colour * brightness / 100.
        k = float(self._get(dev, "brightness") or 100) / 100
        r, g, b = (round(c * k) for c in base)
        self._set(dev, "color_rgb", f"{r},{g},{b}", redValue=r, greenValue=g, blueValue=b, _base=base)

    # -- MCP write mimic (used by route_write() for devices whose source is "sim") ----
    def apply_capability(self, device_id: str, body: dict) -> dict:
        """Behaves like update_device_capabilities on the simulated home."""
        with self.edit() as d:
            dev = next((x for x in all_devices(d["home"]) if x["id"] == device_id), None)
            if dev is None:
                raise KeyError(f"device {device_id} not found")
            t = body["type"]
            if t == "color_rgb":
                base = [int(body[k]) for k in ("redValue", "greenValue", "blueValue")]
                self._set_scaled_colour(dev, base)
                if dev["type"] == "bulb":
                    self._set(dev, "workMode", "colour")
            elif t == "brightness" and dev["type"] == "bulb":
                self._set(dev, "brightness", body["value"])
                cap = next(c for c in dev["capabilities"] if c["type"] == "color_rgb")
                self._set_scaled_colour(dev, cap.get("_base") or [255, 255, 255])
            elif t == "power" and dev["type"] == "plug":
                on = body["value"] == "on"
                self._set(dev, "power", body["value"])
                self._set(dev, "instantPower", self.HEATER_W if on else 0)
            else:
                self._set(dev, t, body["value"])
            return {"id": device_id, "type": t, "ok": True}


def make_sim_home(settings: "Settings") -> SimHome:
    """SimHome with the simulated rooms of config/home.json."""
    from ..home_config import load_home_config
    return SimHome(settings.sim_state_file, load_home_config(settings.home_config_file).simulated_rooms)
