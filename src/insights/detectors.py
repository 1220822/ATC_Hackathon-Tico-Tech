"""Pattern detectors over the real history (code only, no LLM).

    candidates = run_all(history, events, state, prefs, min_days=2)   -> list[Insight]

Detectors:
  daily_pattern    the same metric of a room is high / low / rises / falls in the same time window on
                   >= min_days different days (humidity in the Quarto at night, cold mornings);
  window_heater    window open while the heater plug is on, on >= min_days days (waste);
  standby          plug on with low but non-zero power (standby) for hours, on >= min_days days;
  repeated_action  the user switches the plug on at about the same time on >= min_days days.

Every Insight carries its evidence (days, values, window), a confidence, an action that uses only
devices that exist (or says honestly which device is missing) and the estimated benefit. The text
is a faithful PT-PT template; an LLM may rephrase it (src.insights.service), never invent facts.
"""

import statistics
import time
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ..perception.state import HomeState, de, em, fmt

WINDOWS = [  # (key, label, start hour, end hour); a window may cross midnight
    ("noite", "à noite", 22, 7),
    ("manha", "de manhã", 6, 9),
    ("tarde", "à tarde", 14, 18),
    ("fim_dia", "ao fim do dia", 18, 22),
]
MIN_COVERAGE = 0.4  # a day counts only if at least 40 % of its window has readings
HUMIDITY_HIGH = 70.0
TEMP_COLD = 19.0


class Evidence(BaseModel):
    room: str = ""
    room_name: str = ""
    metric: str = ""
    window: str = ""          # "22:00–07:00"
    window_label: str = ""    # "à noite"
    days: list[str] = Field(default_factory=list)          # "18/11"
    values: list[float] = Field(default_factory=list)      # one value per day (what was observed)
    threshold: float | None = None
    unit: str = ""
    days_with_data: int = 0   # how many days had enough data to be checked
    spans: list[tuple[float, float]] = Field(default_factory=list)  # epoch windows, for the chart


class ProposedAction(BaseModel):
    kind: Literal["preference", "scenario", "schedule"]
    description: str
    pref: dict | None = None        # {"scope", "key", "value"}
    rule: dict | None = None        # RuleRequest dump (built into a TT_ scenario when accepted)
    schedule: dict | None = None    # {"device_id", "at": "HH:MM", "value": "on"|"off"}
    missing_device: str | None = None  # product recommendation (honest: the house does not have it)


class Insight(BaseModel):
    key: str
    type: Literal["daily_pattern", "window_heater", "standby", "repeated_action"]
    agent: Literal["conforto", "energia"]
    evidence: Evidence
    confidence: float = Field(ge=0, le=1)
    proposed_action: ProposedAction | None
    benefit: str
    text: str


def _day_label(d: datetime) -> str:
    return d.strftime("%d/%m")


def _window_span(day: datetime, start_h: int, end_h: int) -> tuple[float, float]:
    """The window that starts on `day` (crossing midnight ends on the next day)."""
    a = day.replace(hour=start_h, minute=0, second=0, microsecond=0)
    b = day.replace(hour=end_h, minute=0, second=0, microsecond=0)
    if end_h <= start_h:
        b += timedelta(days=1)
    return a.timestamp(), b.timestamp()


def _days_back(now: float, days: int) -> list[datetime]:
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return [today - timedelta(days=i) for i in range(days, -1, -1)]


def _window_text(start_h: int, end_h: int) -> str:
    return f"{start_h:02d}:00–{end_h:02d}:00"


