"""State of the digital twin: layout + normalised device values + security + events/decisions.

Built only from SharedMemory (the perception loop's latest HomeState, events and decisions), so
the 3D view never polls the MCP itself. Served by GET /twin/state and pushed by GET /twin/stream
(Server-Sent Events): a "snapshot" first, then "state" after every perception poll and one
"event"/"decision" message per new item.
"""

import asyncio
import json
import time
from typing import Any, AsyncIterator, Awaitable, Callable

from ..config import Settings
from ..coordinator import UI_ACTIONS
from ..memory import Decision, SharedMemory
from ..perception.state import (AlarmEntry, DeviceState, Event, HomeState, _alarm_device, _mode_pt,
                                _num, classify_alarm, hue_rgb)
from .layout import load_layout, resolve_layout

SMOKE_ACTIVE_S = 300  # an unanswered smoke ALARM counts as active this long even if isAlarmed is false

AREA_BY_EVENT = {
    "contact_opened": "Segurança", "contact_closed": "Segurança", "smoke_detected": "Emergência",
    "security_alarm": "Segurança", "alarm_cancelled": "Segurança", "security_mode_changed": "Segurança",
    "alarm_state_changed": "Segurança", "hub_alarm": "Segurança", "tamper_alarm": "Segurança",
    "camera_motion": "Segurança", "camera_sound": "Segurança", "camera_analysis": "Segurança",
    "temperature_changed": "Conforto", "temperature_dropping": "Conforto", "humidity_changed": "Conforto",
    "light_changed": "Conforto", "plug_switched": "Energia", "plug_power_changed": "Energia",
    "device_offline": "Sistema", "device_online": "Sistema", "tick": "Sistema",
}


def _recent_flag(value: Any, now: float, window_s: float) -> bool:
    """Camera motionDetected/soundDetected: "" = nothing; a ms timestamp = active for window_s."""
    if value in ("", None, False, 0, "0"):
        return False
    ts = _num(value)
    if ts is None:
        return True
    ts = ts / 1000 if ts > 1e11 else ts
    return now - ts <= window_s


def active_alarm(state: HomeState, settings: Settings, now: float) -> dict:
    """What is alarming right now. Smoke comes from the newest alarm history entry (the real smoke
    test is ALARM/EMERGENCY with an empty device), cleared by a newer CANCEL."""
    newest: AlarmEntry | None = state.alarms[0] if state.alarms else None
    smoke = intrusion = False
    smoke_dev = None
    if newest is not None and newest.event_type == "ALARM":
        kind = classify_alarm(newest, settings)
        fresh = state.is_alarmed or now - newest.ts <= SMOKE_ACTIVE_S
        if kind == "smoke" and fresh:
            smoke = True
            smoke_dev, _ = _alarm_device(newest, kind, state)
        elif kind == "intrusion" and state.is_alarmed:
            intrusion = True
    hub = any(d.caps.get("alarmState") == "alarm" for d in state.devices_by_role("hub"))
    return {"any": state.is_alarmed or hub or smoke, "smoke": smoke, "intrusion": intrusion, "hub": hub,
            "smoke_device_id": smoke_dev.id if smoke_dev else None,
            "smoke_room": smoke_dev.room_id if smoke_dev else None}


def device_values(d: DeviceState, alarm: dict, now: float, camera_active_s: float) -> dict:
    """Normalised values per device type (units in the key names)."""
    c = d.caps
    v: dict[str, Any] = {}
    if d.role == "temperature":
        v.update(temperature_c=_num(c.get("temperatureValue")), humidity_pct=_num(c.get("humidityValue")))
    elif d.role == "contact":
        state = c.get("sensorState")
        v.update(open=None if state is None else state == "alarm", tamper=c.get("tamperState") == "alarm")
    elif d.role == "light":
        rgb = hue_rgb(c.get("color_rgb"))
        v.update(on=c.get("power") == "on", brightness=_num(c.get("brightness")),
                 color_rgb=list(rgb) if rgb else None, color_rgb_reported=c.get("color_rgb"),
                 work_mode=c.get("workMode"))
    elif d.role == "plug":
        v.update(on=c.get("power") == "on", power_w=_num(c.get("instantPower")),
                 today_kwh=_num(c.get("todayConsumption")))
    elif d.role == "smoke":
        v.update(alarm=alarm["smoke"] and alarm["smoke_device_id"] in (d.id, None))
    elif d.role == "hub":
        v.update(alarm=c.get("alarmState") == "alarm", tamper=c.get("tamperState") == "alarm",
                 charging=c.get("chargeState"), network=c.get("networkMode"))
    elif d.role == "camera":
        v.update(motion=_recent_flag(c.get("motionDetected"), now, camera_active_s),
                 sound=_recent_flag(c.get("soundDetected"), now, camera_active_s))
    battery = c.get("batteryLevel", c.get("batteryStatus"))
    if battery is not None:
        v["battery"] = battery  # % (hub) or low | medium | high (sensors)
    return v


