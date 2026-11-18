"""Normalise raw MCP data into HomeState and diff two states into Events.

Raw input (same shape for the real MCP and the simulator):
    {"home": <get_home result>, "alarm_history": [<get_home_security_alarm_history entries>]}

get_home shape (dump of 2026-11-18): devices live in rooms[].devices; each device carries
roomName but no roomId, so the room id comes from the enclosing rooms[] entry. Older dumps
had a flat top-level "devices" list (roomName = home name); normalize() still reads it.
get_home also has energy {tariff {currency, value}, yesterdayConsumption (kWh)} and addons.

Device semantics confirmed against the "Hack 7" account (docs/capabilities/):
    contactSensor.sensorState: "alarm" = open, "normal" = closed
    temperatureSensor: temperatureValue (C), humidityValue (%)
    gateway.alarmState / tamperState: "normal" | "alarm"
    plug: power on|off, instantPower (W), todayConsumption
    bulb: power, brightness 1-100, temperature 0-100 (white tone), color_rgb "r,g,b", workMode
          The reported color_rgb is the requested colour scaled by brightness/100 (HSV value):
          set 255,140,0 at brightness 40 -> reads 102,56,0. Use light_hue_rgb to compare colours.
    smokeSensor: only batteryStatus -> smoke is read from the alarm history (see classify_alarm)

Sensors of one scenario may sit in different rooms (real account: contact in the Sala,
temperature and plug in the Quarto), so house-level rules must not assume a single room.
"""

import re
import time
import uuid
from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..home_config import HomeConfig, load_home_config, room_settings

ROLE_BY_TYPE = {
    "bulb": "light",
    "plug": "plug",
    "contactSensor": "contact",
    "smokeSensor": "smoke",
    "temperatureSensor": "temperature",
    "gateway": "hub",
    "camera": "camera",
}

EventType = Literal[
    "contact_opened", "contact_closed",
    "temperature_changed", "temperature_dropping", "humidity_changed",
    "smoke_detected", "security_alarm", "alarm_cancelled",
    "security_mode_changed", "alarm_state_changed", "hub_alarm", "tamper_alarm",
    "camera_motion", "camera_sound",
    "camera_analysis",  # evento sintético publicado pelo VisionWorker (não produzido por diff())
    "plug_switched", "plug_power_changed", "light_changed",
    "device_offline", "device_online",
    "tick",
]
Severity = Literal["info", "warning", "critical"]


class DeviceState(BaseModel):
    id: str
    name: str
    type: str
    role: str
    online: bool
    last_seen: float | None = None  # epoch seconds
    room_id: str = ""
    room_name: str = ""
    source: Literal["mcp", "sim"] = "mcp"  # where writes for this device must go
    caps: dict[str, Any] = Field(default_factory=dict)


class RoomState(BaseModel):
    id: str
    name: str
    type: str = ""  # livingRoom | bedroom | ...
    simulated: bool = False  # defined in config/home.json, exists only in the simulator
    setpoint_c: float | None = None  # config/home.json for this room, else SETPOINT_C
    device_ids: list[str] = Field(default_factory=list)


class EnergyState(BaseModel):
    yesterday_consumption_kwh: float | None = None
    tariff: float | None = None  # energy.tariff.value
    tariff_currency: str = ""


class AlarmEntry(BaseModel):
    key: str
    event_type: str  # ALARM | CANCEL | ...
    detail_type: str  # EMERGENCY | INTRUSION | ...
    device_id: str = ""
    device_type: str = ""
    room_name: str = ""
    ts: float