def daily_patterns(history, state: HomeState, prefs, now: float, min_days: int, lookback: int = 14,
                   store_interval_s: float = 30.0) -> list[Insight]:
    out: list[Insight] = []
    for dev in state.devices_by_role("temperature"):
        room = state.room(dev.room_id)
        if room is None:
            continue
        hum_max = (prefs.humidity_max(room.id)[0] if prefs else None) or HUMIDITY_HIGH
        for wkey, wlabel, sh, eh in WINDOWS:
            per_day: dict[str, list] = {"hum_high": [], "hum_rise": [], "cold": [], "drop": []}
            checked = 0
            checked_days: list[str] = []
            for day in _days_back(now, lookback):
                a, b = _window_span(day, sh, eh)
                if b > now:  # window not finished yet
                    continue
                expected = (b - a) / max(store_interval_s, 1)
                hum = history.aggregate(room.id, "humidity", 900, "avg", a, b)
                tmp = history.aggregate(room.id, "temperature", 900, "avg", a, b)
                n_h = sum(x["n"] for x in hum)
                if n_h < expected * MIN_COVERAGE:
                    continue
                checked += 1
                label = _day_label(day)
                checked_days.append(label)
                hv = [x["value"] for x in hum]
                mean_h = statistics.fmean(hv)
                if mean_h > hum_max:
                    per_day["hum_high"].append((label, round(max(hv), 1), (a, b)))
                if len(hv) >= 4 and statistics.fmean(hv[-2:]) - statistics.fmean(hv[:2]) >= 5:
                    per_day["hum_rise"].append((label, round(statistics.fmean(hv[-2:]) - statistics.fmean(hv[:2]), 1), (a, b)))
                tv = [x["value"] for x in tmp]
                if tv:
                    target = None
                    if prefs is not None:
                        mid = datetime.fromtimestamp(a + (b - a) / 2)
                        target = prefs.setpoint(room.id, mid)[0]
                    cold_line = (target - 1) if target else TEMP_COLD
                    if statistics.fmean(tv) < cold_line:
                        per_day["cold"].append((label, round(min(tv), 1), (a, b)))
                    if len(tv) >= 4 and statistics.fmean(tv[:2]) - statistics.fmean(tv[-2:]) >= 1.0:
                        per_day["drop"].append((label, round(statistics.fmean(tv[:2]) - statistics.fmean(tv[-2:]), 1), (a, b)))
            win = _window_text(sh, eh)
            hits = per_day["hum_high"]
            if len(hits) >= min_days:
                out.append(_humidity_insight(room, wkey, wlabel, win, hits, checked_days, hum_max, "high"))
            elif len(per_day["hum_rise"]) >= min_days:
                out.append(_humidity_insight(room, wkey, wlabel, win, per_day["hum_rise"], checked_days, hum_max, "rise"))
            if len(per_day["cold"]) >= min_days:
                out.append(_cold_insight(room, wkey, wlabel, sh, eh, win, per_day["cold"], checked_days, state, prefs))
    return out


def _quando(days: list[str], checked_days: list[str], wkey: str, wlabel: str) -> str:
    """Faithful time phrase: "nas últimas 2 noites" only if they really are the last 2 nights with
    data; otherwise the dates. Never "todas as noites"."""
    n = len(days)
    unit = "noites" if wkey == "noite" else "dias"
    tail = "" if wkey == "noite" else f" ({wlabel})"
    if checked_days[-n:] == days:
        if n == len(checked_days):
            return f"nas {n} {unit} que tenho registadas{tail}"
        return f"nas últimas {n} {unit}{tail}"
    return f"em {n} das últimas {len(checked_days)} {unit} ({', '.join(days)}){tail}"


