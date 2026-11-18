"""Read-only history tools for the agents (OpenAI-compatible function schemas).

    Energia  -> consumo_por_hora(divisao?, horas)         kWh per hour and room, from power samples
                (sample-and-hold: each sample lasts until the next one, at most 2 x STORE_INTERVAL_S,
                so periods without recording count as no data, not as consumption)
    Conforto -> tendencia_temperatura(divisao, horas, periodo_min)   averages + slope in °C/h

The database is opened with SQLite mode=ro, so these tools cannot write even by mistake.
Rooms can be given by name ("Quarto") or id; names are resolved against the latest HomeState.
"""

import json
import time
from typing import Any, Callable

from ..perception.state import HomeState
from .history import History

SCHEMAS: dict[str, dict] = {
    "consumo_por_hora": {
        "type": "function",
        "function": {
            "name": "consumo_por_hora",
            "description": "Consumo elétrico estimado (kWh) por hora e por divisão nas últimas horas, "
                           "calculado a partir da potência das tomadas. Só leitura.",
            "parameters": {
                "type": "object",
                "properties": {
                    "divisao": {"type": "string", "description": "Nome ou id da divisão; omitir = todas."},
                    "horas": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24},
                },
            },
        },
    },
    "tendencia_temperatura": {
        "type": "function",
        "function": {
            "name": "tendencia_temperatura",
            "description": "Temperatura média por intervalo e tendência (°C/h) de uma divisão. Só leitura.",
            "parameters": {
                "type": "object",
                "properties": {
                    "divisao": {"type": "string", "description": "Nome ou id da divisão."},
                    "horas": {"type": "number", "minimum": 0.25, "maximum": 72, "default": 3},
                    "periodo_min": {"type": "integer", "minimum": 1, "maximum": 240, "default": 15},
                },
                "required": ["divisao"],
            },
        },
    },
}

AGENT_TOOLS = {"energia": ["consumo_por_hora"], "conforto": ["tendencia_temperatura"]}


class HistoryTools:
    def __init__(self, history_path, state: Callable[[], HomeState | None], interval_s: float = 30.0):
        self.history = History(history_path, readonly=True)
        self.state = state  # e.g. lambda: memory.state
        self.max_gap_s = 2 * interval_s  # STORE_INTERVAL_S of the recorder

    def schemas(self, agent: str) -> list[dict]:
        return [SCHEMAS[n] for n in AGENT_TOOLS[agent]]

    def call(self, name: str, arguments: str | dict | None = None) -> dict:
        """Dispatch a tool call from the LLM; errors come back as {"erro": ...}."""
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        fn = {"consumo_por_hora": self.consumo_por_hora,
              "tendencia_temperatura": self.tendencia_temperatura}.get(name)
        if fn is None:
            return {"erro": f"ferramenta desconhecida: {name}"}
        if not self.history.path.exists():
            return {"erro": "ainda não há histórico gravado"}
        try:
            return fn(**args)
        except (TypeError, ValueError) as e:
            return {"erro": str(e)}

    # -- helpers ---------------------------------------------------------------
    def _room(self, key: str) -> tuple[str, str]:
        st = self.state()
        r = st.room(key) if st else None
        if r is None and st is not None:
            raise ValueError(f"divisão desconhecida: {key}")
        return (r.id, r.name) if r else (key, key)

    def _rooms_with(self, role: str) -> list[tuple[str, str]]:
        st = self.state()
        if st is None:
            return [(r, r) for r in self.history.rooms()]
        return [(r.id, r.name) for r in st.rooms.values() if st.devices_by_role(role, r.id)]

    # -- tools -----------------------------------------------------------------
    def consumo_por_hora(self, divisao: str | None = None, horas: int = 24) -> dict:
        now = time.time()
        start = (now // 3600 - int(horas) + 1) * 3600
        rooms = [self._room(divisao)] if divisao else self._rooms_with("plug")
        out = {}
        for room_id, name in rooms:
            wh = _energy_wh_by_hour(self.history.query(room_id, "power", start, now + 1), now, self.max_gap_s)
            rows = [{"hora": time.strftime("%Y-%m-%d %H:00", time.localtime(b["ts"])),
                     "kwh": round(wh.get(b["ts"], 0.0) / 1000, 4),
                     "potencia_media_w": round(b["value"], 1), "amostras": b["n"]}
                    for b in self.history.aggregate(room_id, "power", 3600, "avg", start, now + 1)]
            out[name] = {"divisao_id": room_id, "horas": rows,
                         "total_kwh": round(sum(r["kwh"] for r in rows), 4)}
        return {"nota": "estimativa a partir das amostras de potência; horas sem gravação contam 0",
                "divisoes": out}

    def tendencia_temperatura(self, divisao: str, horas: float = 3, periodo_min: int = 15) -> dict:
        room_id, name = self._room(divisao)
        now = time.time()
        start = now - float(horas) * 3600
        buckets = self.history.aggregate(room_id, "temperature", int(periodo_min) * 60, "avg", start, now + 1)
        raw = self.history.query(room_id, "temperature", start, now + 1)
        st = self.state()
        room = st.room(room_id) if st else None
        return {
            "divisao": name, "divisao_id": room_id,
            "setpoint_c": room.setpoint_c if room else None,
            "atual_c": raw[-1]["value"] if raw else None,
            "tendencia_c_por_hora": _slope_per_hour(raw),
            "medias": [{"hora": time.strftime("%H:%M", time.localtime(b["ts"])), "c": round(b["value"], 2)}
                       for b in buckets],
        }


def _energy_wh_by_hour(rows: list[dict[str, Any]], now: float, max_gap_s: float) -> dict[float, float]:
    """Wh per hour bucket; the power of a sample holds until the next one (capped at max_gap_s).
    Several plugs in a room are summed (each sample is one plug)."""
    by_dev: dict[str, list[dict]] = {}
    for r in rows:
        by_dev.setdefault(r["device_id"], []).append(r)
    wh: dict[float, float] = {}
    for samples in by_dev.values():
        for cur, nxt in zip(samples, samples[1:] + [None]):
            dt = min((nxt["ts"] if nxt else now) - cur["ts"], max_gap_s)
            bucket = cur["ts"] // 3600 * 3600
            wh[bucket] = wh.get(bucket, 0.0) + cur["value"] * max(dt, 0) / 3600
    return wh


def _slope_per_hour(rows: list[dict[str, Any]]) -> float | None:
    """Least-squares slope of value over time, in units per hour."""
    if len(rows) < 2:
        return None
    t0 = rows[0]["ts"]
    xs = [(r["ts"] - t0) / 3600 for r in rows]
    ys = [r["value"] for r in rows]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 3)
