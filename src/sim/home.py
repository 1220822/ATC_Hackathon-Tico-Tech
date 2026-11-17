"""Simulated home with exactly the same JSON shape as the MCP (get_home + alarm history).

State lives in a JSON file (var/sim_home.json by default) so that scripts/simulate.py in one
terminal can inject events into the app running in another. It is seeded from the real dump
in docs/capabilities/get_home.json, so the simulated path exercises the real normaliser.
"""

import copy
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..config import ROOT

SEED_FILE = ROOT / "docs" / "capabilities" / "get_home.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _seed() -> dict:
    raw = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    home = copy.deepcopy(raw.get("data", raw))
    home["security"].update({"status": "DISARMED", "isAlarmed": False, "alarmType": ""})
    for d in home["devices"]:
        d["lastCommunicationTime"] = _now_ms()  # everything online in simulation (incl. Light)
        for c in d["capabilities"]:
            t = c["type"]
            if d["type"] == "contactSensor" and t == "sensorState":
                c["value"] = "normal"
            elif d["type"] == "gateway" and t == "alarmState":
                c["value"] = "normal"
            elif d["type"] == "temperatureSensor" and t == "temperatureValue":
                c["value"] = 21.0
            elif d["type"] == "temperatureSensor" and t == "humidityValue":
                c["value"] = 60.0
            elif d["type"] == "plug" and t == "power":
                c["value"] = "off"
            elif d["type"] == "plug" and t == "instantPower":
                c["value"] = 0
            elif d["type"] == "bulb" and t == "power":
                c["value"] = "on"
            elif d["type"] == "bulb" and t == "color_rgb":
                c.update(value="255,255,255", redValue=255, greenValue=255, blueValue=255)
            elif d["type"] == "camera" and t in ("motionDetected", "soundDetected"):
                c["value"] = ""
    return {"home": home, "alarm_history": []}


class SimHome:
    HEATER_W = 1500  # nominal power of the heater on the plug

    def __init__(self, path: Path):
        self.path = Path(path)

    # -- persistence -------------------------------------------------------
    def load(self) -> dict:
        if not self.path.exists():
            self.reset()
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
        self.save(_seed())

    @contextmanager
    def edit(self):
        data = self.load()
        yield data
        self.save(data)

    def snapshot(self) -> dict:
        return self.load()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _device(data: dict, dtype: str) -> dict:
        return next(d for d in data["home"]["devices"] if d["type"] == dtype)

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
            dev.update(id=device["id"], name=device["name"], type=device["type"])
        data["alarm_history"].insert(0, {
            "alarmEventDetail": {"device": dev, "timestamp": ts if event_type == "ALARM" else 0,
                                 "type": detail_type},
            "alarmId": alarm_id, "eventTimestamp": ts, "eventType": event_type,
            "intrusionId": alarm_id, "isPre": False, "securitySettings": None,
        })
        del data["alarm_history"][50:]

    # -- sensor injections (used by scripts/simulate.py) ---------------------
    def set_contact(self, open_: bool) -> None:
        with self.edit() as d:
            contact = self._device(d, "contactSensor")
            self._set(contact, "sensorState", "alarm" if open_ else "normal")
            sec = d["home"]["security"]
            if open_ and sec["status"] in ("STAY", "AWAY"):  # the real hub raises an intrusion
                sec["isAlarmed"] = True
                self._set(self._device(d, "gateway"), "alarmState", "alarm")
                self._alarm(d, "ALARM", "INTRUSION", contact)

    def set_temperature(self, value: float) -> None:
        with self.edit() as d:
            self._set(self._device(d, "temperatureSensor"), "temperatureValue", round(value, 1))

    def set_humidity(self, value: float) -> None:
        with self.edit() as d:
            self._set(self._device(d, "temperatureSensor"), "humidityValue", round(value, 1))

    def get_temperature(self) -> float:
        return float(self._get(self._device(self.load(), "temperatureSensor"), "temperatureValue"))

    def smoke(self) -> None:
        # Mirrors what we saw on the real account: ALARM / EMERGENCY with an empty device.
        with self.edit() as d:
            d["home"]["security"]["isAlarmed"] = True
            self._set(self._device(d, "gateway"), "alarmState", "alarm")
            self._alarm(d, "ALARM", "EMERGENCY")

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

    def camera(self, motion: bool = True, sound: bool = False) -> None:
        with self.edit() as d:
            cam = self._device(d, "camera")
            self._set(cam, "motionDetected", str(_now_ms()) if motion else "")
            self._set(cam, "soundDetected", str(_now_ms()) if sound else "")

    def set_plug(self, on: bool, power_w: float | None = None) -> None:
        with self.edit() as d:
            plug = self._device(d, "plug")
            self._set(plug, "power", "on" if on else "off")
            self._set(plug, "instantPower", (self.HEATER_W if on else 0) if power_w is None else power_w)

    def set_device_online(self, dtype: str, online: bool) -> None:
        with self.edit() as d:
            dev = self._device(d, dtype)
            dev["lastCommunicationTime"] = _now_ms() if online else 1

    # -- MCP write mimic (used by execute() when SIMULATE=true) -------------
    def apply_capability(self, device_id: str, body: dict) -> dict:
        """Behaves like update_device_capabilities on the simulated home."""
        with self.edit() as d:
            dev = next((x for x in d["home"]["devices"] if x["id"] == device_id), None)
            if dev is None:
                raise KeyError(f"device {device_id} not found")
            t = body["type"]
            if t == "color_rgb":
                r, g, b = (int(body[k]) for k in ("redValue", "greenValue", "blueValue"))
                self._set(dev, "color_rgb", f"{r},{g},{b}", redValue=r, greenValue=g, blueValue=b)
                if dev["type"] == "bulb":
                    self._set(dev, "workMode", "colour")
            elif t == "power" and dev["type"] == "plug":
                on = body["value"] == "on"
                self._set(dev, "power", body["value"])
                self._set(dev, "instantPower", self.HEATER_W if on else 0)
            else:
                self._set(dev, t, body["value"])
            return {"id": device_id, "type": t, "ok": True}