def _humidity_insight(room, wkey, wlabel, win, hits, checked_days, hum_max, mode) -> Insight:
    checked = len(checked_days)
    n = len(hits)
    days = [h[0] for h in hits]
    vals = [h[1] for h in hits]
    if mode == "high":
        fact = (f"A humidade {em(room.name)} passou de {fmt(hum_max, 0)} % {_quando(days, checked_days, wkey, wlabel)} "
                f"(máximos de {', '.join(fmt(v, 0) + ' %' for v in vals)}).")
    else:
        fact = (f"A humidade {em(room.name)} subiu {_quando(days, checked_days, wkey, wlabel)} "
                f"(+{', +'.join(fmt(v, 0) for v in vals)} pontos).")
    rule = {"condicoes": [{"sensor": "humidade", "divisao": room.name, "operador": ">", "valor": hum_max}],
            "acoes": [], "avisar": True}
    action = ProposedAction(
        kind="scenario", rule=rule, missing_device="desumidificador",
        description=(f"criar a regra «se a humidade {em(room.name)} passar de {fmt(hum_max, 0)} %, avisa-me», para "
                     "arejares a divisão a tempo"))
    text = (f"{fact} Não tens desumidificador: se tiveres um, liga-o à tomada inteligente e eu passo a ligá-lo "
            f"quando a humidade subir. Para já, queres que te avise quando passar de {fmt(hum_max, 0)} %?")
    return Insight(
        key=f"daily_pattern:{room.id}:humidity_{mode}:{wkey}", type="daily_pattern", agent="conforto",
        evidence=Evidence(room=room.id, room_name=room.name, metric="humidity", window=win, window_label=wlabel,
                          days=days, values=vals, threshold=hum_max, unit="%", days_with_data=checked,
                          spans=[h[2] for h in hits]),
        confidence=round(min(0.95, 0.4 + 0.15 * n + (0.1 if checked and n == checked else 0)), 2),
        proposed_action=action,
        benefit="Humidade alta durante a noite favorece bolor e ácaros; arejar a tempo evita-o.",
        text=text)


def _cold_insight(room, wkey, wlabel, sh, eh, win, hits, checked_days, state, prefs) -> Insight:
    checked = len(checked_days)
    n = len(hits)
    vals = [h[1] for h in hits]
    plug = state.by_role("plug", room.id) or state.by_role("plug")
    target, _ = prefs.setpoint(room.id) if prefs else (None, None)
    line = (target - 1) if target else TEMP_COLD
    fact = (f"{em(room.name).capitalize()} esteve abaixo de {fmt(line, 0)} °C {_quando([h[0] for h in hits], checked_days, wkey, wlabel)} "
            f"(mínimos de {', '.join(fmt(v, 1) + ' °C' for v in vals)}).")
    action = None
    if plug is not None:
        rule = {"condicoes": [{"sensor": "temperatura", "divisao": room.name, "operador": "<", "valor": line}],
                "acoes": [{"dispositivo": "aquecedor", "divisao": plug.room_name, "acao": "ligar"}], "avisar": True}
        action = ProposedAction(kind="scenario", rule=rule,
                                description=f"ligar o aquecedor {de(plug.room_name)} quando a temperatura {em(room.name)} "
                                            f"descer abaixo de {fmt(line, 0)} °C")
    return Insight(
        key=f"daily_pattern:{room.id}:cold:{wkey}", type="daily_pattern", agent="conforto",
        evidence=Evidence(room=room.id, room_name=room.name, metric="temperature", window=win, window_label=wlabel,
                          days=[h[0] for h in hits], values=vals, threshold=line, unit="°C", days_with_data=checked,
                          spans=[h[2] for h in hits]),
        confidence=round(min(0.95, 0.4 + 0.15 * n), 2), proposed_action=action,
        benefit="A divisão fica confortável sem teres de ligar o aquecedor à mão.",
        text=fact + (f" Queres que eu {action.description}?" if action else ""))


def _intervals(rows: list[dict], predicate, max_gap_s: float) -> list[tuple[float, float]]:
    """Merge consecutive readings where predicate(value) holds into [start, end) intervals."""
    out: list[tuple[float, float]] = []
    start = prev = None
    for r in rows:
        ok = predicate(r["value"])
        if ok and start is None:
            start = r["ts"]
        elif ok and prev is not None and r["ts"] - prev > max_gap_s:
            out.append((start, prev))
            start = r["ts"]
        elif not ok and start is not None:
            out.append((start, r["ts"]))
            start = None
        prev = r["ts"]
    if start is not None and prev is not None:
        out.append((start, prev))
    return out