def event_view(e: Event) -> dict:
    return {**e.model_dump(), "area": AREA_BY_EVENT.get(e.type, "Sistema"), "kind": "event"}


def decision_view(d: Decision) -> dict:
    return {**d.model_dump(), "area": d.agent, "kind": "decision"}


def twin_state(memory: SharedMemory, settings: Settings, n: int = 30, now: float | None = None,
               with_history: bool = True) -> dict | None:
    """Full twin state, or None before the first perception poll."""
    state = memory.state
    if state is None:
        return None
    now = time.time() if now is None else now
    from .edit import active_layout_file
    layout = load_layout(active_layout_file(settings))
    placed = resolve_layout(layout, state)
    alarm = active_alarm(state, settings, now)
    devices = []
    for dev_id, p in placed["devices"].items():
        d = state.devices[dev_id]
        devices.append({
            "id": d.id, "name": d.name, "type": d.type, "role": d.role, "room": d.room_id,
            "room_name": d.room_name, "online": d.online, "last_seen": d.last_seen,
            **{k: p[k] for k in ("position", "rotation_y", "mount", "opening", "target", "placed")},
            "values": device_values(d, alarm, now, layout.display.camera_active_s),
        })
    rooms = []
    for r in placed["rooms"]:
        temp = state.by_role("temperature", r["id"])
        rooms.append({**r, "temperature_c": _num(temp.caps.get("temperatureValue")) if temp else None,
                      "smoke": alarm["smoke"] and alarm["smoke_room"] == r["id"]})
    out = {
        "ts": state.ts, "home_name": state.home_name, "mode": "simulado" if settings.simulate else "mcp",
        "dry_run": settings.dry_run,
        "actions": {role: list(caps["power"]) for role, caps in UI_ACTIONS.items()},  # what the UI may request
        "security": {"status": state.security_status, "label": _mode_pt(state.security_status),
                     "armed": state.armed, "is_alarmed": state.is_alarmed},
        "alarm": alarm,
        "display": layout.display.model_dump(),
        "rooms": rooms, "devices": devices, "warnings": placed["warnings"],
    }
    out["agents"] = memory.agent_status()
    if with_history:
        out["events"] = [event_view(e) for e in memory.recent_events(n) if e.type != "tick"]
        out["decisions"] = [decision_view(d) for d in memory.recent_decisions(n)]
    return out


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def twin_stream(memory: SharedMemory, settings: Settings,
                      disconnected: Callable[[], Awaitable[bool]] | None = None,
                      check_s: float = 0.25, heartbeat_s: float = 15.0,
                      max_messages: int | None = None) -> AsyncIterator[str]:
    """SSE messages. Reads SharedMemory every check_s (cheap, in-process); the MCP is only ever
    polled by the perception loop."""
    sent = 0
    seq = memory.seq
    version = -1
    last_beat = time.monotonic()
    snapshot_sent = False
    while max_messages is None or sent < max_messages:
        if disconnected is not None and await disconnected():
            return
        out: list[str] = []
        if not snapshot_sent:
            st = twin_state(memory, settings)
            if st is not None:
                out.append(_sse("snapshot", st))
                snapshot_sent, version = True, memory.version
        else:
            if memory.version != version:
                version = memory.version
                st = twin_state(memory, settings, with_history=False)
                if st is not None:
                    out.append(_sse("state", st))
            new_events = memory.events_after(seq)
            new_decisions = memory.decisions_after(seq)
            for i, e in new_events:
                if e.type != "tick":
                    out.append(_sse("event", event_view(e)))
            for i, d in new_decisions:
                out.append(_sse("decision", decision_view(d)))
            seq = max([seq] + [i for i, _ in new_events] + [i for i, _ in new_decisions])
        if not out and time.monotonic() - last_beat >= heartbeat_s:
            out.append(": ping\n\n")
        for msg in out:
            yield msg
            last_beat = time.monotonic()
            sent += 1
            if max_messages is not None and sent >= max_messages:
                return
        await asyncio.sleep(check_s)
