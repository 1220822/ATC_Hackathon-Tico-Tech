"""Mock of the Connected Home MCP for the scenario mode: same interface as src.mcp_client.McpClient
(call(tool, **args)), never touches the network.

The house is the real get_home snapshot (docs/capabilities/get_home.json, rooms Sala and Quarto); its
state lives in var/cenarios/mcp_home.json (SimHome format). Scenarios created with post_new_scenario are
only stored: the mock does not run them (in the recordings the light did not turn amber).

Two behaviours copy what the real hub did in the recordings:
  - a contact opening with the house armed sets isAlarmed, with no entry in the alarm history (the
    history lagged, so the intrusion was confirmed by the camera);
  - disarming keeps isAlarmed for a while (the Coordinator silences that alarm on its side).
"""

import copy
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from ..sim.home import SimHome, all_devices

READ_TOOLS = frozenset({
    "get_home", "get_home_devices", "get_home_rooms", "get_device_details", "get_home_scenarios",
    "get_scenario", "get_home_security_status", "get_home_security_alarm_history",
})
WRITE_TOOLS = frozenset({
    "update_device_capabilities", "post_new_scenario", "patch_scenario_activation",
    "patch_home_security_status",
})
SECURITY = {"disarm": "DISARMED", "stay": "STAY", "away": "AWAY"}

# common initial state of the scenarios
PLUG_W = 7.0
TEMPERATURE_C = 22.8
HUMIDITY_PCT = 59.0