class HomeState(BaseModel):
    ts: float
    home_name: str = ""
    security_status: str = "DISARMED"  # DISARMED | STAY | AWAY
    is_alarmed: bool = False
    devices: dict[str, DeviceState] = Field(default_factory=dict)
    rooms: dict[str, RoomState] = Field(default_factory=dict)
    energy: EnergyState = Field(default_factory=EnergyState)
    alarms: list[AlarmEntry] = Field(default_factory=list)  # newest first

    # -- lookups -------------------------------------------------------------
    def room(self, key: str | None) -> RoomState | None:
        """Room by id or by name (case-insensitive)."""
        if not key:
            return None
        if key in self.rooms:
            return self.rooms[key]
        k = key.casefold()
        return next((r for r in self.rooms.values() if r.name.casefold() == k), None)

    def devices_by_role(self, role: str, room: str | None = None) -> list[DeviceState]:
        """All devices with this role, optionally only in one room (id or name)."""
        room_id = None
        if room is not None:
            r = self.room(room)
            if r is None:
                return []
            room_id = r.id
        return [d for d in self.devices.values()
                if d.role == role and (room_id is None or d.room_id == room_id)]

    def by_role(self, role: str, room: str | None = None) -> DeviceState | None:
        """First device with this role (in the room, if given)."""
        found = self.devices_by_role(role, room)
        return found[0] if found else None

    def cap(self, role: str, cap: str, room: str | None = None, default: Any = None) -> Any:
        dev = self.by_role(role, room)
        return dev.caps.get(cap, default) if dev else default

    def room_devices(self, room: str) -> list[DeviceState]:
        r = self.room(room)
        return [self.devices[i] for i in r.device_ids if i in self.devices] if r else []

    # -- single-device views (first device found; kept for existing callers) ---
    @property
    def armed(self) -> bool:
        return self.security_status in ("STAY", "AWAY")

    @property
    def contact_open(self) -> bool | None:
        v = self.cap("contact", "sensorState")
        return None if v is None else v == "alarm"

    @property
    def temperature(self) -> float | None:
        return _num(self.cap("temperature", "temperatureValue"))

    @property
    def humidity(self) -> float | None:
        return _num(self.cap("temperature", "humidityValue"))

    @property
    def plug_on(self) -> bool | None:
        v = self.cap("plug", "power")
        return None if v is None else v == "on"

    @property
    def plug_power_w(self) -> float | None:
        return _num(self.cap("plug", "instantPower"))

    @property
    def light_hue_rgb(self) -> tuple[int, int, int] | None:
        """Reported light colour normalised to full brightness (max channel = 255)."""
        return hue_rgb(self.cap("light", "color_rgb"))

    # -- house-level views (all rooms) -----------------------------------------
    def open_contacts(self) -> list[DeviceState]:
        return [d for d in self.devices_by_role("contact") if d.caps.get("sensorState") == "alarm"]

    def plugs_on(self) -> list[DeviceState]:
        return [d for d in self.devices_by_role("plug") if d.caps.get("power") == "on"]

    @property
    def any_contact_open(self) -> bool | None:
        return bool(self.open_contacts()) if self.devices_by_role("contact") else None

    @property
    def any_plug_on(self) -> bool | None:
        return bool(self.plugs_on()) if self.devices_by_role("plug") else None

    # -- summaries ---------------------------------------------------------------
    def room_summary(self, room: str) -> dict:
        """Sensor values of one room (only the roles present there)."""
        r = self.room(room)
        if r is None:
            return {}
        out: dict[str, Any] = {"id": r.id, "tipo": r.type}
        if r.simulated:
            out["simulado"] = True
        devs = self.room_devices(r.id)
        roles = {d.role for d in devs}
        if "contact" in roles:
            out["janela_aberta"] = self.cap("contact", "sensorState", room=r.id) == "alarm"
        if "temperature" in roles:
            out["temperatura_c"] = _num(self.cap("temperature", "temperatureValue", room=r.id))
            out["humidade_pct"] = _num(self.cap("temperature", "humidityValue", room=r.id))
            out["setpoint_c"] = r.setpoint_c
        if "plug" in roles:
            out["aquecedor_ligado"] = self.cap("plug", "power", room=r.id) == "on"
            out["aquecedor_potencia_w"] = _num(self.cap("plug", "instantPower", room=r.id))
        if "light" in roles:
            light = self.by_role("light", r.id)
            out["luz"] = {"power": light.caps.get("power"), "cor": hue_rgb(light.caps.get("color_rgb")),
                          "brilho": light.caps.get("brightness")}
        out["dispositivos"] = [{"nome": d.name, "papel": d.role, "online": d.online} for d in devs]
        out["offline"] = [d.name for d in devs if not d.online]
        return out

    def summary(self) -> dict:
        """Compact view for agents / UI. Top-level keys use the first device of each role;
        "divisoes" has the same values per room."""
        light = self.by_role("light")
        rooms: dict[str, dict] = {}
        for r in self.rooms.values():
            key = r.name if r.name not in rooms else f"{r.name} ({r.id[:8]})"
            rooms[key] = self.room_summary(r.id)
        return {
            "seguranca": self.security_status,
            "alarme_ativo": self.is_alarmed,
            "janela_aberta": self.contact_open,
            "temperatura_c": self.temperature,
            "humidade_pct": self.humidity,
            "aquecedor_ligado": self.plug_on,
            "aquecedor_potencia_w": self.plug_power_w,
            "luz": None if not light else {
                "power": light.caps.get("power"), "cor": self.light_hue_rgb,
                "brilho": light.caps.get("brightness"), "online": light.online,
            },
            "offline": [d.name for d in self.devices.values() if not d.online],
            "divisoes": rooms,
        }


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    ts: float = Field(default_factory=time.time)
    type: EventType
    severity: Severity = "info"
    device_id: str | None = None
    role: str | None = None
    room: str | None = None  # room id
    room_name: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    message: str  # PT-PT, shown in the UI timeline


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def hue_rgb(value: Any) -> tuple[int, int, int] | None:
    """'102,56,0' -> (255, 140, 0): undo the brightness scaling of the reported colour."""
    try:
        r, g, b = (int(float(x)) for x in str(value).split(","))
    except (TypeError, ValueError):
        return None
    m = max(r, g, b)
    if m == 0:
        return (0, 0, 0)
    return tuple(round(c * 255 / m) for c in (r, g, b))


