"""Add N days of plausible history (readings + events) before the oldest reading in the history DB.

    python scripts/seed_history.py              # 3 days, every 30 s (like the recorder), var/home.db
    python scripts/seed_history.py --days 5 --seed 7
    python scripts/seed_history.py --dry-run    # only says what it would write

A daily routine of the real house (ids from var/layout_user.json / get_home): the Quarto heater on in the
morning and in the evening, its light, the Sala door opening when leaving / arriving and, on some evenings,
left open with the heater on (the waste pattern the insights look for); temperature and humidity follow.
Events (contact_opened/closed, plug_switched, light_changed, temperature/humidity_changed) match the
readings, so the timeline and the charts agree. It never writes over time that already has readings.
"""

import argparse
import json
import math
import random
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_settings  # noqa: E402

SALA, QUARTO = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1", "bd792398-7e60-40e9-9b62-11c4255f6c72"
TEMP, PLUG, LIGHT = "54e1ea8e-9ca3-4933-a4dc-a1eb37e6990c", "a82edb80-d92f-44bb-8df0-8e10beeea621", "b667d87c-2566-440a-afce-02d9ec82df1b"
CONTACT, SMOKE, HUB = "20d98aec-c266-4707-bd27-d06bb51733b7", "726d443d-502c-41ab-b9c1-f1a84a02a10c", "ef8015f6-d7af-4da6-b48a-f1d0e7630b3e"
HEATER_W = 1200.0


def at(day: datetime, h: float, rnd: random.Random, jitter_min: float = 10) -> float:
    return (day + timedelta(hours=h, minutes=rnd.uniform(-jitter_min, jitter_min))).timestamp()


def day_plan(day: datetime, i: int, rnd: random.Random) -> dict:
    """Intervals [start, end) of the day: heater on, light on, door open."""
    heater = [(at(day, 7.0, rnd, 8), at(day, 8.25, rnd, 8)), (at(day, 19.0, rnd, 12), at(day, 22.5, rnd, 15))]
    light = [(at(day, 7.0, rnd, 5), at(day, 7.7, rnd, 5)), (at(day, 19.5, rnd, 10), at(day, 23.25, rnd, 15))]
    door = [(at(day, 8.35, rnd, 5), None), (at(day, 18.75, rnd, 8), None)]
    door = [(s, s + rnd.uniform(40, 120)) for s, _ in door]
    if i % 3 != 2:  # two evenings out of three: the door left open ~30 min with the heater on
        s = at(day, 20.0, rnd, 10)
        door.append((s, s + rnd.uniform(25, 40) * 60))
    return {"heater": heater, "light": light, "door": door}


