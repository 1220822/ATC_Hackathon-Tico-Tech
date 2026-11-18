"""The two memories every agent sees: the house's data history and the user's preferences.

    mem = Memories.open(settings)               # history + events + decisions + preferences (var/home.db)
    ctx = build_context("energia", event, state, mem)
    text = render_context(ctx)                  # compact JSON for the prompt (the LLM may be local)

The context is small on purpose:
  dados        state of the room(s) involved; 2 h trend in 15 min buckets; energy today/yesterday;
               up to 5 recent relevant events
  preferencias active preferences that apply (room, its devices, global), each with its id
  decisoes     the last 3 similar decisions and how the user answered them
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .perception.state import Event, HomeState
from .prefs.models import CATEGORY_PT, KEY_LABEL_PT, LEVEL_PT
from .prefs.store import PrefStore
from .storage.decisions import DecisionStore
from .storage.events import EventStore
from .storage.history import History

TREND_S = 2 * 3600
BUCKET_S = 15 * 60
AGENT_METRICS = {
    "energia": ("power_w", "temperature"),
    "conforto": ("temperature", "humidity"),
    "seguranca": ("open",),
    "coordenador": ("temperature", "humidity", "power_w"),
}
AGENT_ROLES = {"energia": ("plug",), "conforto": ("temperature",), "seguranca": ("contact", "camera", "hub")}
RESPONSE_PT = {"approved": "aprovou", "rejected": "rejeitou", "modified": "alterou", None: "sem resposta"}
STATUS_PT = {"executed": "executada", "pending": "pendente", "rejected": "recusada", "dry_run": "dry-run",
             "vetoed": "vetada"}


@dataclass
class Memories:
    history: History
    events: EventStore
    decisions: DecisionStore
    prefs: PrefStore

    @classmethod
    def open(cls, path: Path | str) -> "Memories":
        History(path)  # schema
        return cls(History(path), EventStore(path), DecisionStore(path), PrefStore(path))


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _r(v, nd=1):
    return None if v is None else round(float(v), nd)


def describe_pref(p: dict) -> str:
    """One PT-PT line for a preference (used in prompts, explanations and the UI)."""
    v, key = p["value"], p["key"]
    if key == "setpoint_c":
        parts = [f"{p2['c']:g} °C das {p2['start']} às {p2['end']}" for p2 in v.get("periods", [])]
        if v.get("c") is not None:
            parts.append(f"{v['c']:g} °C no resto do dia" if parts else f"{v['c']:g} °C")
        return "temperatura alvo " + ", ".join(parts)
    if key == "comfort_range_c":
        return f"conforto entre {v['min_c']:g} e {v['max_c']:g} °C"
    if key == "humidity_max_pct":
        return f"humidade máxima {v['pct']:g} %"
    if key == "autonomy":
        if v.get("kind"):
            return f"{LEVEL_PT.get(v['level'], v['level'])}: {v.get('description') or v['kind']}"
        return f"autonomia {CATEGORY_PT.get(v['category'], v['category'])}: {LEVEL_PT.get(v['level'], v['level'])}"
    if key == "quiet_hours":
        return f"sem sugestões entre as {v['start']} e as {v['end']}"
    if key == "rule":
        return f"regra {v['name']}: {v['description']}"
    if key == "layout":
        return f"disposição 3D: {v['description']}"
    if key == "suggestion_muted":
        return f"não sugerir: {v.get('description') or v['insight_key']}"
    if key == "note":
        return f"nota: {v['text']}"
    return f"{KEY_LABEL_PT.get(key, key)}: {json.dumps(v, ensure_ascii=False)}"


def scope_label(scope: str, state: HomeState | None) -> str:
    if scope == "global":
        return "casa"
    kind, _, ident = scope.partition(":")
    if state is not None:
        if kind == "room" and (r := state.room(ident)):
            return r.name
        if kind == "device" and ident in state.devices:
            d = state.devices[ident]
            return f"{d.name} ({d.room_name})"
    return scope


def _rooms_for(agent: str, trigger: Event | None, state: HomeState) -> list[str]:
    if trigger is not None and trigger.room and state.room(trigger.room):
        rooms = [trigger.room]
    else:
        rooms = []
    for role in AGENT_ROLES.get(agent, ()):
        for d in state.devices_by_role(role):
            if d.room_id and d.room_id not in rooms:
                rooms.append(d.room_id)
    return rooms[:2] or list(state.rooms)[:2]


def _trend(history: History, room: str, metric: str, now: float) -> list:
    rows = history.aggregate(room, metric, BUCKET_S, "avg", now - TREND_S, now + 1)
    nd = 0 if metric == "power_w" else (2 if metric == "open" else 1)
    return [[_hm(b["ts"]), _r(b["value"], nd)] for b in rows]


def energy_summary(state: HomeState, history: History | None, now: float | None = None) -> dict:
    """Energy today and yesterday: the plugs' own counters (MCP) and our integration of the power."""
    now = time.time() if now is None else now
    out: dict = {"ontem_kwh_casa": state.energy.yesterday_consumption_kwh}
    plugs = state.devices_by_role("plug")
    today = [d.caps.get("todayConsumption") for d in plugs if d.caps.get("todayConsumption") is not None]
    if today:
        out["hoje_kwh_tomadas"] = round(sum(float(x) for x in today), 3)
    yday = [d.caps.get("yesterdayConsumption") for d in plugs if d.caps.get("yesterdayConsumption") is not None]
    if yday:
        out["ontem_kwh_tomadas"] = round(sum(float(x) for x in yday), 3)
    if history is not None:
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        out["hoje_kwh_medido"] = round(history.energy_wh(None, midnight, now + 1) / 1000, 3)
        out["ontem_kwh_medido"] = round(history.energy_wh(None, midnight - 86400, midnight) / 1000, 3)
    if state.energy.tariff:
        out["tarifa"] = state.energy.tariff
    return out


