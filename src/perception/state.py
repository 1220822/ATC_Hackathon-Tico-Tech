"""Normalise raw MCP data into HomeState and diff two states into Events.

Raw input (same shape for the real MCP and the simulator):
    {"home": <get_home result>, "alarm_history": [<get_home_security_alarm_history entries>]}

Device semantics confirmed against the "Hack 7" account (docs/capabilities/):
    contactSensor.sensorState: "alarm" = open, "normal" = closed
    temperatureSensor: temperatureValue (C), humidityValue (%)
    gateway.alarmState / tamperState: "normal" | "alarm"
    plug: power on|off, instantPower (W), todayConsumption
    bulb: power, brightness 1-100, temperature 0-100 (white tone), color_rgb "r,g,b", workMode
          The reported color_rgb is the requested colour scaled by brightness/100 (HSV value):
          set 255,140,0 at brightness 40 -> reads 102,56,0. Use light_hue_rgb to compare colours.
    smokeSensor: only batteryStatus -> smoke is read from the alarm history (see classify_alarm)
"""

import time
import uuid
from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import Settings

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
    caps: dict[str, Any] = Field(default_factory=dict)


class AlarmEntry(BaseModel):
    key: str
    event_type: str  # ALARM | CANCEL | ...
    detail_type: str  # EMERGENCY | INTRUSION | ...
    device_id: str = ""
    device_type: str = ""
    ts: float


class HomeState(BaseModel):
    ts: float
    home_name: str = ""
    security_status: str = "DISARMED"  # DISARMED | STAY | AWAY
    is_alarmed: bool = False
    devices: dict[str, DeviceState] = Field(default_factory=dict)
    alarms: list[AlarmEntry] = Field(default_factory=list)  # newest first

    def by_role(self, role: str) -> DeviceState | None:
        return next((d for d in self.devices.values() if d.role == role), None)

    def cap(self, role: str, cap: str, default: Any = None) -> Any:
        dev = self.by_role(role)
        return dev.caps.get(cap, default) if dev else default

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

    def summary(self) -> dict:
        """Compact view for agents / UI."""
        light = self.by_role("light")
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
        }


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    ts: float = Field(default_factory=time.time)
    type: EventType
    severity: Severity = "info"
    device_id: str | None = None
    role: str | None = None
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


# ---------------------------------------------------------------- normalise

def normalize(raw: dict, settings: Settings, now: float | None = None) -> HomeState:
    now = time.time() if now is None else now
    home = raw.get("home") or {}
    security = home.get("security") or {}
    devices: dict[str, DeviceState] = {}
    for d in home.get("devices") or []:
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
            caps=caps,
        )
    return HomeState(
        ts=now,
        home_name=home.get("name", ""),
        security_status=str(security.get("status", "DISARMED")).upper(),
        is_alarmed=bool(security.get("isAlarmed", False)),
        devices=devices,
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
        ts=ts / 1000 if ts else 0.0,
    )


def classify_alarm(entry: AlarmEntry, settings: Settings) -> Literal["smoke", "intrusion", "other"]:
    if entry.device_type == "smokeSensor" or entry.detail_type in settings.smoke_alarm_types:
        return "smoke"
    if entry.device_type in ("contactSensor", "camera") or "INTRUSION" in entry.detail_type:
        return "intrusion"
    return "other"


# --------------------------------------------------------------------- diff