def inside(t: float, spans: list) -> bool:
    return any(s <= t < e for s, e in spans)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--step", type=float, default=30, help="seconds between readings (recorder: 30)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    db = a.db or get_settings().history_db
    rnd = random.Random(a.seed)
    con = sqlite3.connect(db, timeout=30)
    first = con.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
    end = float(first) if first is not None else time.time()
    start = end - a.days * 86400

    days = {}
    d0 = datetime.fromtimestamp(start).replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(int(a.days) + 2):
        d = d0 + timedelta(days=i)
        days[d.date()] = day_plan(d, i, rnd)

    readings, events = [], []

    def event(ts, type_, room, room_name, dev, message, **data):
        events.append((uuid.uuid4().hex[:10], ts, type_, "info", room, room_name, dev, json.dumps(data, ensure_ascii=False), message))

    temp, hum = 19.4, 60.0
    ev_temp, ev_hum = temp, hum
    kwh_today, kwh_yesterday, today = 0.0, 0.004, datetime.fromtimestamp(start).date()
    prev = {"heater": None, "light": None, "door": None}
    t = start
    while t < end:
        dt = datetime.fromtimestamp(t)
        if dt.date() != today:
            kwh_yesterday, kwh_today, today = kwh_today, 0.0, dt.date()
        plan = days[dt.date()]
        h = dt.hour + dt.minute / 60
        heater, light, door = inside(t, plan["heater"]), inside(t, plan["light"]), inside(t, plan["door"])
        # Quarto temperature: the heater pulls it to ~23.5 °C, otherwise it drifts to the outside of the
        # day (cold before dawn, a bit of sun in the afternoon); an open door cools it a little
        passive = 17.6 + 1.6 * max(0.0, math.sin((h - 9) / 10 * math.pi)) if 9 <= h <= 19 else 17.6
        target, k = (23.5, 0.0045) if heater else (passive, 0.0012)
        temp += (target - temp) * k * a.step / 30 - (0.004 if door else 0) + rnd.gauss(0, 0.01)
        # humidity: higher at night with the room closed and the heater off, drops while it heats
        hum_target = 67.5 if (h >= 23 or h < 7) else 58.5
        hum += (hum_target - hum) * (0.004 if not heater else 0.008) * a.step / 30 + rnd.gauss(0, 0.05)
        if heater:
            hum -= 0.01
        power = HEATER_W + rnd.gauss(0, 15) if heater else 0.0
        kwh_today += power * a.step / 3.6e6
        T, H = round(temp, 1), round(hum, 1)
        rows = [(QUARTO, TEMP, "temperature", T), (QUARTO, TEMP, "humidity", H), (QUARTO, TEMP, "battery", 100.0),
                (QUARTO, PLUG, "power_w", round(power, 1)), (QUARTO, PLUG, "on", 1.0 if heater else 0.0),
                (QUARTO, PLUG, "energy_today_kwh", round(kwh_today, 3)),
                (QUARTO, PLUG, "energy_yesterday_kwh", round(kwh_yesterday, 3)),
                (QUARTO, LIGHT, "on", 1.0 if light else 0.0),
                (SALA, CONTACT, "open", 1.0 if door else 0.0), (SALA, CONTACT, "battery", 100.0),
                (SALA, SMOKE, "battery", 100.0), (SALA, HUB, "battery", 100.0),
                ("home", "home", "energy_yesterday_kwh", round(kwh_yesterday, 3))]
        ts = t + rnd.uniform(-0.4, 0.4)
        readings += [(ts, room, res, metric, value) for room, res, metric, value in rows]

        if prev["door"] is not None and door != prev["door"]:
            event(ts, "contact_opened" if door else "contact_closed", SALA, "Sala", CONTACT,
                  f"Porta da Sala {'aberta' if door else 'fechada'}.", open=door, armed=False,
                  security="DISARMED", opening="porta")
        if prev["heater"] is not None and heater != prev["heater"]:
            event(ts, "plug_switched", QUARTO, "Quarto", PLUG,
                  f"Aquecedor (tomada) do Quarto {'ligado' if heater else 'desligado'}.", on=heater)
        if prev["light"] is not None and light != prev["light"]:
            event(ts, "light_changed", QUARTO, "Quarto", LIGHT, "A luz do Quarto mudou de estado.",
                  power="on" if light else "off", color_rgb="255,255,255", brightness=100, workMode="white")
        if abs(T - ev_temp) >= 0.3:
            event(ts, "temperature_changed", QUARTO, "Quarto", TEMP,
                  f"Temperatura no Quarto: {ev_temp:.1f} °C → {T:.1f} °C".replace(".", ",") + ".",
                  before=ev_temp, after=T, delta=round(T - ev_temp, 2))
            ev_temp = T
        if abs(H - ev_hum) >= 5:
            event(ts, "humidity_changed", QUARTO, "Quarto", TEMP, f"Humidade no Quarto: {ev_hum:.0f}% → {H:.0f}%.",
                  before=ev_hum, after=H)
            ev_hum = H
        prev = {"heater": heater, "light": light, "door": door}
        t += a.step

    span = f"{datetime.fromtimestamp(start):%d/%m %H:%M} → {datetime.fromtimestamp(end):%d/%m %H:%M}"
    print(f"{db}: {len(readings)} leituras e {len(events)} eventos, {span}")
    if a.dry_run:
        return
    with con:
        con.executemany("INSERT INTO readings (ts, room, resource_id, metric, value) VALUES (?, ?, ?, ?, ?)", readings)
        con.executemany("INSERT OR IGNORE INTO events (id, ts, type, severity, room, room_name, device_id, data, message) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", events)
    print("gravado.")


if __name__ == "__main__":
    main()