# ------------------------------------------------------------- PT-PT wording

_FEMININE_EXCEPTIONS = {"suite", "suíte", "garagem", "arrecadação"}


def _feminine(room_name: str) -> bool:
    word = room_name.strip().split()[0].casefold() if room_name.strip() else ""
    return word in _FEMININE_EXCEPTIONS or word.endswith(("a", "agem", "ção", "dade"))


def de(room_name: str | None) -> str:
    """'Sala' -> 'da Sala', 'Quarto' -> 'do Quarto' (empty if no room)."""
    if not room_name:
        return ""
    return f"{'da' if _feminine(room_name) else 'do'} {room_name}"


def em(room_name: str | None) -> str:
    """'Sala' -> 'na Sala', 'Quarto' -> 'no Quarto' (empty if no room)."""
    if not room_name:
        return ""
    return f"{'na' if _feminine(room_name) else 'no'} {room_name}"


def _sp(text: str) -> str:
    return f" {text}" if text else ""


def fmt(v: float, nd: int = 1) -> str:
    """PT-PT decimal comma: 21.0 -> '21,0'."""
    return f"{v:.{nd}f}".replace(".", ",")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


# ---------------------------------------------------------------- normalise

def _iter_devices(home: dict):
    """(room dict | None, device dict) for every device; rooms[].devices first, then the
    old flat top-level list (skipping ids already seen)."""
    seen: set[str] = set()
    for room in home.get("rooms") or []:
        for d in room.get("devices") or []:
            seen.add(d.get("id"))
            yield room, d
    for d in home.get("devices") or []:
        if d.get("id") not in seen:
            yield None, d