def _overlap(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out = []
    for s1, e1 in a:
        for s2, e2 in b:
            s, e = max(s1, s2), min(e1, e2)
            if e > s:
                out.append((s, e))
    return out


def window_heater(history, state: HomeState, now: float, min_days: int, lookback: int = 14,
                  max_gap_s: float = 120.0) -> list[Insight]:
    contact = state.by_role("contact")
    plug = state.by_role("plug")
    if contact is None or plug is None:
        return []
    start = now - lookback * 86400
    opened = _intervals(history.query(None, "open", start, now + 1, contact.id), lambda v: v >= 0.5, max_gap_s)
    heater = _intervals(history.query(None, "on", start, now + 1, plug.id), lambda v: v >= 0.5, max_gap_s)
    both = [(s, e) for s, e in _overlap(opened, heater) if e - s >= 600]
    by_day: dict[str, list] = {}
    for s, e in both:
        by_day.setdefault(_day_label(datetime.fromtimestamp(s)), []).append((s, e))
    if len(by_day) < min_days:
        return []
    days = sorted(by_day, key=lambda d: by_day[d][0][0])
    hours = [round(sum(e - s for s, e in by_day[d]) / 3600, 1) for d in days]
    wh = sum(history.energy_wh(None, s, e, plug.id) for s, e in both)
    n = len(days)
    noun = contact.opening_noun  # porta | janela (3D layout)
    rule = {"condicoes": [{"sensor": "janela", "divisao": contact.room_name, "valor": "aberta"},
                          {"sensor": "aquecedor", "divisao": plug.room_name, "valor": "ligado"}],
            "acoes": [{"dispositivo": "aquecedor", "divisao": plug.room_name, "acao": "desligar"}], "avisar": True}
    consumo = (f"consumiu {fmt(wh / 1000, 2)} kWh nesses períodos" if wh >= 1
               else "quase não consumiu nesses períodos, mas podia ter aquecido para a rua")
    text = (f"Em {n} dias, a {noun} {de(contact.room_name)} esteve aberta com o aquecedor {de(plug.room_name)} "
            f"ligado ({', '.join(f'{fmt(h, 1)} h a {d}' for d, h in zip(days, hours))}). O aquecedor {consumo}. "
            f"Queres que eu desligue o aquecedor sempre que a {noun} abrir?")
    return [Insight(
        key=f"window_heater:{contact.id}:{plug.id}", type="window_heater", agent="energia",
        evidence=Evidence(room=plug.room_id, room_name=plug.room_name, metric="power_w", window="", days=days,
                          values=hours, unit="h", days_with_data=lookback, spans=both[:20]),
        confidence=round(min(0.95, 0.5 + 0.15 * n), 2),
        proposed_action=ProposedAction(kind="scenario", rule=rule,
                                       description=f"desligar o aquecedor {de(plug.room_name)} quando a {noun} "
                                                   f"{de(contact.room_name)} abrir"),
        benefit=f"Evita aquecer com a {noun} aberta ({fmt(wh / 1000, 2)} kWh nestas ocorrências).",
        text=text)]


def standby(history, state: HomeState, now: float, min_days: int, lookback: int = 14,
            low_w: float = 5.0, min_hours: float = 2.0, max_gap_s: float = 120.0) -> list[Insight]:
    plug = state.by_role("plug")
    if plug is None:
        return []
    rows = history.query(None, "power_w", now - lookback * 86400, now + 1, plug.id)
    spans = [(s, e) for s, e in _intervals(rows, lambda v: 0 < v < low_w, max_gap_s)]
    by_day: dict[str, float] = {}
    for s, e in spans:
        d = _day_label(datetime.fromtimestamp(s))
        by_day[d] = by_day.get(d, 0) + (e - s) / 3600
    days = [d for d, h in by_day.items() if h >= min_hours]
    if len(days) < min_days:
        return []
    wh = sum(history.energy_wh(None, s, e, plug.id) for s, e in spans)
    hours = [round(by_day[d], 1) for d in days]
    return [Insight(
        key=f"standby:{plug.id}", type="standby", agent="energia",
        evidence=Evidence(room=plug.room_id, room_name=plug.room_name, metric="power_w", days=days, values=hours,
                          unit="h", threshold=low_w, days_with_data=lookback, spans=spans[:20]),
        confidence=round(min(0.9, 0.4 + 0.15 * len(days)), 2),
        proposed_action=ProposedAction(kind="schedule", schedule={"device_id": plug.id, "at": "01:00", "value": "off"},
                                       description=f"desligar a tomada {de(plug.room_name)} à 1h da manhã"),
        benefit=f"O standby gastou {fmt(wh / 1000, 3)} kWh nestes dias.",
        text=(f"A tomada {de(plug.room_name)} ficou em standby (menos de {fmt(low_w, 0)} W) durante horas em "
              f"{len(days)} dias ({', '.join(f'{fmt(h, 1)} h a {d}' for d, h in zip(days, hours))}). "
              f"Queres que a desligue à 1h da manhã?"))]


def repeated_action(events, state: HomeState, now: float, min_days: int, lookback: int = 14,
                    tolerance_min: int = 30, exclude_ts: list[float] | None = None) -> list[Insight]:
    plug = state.by_role("plug")
    if plug is None or events is None:
        return []
    # only switches made by the user: an event up to 2 min after one of our executed decisions is ours
    ours = exclude_ts or []
    on = [e for e in events.by_day("plug_switched", lookback, plug.id)
          if e["data"].get("on") and not any(0 <= e["ts"] - t <= 120 for t in ours)]
    by_day: dict[str, float] = {}
    for e in on:
        d = datetime.fromtimestamp(e["ts"])
        by_day.setdefault(_day_label(d), d.hour * 60 + d.minute)
    if len(by_day) < min_days:
        return []
    mins = list(by_day.values())
    centre = statistics.median(mins)
    close = {d: m for d, m in by_day.items() if abs(m - centre) <= tolerance_min}
    if len(close) < min_days:
        return []
    at = f"{int(centre // 60):02d}:{int(centre % 60) // 5 * 5:02d}"
    days = list(close)
    return [Insight(
        key=f"repeated_action:{plug.id}:on:{at[:2]}", type="repeated_action", agent="energia",
        evidence=Evidence(room=plug.room_id, room_name=plug.room_name, metric="on", days=days,
                          values=[round(m / 60, 2) for m in close.values()], window=at, unit="h",
                          days_with_data=lookback),
        confidence=round(min(0.9, 0.4 + 0.15 * len(days)), 2),
        proposed_action=ProposedAction(kind="schedule", schedule={"device_id": plug.id, "at": at, "value": "on"},
                                       description=f"ligar o aquecedor {de(plug.room_name)} às {at}"),
        benefit="Deixas de ter de o ligar à mão todos os dias.",
        text=(f"Ligaste o aquecedor {de(plug.room_name)} por volta das {at} em {len(days)} dias "
              f"({', '.join(days)}). Queres que passe a ligá-lo eu a essa hora?"))]


def run_all(history, events, state: HomeState, prefs, min_days: int = 2, now: float | None = None,
            store_interval_s: float = 30.0, exclude_ts: list[float] | None = None) -> list[Insight]:
    now = time.time() if now is None else now
    out: list[Insight] = []
    for fn in (lambda: daily_patterns(history, state, prefs, now, min_days, store_interval_s=store_interval_s),
               lambda: window_heater(history, state, now, min_days, max_gap_s=4 * store_interval_s),
               lambda: standby(history, state, now, min_days, max_gap_s=4 * store_interval_s),
               lambda: repeated_action(events, state, now, min_days, exclude_ts=exclude_ts)):
        try:
            out += fn()
        except Exception:  # noqa: BLE001 - one broken detector must not hide the others
            import logging
            logging.getLogger(__name__).exception("detector failed")
    out.sort(key=lambda i: (-i.confidence, -len(i.evidence.days)))
    return out