def diff(prev: HomeState | None, curr: HomeState, settings: Settings) -> list[Event]:
    """Events for what changed between two snapshots. First snapshot -> no events."""
    if prev is None:
        return []
    ev: list[Event] = []

    def add(type_, message, severity="info", dev=None, **data):
        ev.append(Event(type=type_, message=message, severity=severity,
                        device_id=dev.id if dev else None, role=dev.role if dev else None,
                        data=data, ts=curr.ts))

    # --- alarm history (smoke / intrusion / cancel): only entries we have not seen before
    seen = {a.key for a in prev.alarms}
    for a in reversed(curr.alarms):  # oldest first
        if a.key in seen:
            continue
        if a.event_type == "CANCEL":
            add("alarm_cancelled", "O alarme foi cancelado.", alarm=a.model_dump())
            continue
        kind = classify_alarm(a, settings)
        if kind == "smoke":
            add("smoke_detected", "Fumo detetado! A ativar o protocolo de emergência.",
                "critical", alarm=a.model_dump())
        else:
            msg = ("Possível intrusão: alarme disparado por um sensor com a casa armada."
                   if kind == "intrusion" else "Alarme de segurança disparado.")
            add("security_alarm", msg, "critical", alarm=a.model_dump(), kind=kind)

    # --- security mode
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
        if old.online != dev.online:
            add("device_online" if dev.online else "device_offline",
                f"{dev.name} {'voltou a comunicar' if dev.online else 'deixou de comunicar'}.",
                "info" if dev.online else "warning", dev)

        def changed(cap):
            return old.caps.get(cap) != dev.caps.get(cap)

        if dev.role == "contact" and changed("sensorState"):
            opened = dev.caps.get("sensorState") == "alarm"
            add("contact_opened" if opened else "contact_closed",
                f"Janela/porta {'aberta' if opened else 'fechada'}.",
                "warning" if opened and curr.armed else "info", dev,
                open=opened, armed=curr.armed, security=curr.security_status)

        if changed("tamperState") and dev.caps.get("tamperState") == "alarm":
            add("tamper_alarm", f"{dev.name}: tentativa de manipulação detetada.", "warning", dev)

        if dev.role == "hub" and changed("alarmState"):
            on = dev.caps.get("alarmState") == "alarm"
            add("hub_alarm", "Central de segurança em alarme." if on else
                "Central de segurança voltou ao normal.", "critical" if on else "info", dev,
                alarm=on, armed=curr.armed)

        if dev.role == "temperature":
            t0, t1 = _num(old.caps.get("temperatureValue")), _num(dev.caps.get("temperatureValue"))
            if t0 is not None and t1 is not None and abs(t1 - t0) >= settings.temp_change_c:
                add("temperature_changed", f"Temperatura: {t0:.1f} °C → {t1:.1f} °C.", dev=dev,
                    before=t0, after=t1, delta=round(t1 - t0, 2))
            h0, h1 = _num(old.caps.get("humidityValue")), _num(dev.caps.get("humidityValue"))
            if h0 is not None and h1 is not None and abs(h1 - h0) >= settings.humidity_change_pct:
                add("humidity_changed", f"Humidade: {h0:.0f}% → {h1:.0f}%.", dev=dev,
                    before=h0, after=h1)

        if dev.role == "camera":
            for cap, type_, msg in (("motionDetected", "camera_motion", "Movimento detetado pela câmara."),
                                    ("soundDetected", "camera_sound", "Som detetado pela câmara.")):
                if changed(cap) and dev.caps.get(cap) not in ("", None, False):
                    add(type_, msg, "warning" if curr.armed else "info", dev,
                        value=dev.caps.get(cap), armed=curr.armed)

        if dev.role == "plug":
            if changed("power"):
                on = dev.caps.get("power") == "on"
                add("plug_switched", f"Aquecedor (tomada) {'ligado' if on else 'desligado'}.",
                    dev=dev, on=on)
            p0, p1 = _num(old.caps.get("instantPower")), _num(dev.caps.get("instantPower"))
            if p0 is not None and p1 is not None and abs(p1 - p0) >= settings.plug_power_change_w:
                add("plug_power_changed", f"Consumo da tomada: {p0:.0f} W → {p1:.0f} W.",
                    dev=dev, before=p0, after=p1)

        if dev.role == "light":
            light_caps = ("power", "color_rgb", "brightness", "workMode")
            if any(changed(c) for c in light_caps):
                add("light_changed", "A luz mudou de estado.", dev=dev,
                    **{c: dev.caps.get(c) for c in light_caps})
    return ev


def _mode_pt(mode: str) -> str:
    return {"DISARMED": "desarmada", "STAY": "armada (em casa)", "AWAY": "armada (ausente)"}.get(mode, mode)


# ------------------------------------------------------------- trend (stateful)

class TemperatureTrend:
    """Emits temperature_dropping once when the temperature falls by >= temp_drop_c within
    temp_drop_window_s; re-arms when the temperature stops falling."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.samples: deque[tuple[float, float]] = deque()
        self.active = False

    def update(self, state: HomeState) -> list[Event]:
        t = state.temperature
        if t is None:
            return []
        self.samples.append((state.ts, t))
        while self.samples and state.ts - self.samples[0][0] > self.s.temp_drop_window_s:
            self.samples.popleft()
        peak = max(v for _, v in self.samples)
        drop = peak - t
        if not self.active and drop >= self.s.temp_drop_c:
            self.active = True
            dev = state.by_role("temperature")
            return [Event(
                type="temperature_dropping", severity="warning", ts=state.ts,
                device_id=dev.id if dev else None, role="temperature",
                message=f"A temperatura está a descer: {peak:.1f} °C → {t:.1f} °C "
                        f"em {int(self.s.temp_drop_window_s // 60)} min.",
                data={"from": peak, "to": t, "drop": round(drop, 2),
                      "window_open": state.contact_open, "heater_on": state.plug_on},
            )]
        if self.active and drop < self.s.temp_drop_c / 2:
            self.active = False
        return []