def normalize(raw: dict, settings: Settings, now: float | None = None) -> HomeState:
    now = time.time() if now is None else now
    home = raw.get("home") or {}
    security = home.get("security") or {}
    config = load_home_config(settings.home_config_file)
    default_source = "sim" if raw.get("source") == "sim" else "mcp"
    sim_rooms = set(raw.get("sim_rooms") or [])
    devices: dict[str, DeviceState] = {}
    rooms: dict[str, RoomState] = {}

    def new_room(room_id: str, name: str, type_: str = "") -> RoomState:
        sp = config.params_for(room_id, name).setpoint_c
        return RoomState(id=room_id, name=name, type=type_, simulated=room_id in sim_rooms,
                         setpoint_c=settings.setpoint_c if sp is None else sp)

    for r in home.get("rooms") or []:
        if r.get("id"):
            rooms[r["id"]] = new_room(r["id"], r.get("name", ""), r.get("type", ""))

    for room, d in _iter_devices(home):
        if room is not None and room.get("id"):
            room_id, room_name = room["id"], room.get("name") or d.get("roomName", "")
        else:  # old flat format: only roomName (and maybe roomId) on the device
            room_name = d.get("roomName") or home.get("name", "")
            room_id = d.get("roomId") or f"room-{_slug(room_name) or 'casa'}"
            if room_id not in rooms:
                rooms[room_id] = new_room(room_id, room_name)
        last = d.get("lastCommunicationTime")
        last_s = last / 1000 if isinstance(last, (int, float)) and last > 0 else None
        caps = {c.get("type"): c.get("value") for c in d.get("capabilities") or [] if c.get("type")}
        devices[d["id"]] = DeviceState(
            id=d["id"],
            name=d.get("name", ""),
            type=d.get("type", ""),
            role=ROLE_BY_TYPE.get(d.get("type", ""), d.get("type", "")),
            online=last_s is not None and now - last_s <= settings.offline_after_s,
            last_seen=last_s,
            room_id=room_id,
            room_name=room_name,
            source="sim" if room_id in sim_rooms else default_source,
            caps=caps,
        )
        rooms[room_id].device_ids.append(d["id"])

    energy = home.get("energy") or {}
    tariff = energy.get("tariff") or {}
    return HomeState(
        ts=now,
        home_name=home.get("name", ""),
        security_status=str(security.get("status", "DISARMED")).upper(),
        is_alarmed=bool(security.get("isAlarmed", False)),
        devices=devices,
        rooms=rooms,
        energy=EnergyState(
            yesterday_consumption_kwh=_num(energy.get("yesterdayConsumption")),
            tariff=_num(tariff.get("value")) if isinstance(tariff, dict) else _num(tariff),
            tariff_currency=str(tariff.get("currency", "")) if isinstance(tariff, dict) else "",
        ),
        alarms=[_alarm_entry(e) for e in raw.get("alarm_history") or []],
    )


def _alarm_entry(e: dict) -> AlarmEntry:
    detail = e.get("alarmEventDetail") or {}
    dev = detail.get("device") or {}
    ts = e.get("eventTimestamp") or detail.get("timestamp") or 0
    return AlarmEntry(
        key=f"{ts}:{e.get('eventType', '')}:{e.get('alarmId', '')}",
        event_type=str(e.get("eventType", "")).upper(),
        detail_type=str(detail.get("type", "")).upper(),
        device_id=dev.get("id", ""),
        device_type=dev.get("type", ""),
        room_name=dev.get("roomName", "") or "",
        ts=ts / 1000 if ts else 0.0,
    )


def classify_alarm(entry: AlarmEntry, settings: Settings) -> Literal["smoke", "intrusion", "other"]:
    if entry.device_type == "smokeSensor" or entry.detail_type in settings.smoke_alarm_types:
        return "smoke"
    if entry.device_type in ("contactSensor", "camera") or "INTRUSION" in entry.detail_type:
        return "intrusion"
    return "other"


def _alarm_device(entry: AlarmEntry, kind: str, state: HomeState) -> tuple[DeviceState | None, bool]:
    """Device behind an alarm entry, and whether it was inferred. The real smoke test arrives
    with an empty device; if the home has exactly one smoke sensor, it must be that one."""
    if entry.device_id and entry.device_id in state.devices:
        return state.devices[entry.device_id], False
    if kind == "smoke":
        smoke = state.devices_by_role("smoke")
        if len(smoke) == 1:
            return smoke[0], True
    return None, False


# --------------------------------------------------------------------- diff