class MockMcpClient:
    def __init__(self, path: Path | str, allow_write: bool = True, plug_on_w: float = PLUG_W):
        self.allow_write = allow_write
        self.path = Path(path)
        self.home = SimHome(self.path)
        self.scenarios_path = self.path.with_name("mcp_scenarios.json")
        self.plug_on_w = plug_on_w
        self.calls: list[tuple[str, dict]] = []
        self._lock = threading.RLock()

    # -- the McpClient interface -------------------------------------------------------
    def call(self, tool: str, **args: Any) -> Any:
        if tool in WRITE_TOOLS and not self.allow_write:
            raise PermissionError(f"{tool} is a write tool; only the coordinator may call it")
        handler = getattr(self, f"_{tool}", None)
        if handler is None:
            raise ValueError(f"ferramenta MCP não simulada: {tool}")
        with self._lock:
            self.calls.append((tool, copy.deepcopy(args)))
            return handler(**args)

    def _get_home(self) -> dict:
        return copy.deepcopy(self.home.load()["home"])

    def _get_home_devices(self) -> list[dict]:
        return copy.deepcopy(all_devices(self.home.load()["home"]))

    def _get_home_rooms(self) -> list[dict]:
        return [{"id": r["id"], "name": r["name"]} for r in self.home.load()["home"]["rooms"]]

    def _get_device_details(self, deviceId: str) -> dict:
        dev = next((d for d in all_devices(self.home.load()["home"]) if d["id"] == deviceId), None)
        if dev is None:
            raise KeyError(f"device {deviceId} not found")
        return {"data": copy.deepcopy(dev)}

    def _get_home_security_status(self) -> dict:
        return copy.deepcopy(self.home.load()["home"]["security"])

    def _get_home_security_alarm_history(self, page: int = 0, size: int = 10) -> dict:
        hist = self.home.load()["alarm_history"]
        return {"history": copy.deepcopy(hist[page * size:(page + 1) * size])}

    def _get_home_scenarios(self) -> dict:
        return {"data": self.scenarios()}

    def _get_scenario(self, scenarioId: str) -> dict:
        return {"data": next((s for s in self.scenarios() if s["id"] == scenarioId), None)}

    def _update_device_capabilities(self, deviceId: str, body: dict) -> dict:
        out = self.home.apply_capability(deviceId, body)
        if body.get("type") == "power":  # the plug of the recordings draws 7 W, not the sim's 1500 W
            with self.home.edit() as d:
                dev = next(x for x in all_devices(d["home"]) if x["id"] == deviceId)
                if dev["type"] == "plug":
                    SimHome._set(dev, "instantPower", self.plug_on_w if body["value"] == "on" else 0)
        return {"data": out}

    def _post_new_scenario(self, **payload) -> dict:
        sc = {**copy.deepcopy(payload), "id": uuid.uuid4().hex}
        data = self._read_scenarios()
        data.append(sc)
        self._write_scenarios(data)
        return {"data": sc}

    def _patch_scenario_activation(self, scenarioId: str, isActive: bool) -> dict:
        data = self._read_scenarios()
        for sc in data:
            if sc["id"] == scenarioId:
                sc["isActive"] = bool(isActive)
        self._write_scenarios(data)
        return {"data": next((s for s in data if s["id"] == scenarioId), None)}

    def _patch_home_security_status(self, body: dict) -> dict:
        mode = SECURITY.get(str(body.get("status", "")).lower())
        if mode is None:
            raise ValueError(f"estado de segurança inválido: {body.get('status')}")
        with self.home.edit() as d:
            sec = d["home"]["security"]
            sec["status"] = mode
            if mode != "DISARMED":  # arming starts clean; disarming keeps the hub's isAlarmed for a while
                sec["isAlarmed"] = False
                sec["alarmType"] = ""
        return {"data": {"status": mode}}

    # -- scenarios store -----------------------------------------------------------------
    def _read_scenarios(self) -> list[dict]:
        if not self.scenarios_path.exists():
            return []
        return json.loads(self.scenarios_path.read_text(encoding="utf-8"))

    def _write_scenarios(self, data: list[dict]) -> None:
        self.scenarios_path.parent.mkdir(parents=True, exist_ok=True)
        self.scenarios_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def scenarios(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._read_scenarios())

    # -- the director's hands (not MCP tools) ---------------------------------------------
    def reset(self) -> None:
        """The common initial state: door closed, disarmed, plug on at 7 W, light normal (white, 100 %),
        Quarto at 22,8 °C and 59 %, no scenarios."""
        with self._lock:
            self.home.reset()
            with self.home.edit() as d:
                d["home"]["security"].update(status="DISARMED", isAlarmed=False, alarmType="")
                d["alarm_history"] = []
                for dev in all_devices(d["home"]):
                    if dev["type"] == "plug":
                        SimHome._set(dev, "power", "on")
                        SimHome._set(dev, "instantPower", self.plug_on_w)
                    elif dev["type"] == "temperatureSensor":
                        SimHome._set(dev, "temperatureValue", TEMPERATURE_C)
                        SimHome._set(dev, "humidityValue", HUMIDITY_PCT)
            self._write_scenarios([])
            self.calls.clear()

    def set_contact(self, open_: bool) -> None:
        with self._lock, self.home.edit() as d:
            dev = SimHome._device(d, "contactSensor")
            SimHome._set(dev, "sensorState", "alarm" if open_ else "normal")
            sec = d["home"]["security"]
            if open_ and sec["status"] in ("STAY", "AWAY"):
                sec["isAlarmed"] = True
                sec["alarmType"] = "INTRUSION"

    def set_plug(self, on: bool, power_w: float | None = None) -> None:
        with self._lock, self.home.edit() as d:
            dev = SimHome._device(d, "plug")
            SimHome._set(dev, "power", "on" if on else "off")
            SimHome._set(dev, "instantPower", (self.plug_on_w if power_w is None else power_w) if on else 0)

    def set_security(self, mode: str, alarmed: bool | None = None) -> None:
        with self._lock, self.home.edit() as d:
            sec = d["home"]["security"]
            sec["status"] = mode
            if alarmed is not None:
                sec["isAlarmed"] = alarmed

    def light_normal(self) -> None:
        with self._lock, self.home.edit() as d:
            dev = SimHome._device(d, "bulb")
            SimHome._set(dev, "power", "on")
            SimHome._set(dev, "brightness", 100)
            SimHome._set(dev, "color_rgb", "255,255,255", redValue=255, greenValue=255, blueValue=255,
                         _base=[255, 255, 255])

    def light(self) -> dict:
        dev = SimHome._device(self.home.load(), "bulb")
        return {c["type"]: c.get("value") for c in dev["capabilities"]}

    def plug(self) -> dict:
        dev = SimHome._device(self.home.load(), "plug")
        return {c["type"]: c.get("value") for c in dev["capabilities"]}
