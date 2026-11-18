"""HTTP API and UI per room (FastAPI). Reads only: shared memory (latest HomeState) and the
SQLite history. It never writes to the MCP.

    python -m src.api                 # SIMULATE from .env (false -> real MCP + simulated rooms)
    python -m src.api --sim           # everything simulated
    python -m src.api --port 8000 --host 127.0.0.1

Endpoints (rooms by id or name):
    GET /                                                     UI: one card per room
    GET /rooms                                                rooms with sensor values
    GET /rooms/status                                         online/offline per device
    GET /rooms/{id}
    GET /historic/rooms/{id}/{metric}?start=&end=             raw readings (epoch seconds)
    GET /historic/aggregated/{agg}/rooms/{id}/{metric}?period_s=&start=&end=
    GET /home/energy                                          current power per room + MCP yesterdayConsumption
Metrics: temperature, humidity, power, contact, light. agg: avg | sum | min | max.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .config import Settings, get_settings
from .memory import SharedMemory
from .perception.state import HomeState, RoomState, _num
from .storage.history import History

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"
log = logging.getLogger(__name__)


def _device_view(d) -> dict:
    return {"id": d.id, "nome": d.name, "papel": d.role, "online": d.online,
            "ultima_comunicacao": d.last_seen, "fonte": d.source}


def room_view(st: HomeState, r: RoomState) -> dict:
    sensors = {k: v for k, v in st.room_summary(r.id).items()
               if k not in ("id", "tipo", "simulado", "dispositivos", "offline")}
    devs = st.room_devices(r.id)
    return {
        "id": r.id, "nome": r.name, "tipo": r.type, "simulado": r.simulated, "setpoint_c": r.setpoint_c,
        "online": all(d.online for d in devs), "offline": [d.name for d in devs if not d.online],
        "sensores": sensors, "dispositivos": [_device_view(d) for d in devs],
    }


def create_app(memory: SharedMemory, settings: Settings) -> FastAPI:
    app = FastAPI(title="Tico&Tech: casa por divisões", version="1.0")
    History(settings.history_db)  # make sure the file and schema exist
    history = History(settings.history_db, readonly=True)

    def state() -> HomeState:
        st = memory.state
        if st is None:
            raise HTTPException(503, "Estado da casa ainda não disponível.")
        return st

    def room(key: str) -> RoomState:
        r = state().room(key)
        if r is None:
            raise HTTPException(404, f"Divisão desconhecida: {key}")
        return r

    def room_id(key: str) -> str:
        st = memory.state
        if st is None:
            return key
        return room(key).id

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def ui():
        return UI_FILE.read_text(encoding="utf-8")

    @app.get("/rooms")
    def rooms():
        st = state()
        return [room_view(st, r) for r in st.rooms.values()]

    @app.get("/rooms/status")  # before /rooms/{room_key}
    def rooms_status():
        st = state()
        return [{"id": r.id, "nome": r.name, "simulado": r.simulated,
                 "online": all(d.online for d in st.room_devices(r.id)),
                 "dispositivos": [{k: v for k, v in _device_view(d).items() if k != "fonte"}
                                  for d in st.room_devices(r.id)]}
                for r in st.rooms.values()]

    @app.get("/rooms/{room_key}")
    def room_detail(room_key: str):
        return room_view(state(), room(room_key))

    @app.get("/historic/rooms/{room_key}/{metric}")
    def historic(room_key: str, metric: str, start: float | None = None, end: float | None = None):
        rid = room_id(room_key)
        try:
            return {"divisao": rid, "metrica": metric, "leituras": history.query(rid, metric, start, end)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/historic/aggregated/{agg}/rooms/{room_key}/{metric}")
    def historic_aggregated(agg: str, room_key: str, metric: str,
                            period_s: float = Query(3600, gt=0), start: float | None = None,
                            end: float | None = None):
        rid = room_id(room_key)
        try:
            rows = history.aggregate(rid, metric, period_s, agg, start, end)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"divisao": rid, "metrica": metric, "agg": agg, "period_s": period_s, "buckets": rows}

    @app.get("/home/energy")
    def home_energy():
        st = state()
        per_room = []
        for r in st.rooms.values():
            plugs = [d for d in st.devices_by_role("plug", r.id) if d.online]
            if not st.devices_by_role("plug", r.id):
                continue
            per_room.append({"id": r.id, "nome": r.name, "simulado": r.simulated,
                             "potencia_w": sum(_num(d.caps.get("instantPower")) or 0.0 for d in plugs)})
        return {
            "divisoes": per_room,
            "potencia_total_w": sum(x["potencia_w"] for x in per_room),
            "consumo_ontem_kwh": st.energy.yesterday_consumption_kwh,  # get_home.energy (MCP)
            "tarifa": st.energy.tariff,
            "moeda": st.energy.tariff_currency,
        }

    return app


def main() -> None:
    p = argparse.ArgumentParser(description="API e UI por divisão")
    p.add_argument("--sim", action="store_true", help="tudo simulado (SIMULATE=true)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.sim:
        os.environ["SIMULATE"] = "true"

    import uvicorn

    from .memory import EventBus
    from .perception.poller import Perception
    from .perception.sources import make_source
    from .storage.history import HistoryRecorder

    settings = get_settings()
    memory = SharedMemory()
    perception = Perception(make_source(settings), EventBus(), memory, settings)
    try:
        perception.step()
    except Exception as e:  # the loop retries with backoff; the API answers 503 meanwhile
        log.warning("first poll failed: %s", e)
    perception.start_thread()
    recorder = HistoryRecorder(memory, History(settings.history_db), settings.store_interval_s)
    recorder.step()
    recorder.start_thread()
    uvicorn.run(create_app(memory, settings), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