def diff(prev: HomeState | None, curr: HomeState, settings: Settings) -> list[Event]:
    """Events for what changed between two snapshots. First snapshot -> no events."""
    if prev is None:
        return []
    ev: list[Event] = []
    config = load_home_config(settings.home_config_file)

    def add(type_, message, severity="info", dev=None, room_name=None, **data):
        ev.append(Event(type=type_, message=message, severity=severity,
                        device_id=dev.id if dev else None, role=dev.role if dev else None,
                        room=dev.room_id if dev else None,
                        room_name=dev.room_name if dev else room_name,
                        data=data, ts=curr.ts))

    # --- alarm history (smoke / intrusion / cancel): only entries we have not seen before
    seen = {a.key for a in prev.alarms}
    for a in reversed(curr.alarms):  # oldest first
        if a.key in seen:
            continue
        if a.event_type == "CANCEL":
            add("alarm_cancelled", "O alarme foi cancelado.", alarm=a.model_dump())
            continue
        if a.event_type != "ALARM":  # AWAY / STAY / DISARMED (and isPre countdowns): mode changes,
            continue                 # reported by security_mode_changed, not alarms
        kind = classify_alarm(a, settings)
        dev, inferred = _alarm_device(a, kind, curr)
        room_name = dev.room_name if dev else a.room_name
        if kind == "smoke":
            add("smoke_detected", f"Fumo detetado{_sp(em(room_name))}! A ativar o protocolo de emergência.",
                "critical", dev, room_name, alarm=a.model_dump(), room_inferred=inferred)
        else:
            msg = (f"Possível intrusão{_sp(em(room_name))}: alarme disparado por um sensor com a casa armada."
                   if kind == "intrusion" else f"Alarme de segurança disparado{_sp(em(room_name))}.")
            add("security_alarm", msg, "critical", dev, room_name, alarm=a.model_dump(), kind=kind)

    # --- security mode (house level)
    if prev.security_status != curr.security_status:
        add("security_mode_changed", f"Segurança: {_mode_pt(prev.security_status)} → "
            f"{_mode_pt(curr.security_status)}.", before=prev.security_status,
            after=curr.security_status)
    if prev.is_alarmed != curr.is_alarmed:
        add("alarm_state_changed", "A casa está em alarme!" if curr.is_alarmed else
            "O alarme da casa terminou.", "critical" if curr.is_alarmed else "info",
            is_alarmed=curr.is_alarmed)

    # --- per device
    for dev_id, dev in curr.devices.items():
        old = prev.devices.get(dev_id)
        if old is None:
            continue
        de_room, em_room = _sp(de(dev.room_name)), _sp(em(dev.room_name))
        rs = room_settings(settings, dev.room_id, dev.room_name, config)  # per-room thresholds
        if old.online != dev.online:
            add("device_online" if dev.online else "device_offline",
                f"{dev.name}{de_room} {'voltou a comunicar' if dev.online else 'deixou de comunicar'}.",
                "info" if dev.online else "warning", dev)

        def changed(cap):
            return old.caps.get(cap) != dev.caps.get(cap)

        if dev.role == "contact" and changed("sensorState"):
            opened = dev.caps.get("sensorState") == "alarm"
            add("contact_opened" if opened else "contact_closed",
                f"Janela{de_room} {'aberta' if opened else 'fechada'}.",
                "warning" if opened and curr.armed else "info", dev,
                open=opened, armed=curr.armed, security=curr.security_status)

        if changed("tamperState") and dev.caps.get("tamperState") == "alarm":
            add("tamper_alarm", f"{dev.name}{de_room}: tentativa de manipulação detetada.", "warning", dev)

        if dev.role == "hub" and changed("alarmState"):
            on = dev.caps.get("alarmState") == "alarm"
            add("hub_alarm", f"Central de segurança{de_room} em alarme." if on else
                f"Central de segurança{de_room} voltou ao normal.", "critical" if on else "info", dev,
                alarm=on, armed=curr.armed)

        if dev.role == "temperature":
            t0, t1 = _num(old.caps.get("temperatureValue")), _num(dev.caps.get("temperatureValue"))
            if t0 is not None and t1 is not None and abs(t1 - t0) >= rs.temp_change_c:
                add("temperature_changed", f"Temperatura{em_room}: {fmt(t0)} °C → {fmt(t1)} °C.", dev=dev,
                    before=t0, after=t1, delta=round(t1 - t0, 2))
            h0, h1 = _num(old.caps.get("humidityValue")), _num(dev.caps.get("humidityValue"))
            if h0 is not None and h1 is not None and abs(h1 - h0) >= rs.humidity_change_pct:
                add("humidity_changed", f"Humidade{em_room}: {h0:.0f}% → {h1:.0f}%.", dev=dev,
                    before=h0, after=h1)

        if dev.role == "camera":
            for cap, type_, what in (("motionDetected", "camera_motion", "Movimento"),
                                     ("soundDetected", "camera_sound", "Som")):
                if changed(cap) and dev.caps.get(cap) not in ("", None, False):
                    add(type_, f"{what} detetado pela câmara{de_room}.", "warning" if curr.armed else "info",
                        dev, value=dev.caps.get(cap), armed=curr.armed)

        if dev.role == "plug":
            if changed("power"):
                on = dev.caps.get("power") == "on"
                add("plug_switched", f"Aquecedor (tomada){de_room} {'ligado' if on else 'desligado'}.",
                    dev=dev, on=on)
            p0, p1 = _num(old.caps.get("instantPower")), _num(dev.caps.get("instantPower"))
            if p0 is not None and p1 is not None and abs(p1 - p0) >= rs.plug_power_change_w:
                add("plug_power_changed", f"Consumo da tomada{de_room}: {p0:.0f} W → {p1:.0f} W.",
                    dev=dev, before=p0, after=p1)

        if dev.role == "light":
            light_caps = ("power", "color_rgb", "brightness", "workMode")
            if any(changed(c) for c in light_caps):
                add("light_changed", f"A luz{de_room} mudou de estado.", dev=dev,
                    **{c: dev.caps.get(c) for c in light_caps})
    return ev