def applicable_prefs(prefs: PrefStore, state: HomeState, rooms: list[str]) -> list[dict]:
    seen, out = set(), []
    for room in rooms or [None]:
        devs = [d.id for d in state.room_devices(room)] if room else []
        for p in prefs.applicable(room, devs):
            if p["id"] not in seen and p["key"] not in ("layout",):
                seen.add(p["id"])
                out.append(p)
    return out


def build_context(agent: str, trigger: Event | None, state: HomeState, mem: Memories,
                  now: float | None = None, kind: str | None = None) -> dict:
    now = time.time() if now is None else now
    rooms = _rooms_for(agent, trigger, state)
    dados: dict = {"divisoes": {}}
    for rid in rooms:
        r = state.room(rid)
        summary = {k: v for k, v in state.room_summary(rid).items()
                   if k not in ("id", "tipo", "dispositivos", "simulado") and v not in (None, [], {})}
        trend = {}
        for metric in AGENT_METRICS.get(agent, ("temperature",)):
            try:
                t = _trend(mem.history, rid, metric, now)
            except Exception:  # noqa: BLE001 - no history yet
                t = []
            if t:
                trend[metric] = t
        dados["divisoes"][r.name if r else rid] = {"estado": summary, "tendencia_2h_15min": trend}
    try:
        dados["energia"] = energy_summary(state, mem.history, now)
    except Exception:  # noqa: BLE001
        dados["energia"] = energy_summary(state, None, now)
    evs = []
    for rid in rooms:
        for e in mem.events.recent(5, room=rid, since=now - TREND_S):
            if trigger is None or e["id"] != trigger.id:
                evs.append(e)
    evs.sort(key=lambda e: e["ts"], reverse=True)
    dados["eventos_recentes"] = [f"{_hm(e['ts'])} {e['message']}" for e in evs[:5]]

    prefs = [{"id": p["id"], "ambito": scope_label(p["scope"], state), "tipo": p["key"],
              "descricao": describe_pref(p), "origem": p["source"],
              **({"frase": p["original_text"]} if p.get("original_text") else {})}
             for p in applicable_prefs(mem.prefs, state, rooms)]

    similar = mem.decisions.similar(kind=kind, trigger_type=trigger.type if trigger else None,
                                    room=rooms[0] if rooms else None, n=3)
    decs = [{"quando": time.strftime("%d/%m %H:%M", time.localtime(d["ts"])),
             "decisao": (d.get("explanation") or "")[:160],
             "resultado": STATUS_PT.get(d["status"], d["status"]),  # what the Coordinator / guardrails did
             "resposta_do_utilizador": RESPONSE_PT.get(d.get("user_response"), d.get("user_response")),
             **({"comentario_do_utilizador": d["user_comment"]} if d.get("user_comment") else {})} for d in similar]
    return {"dados": dados, "preferencias": prefs, "decisoes_semelhantes": decs}


def render_context(ctx: dict) -> str:
    return json.dumps(ctx, ensure_ascii=False, separators=(",", ":"))