def _mode_pt(mode: str) -> str:
    return {"DISARMED": "desarmada", "STAY": "armada (em casa)", "AWAY": "armada (ausente)"}.get(mode, mode)


# ------------------------------------------------------------- trend (stateful)

class _Track:
    __slots__ = ("samples", "active")

    def __init__(self):
        self.samples: deque[tuple[float, float]] = deque()
        self.active = False


class TemperatureTrend:
    """One track per temperature sensor (i.e. per room). Emits temperature_dropping once when
    a sensor falls by >= temp_drop_c within temp_drop_window_s; re-arms when it stops falling.

    The event carries the house-level context of scenario 1 (window open + heating on), with
    the room of each sensor, because the contact and the plug may be in other rooms."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.tracks: dict[str, _Track] = {}

    def update(self, state: HomeState, config: HomeConfig | None = None) -> list[Event]:
        out: list[Event] = []
        config = config if config is not None else load_home_config(self.s.home_config_file)
        for dev in state.devices_by_role("temperature"):
            t = _num(dev.caps.get("temperatureValue"))
            if t is None:
                continue
            rs = room_settings(self.s, dev.room_id, dev.room_name, config)
            tr = self.tracks.setdefault(dev.id, _Track())
            tr.samples.append((state.ts, t))
            while tr.samples and state.ts - tr.samples[0][0] > rs.temp_drop_window_s:
                tr.samples.popleft()
            peak = max(v for _, v in tr.samples)
            drop = peak - t
            if not tr.active and drop >= rs.temp_drop_c:
                tr.active = True
                out.append(self._event(state, dev, peak, t, drop, rs))
            elif tr.active and drop < rs.temp_drop_c / 2:
                tr.active = False
        return out

    def _event(self, state: HomeState, dev: DeviceState, peak: float, t: float, drop: float,
               rs: Settings) -> Event:
        open_rooms = _room_names(state.open_contacts())
        heater_rooms = _room_names(state.plugs_on())
        context = []
        if open_rooms:
            context.append("janela aberta " + ", ".join(em(n) for n in open_rooms))
        if heater_rooms:
            context.append("aquecedor ligado " + ", ".join(em(n) for n in heater_rooms))
        return Event(
            type="temperature_dropping", severity="warning", ts=state.ts,
            device_id=dev.id, role="temperature", room=dev.room_id, room_name=dev.room_name,
            message=f"A temperatura{_sp(em(dev.room_name))} está a descer: {fmt(peak)} °C → {fmt(t)} °C "
                    f"em {int(rs.temp_drop_window_s // 60)} min"
                    + (f" ({'; '.join(context)})." if context else "."),
            data={"from": peak, "to": t, "drop": round(drop, 2),
                  "window_open": state.any_contact_open, "heater_on": state.any_plug_on,
                  "open_windows": open_rooms, "heaters_on": heater_rooms},
        )


def _room_names(devs: list[DeviceState]) -> list[str]:
    return list(dict.fromkeys(d.room_name for d in devs if d.room_name))
