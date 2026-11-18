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
    GET /twin                                                 3D digital twin (Three.js, served locally)
    GET /twin/state?n=30                                      digital twin: layout + device values + events
    GET /twin/stream                                          the same, live (Server-Sent Events)
    POST /coordinator/request {device_id, action: on|off}     user request -> Coordinator (guard, dry-run)
    POST /chat {text}                                         chat with the Coordinator -> new messages
    GET /chat/messages?after=<id>                             chat history (polling)
    POST /chat/action {action, id, response, comment}         buttons: confirm/cancel, decision, suggestion
    GET /chat/pending                                         pending decisions, open question, suggestions
    GET /events, GET /decisions                               data memory (var/home.db)
    GET /agents/status                                        A2A agents as seen by the Coordinator
    GET /tools/consumo_por_hora?divisao=&horas=               read-only tools of the LLM agents
    GET /tools/tendencia_temperatura?divisao=&horas=&periodo_min=
Metrics: temperature, humidity, power_w, energy_today_kwh, energy_yesterday_kwh, open, on, battery
(old names power, contact, light still work). agg: avg | sum | min | max.
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .context import Memories
from .coordinator import Coordinator, RequestRefused, UserRequest
from .memory import SharedMemory
from .perception.state import HomeState, RoomState, _num
from .storage.decisions import DecisionStore
from .storage.events import EventStore
from .storage.history import History
from .storage.tools import HistoryTools
from .twin.layout import LayoutError
from .twin.state import twin_state, twin_stream
from .vision.capture import resolve_frame_path
from .writes import UnknownDevice


class VisionAnalyzeRequest(BaseModel):
    divisao: str | None = None
    contexto: Literal["intrusion", "smoke", "general"] = "general"


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ChatActionIn(BaseModel):
    action: str = Field(min_length=1, max_length=40)
    id: str | None = Field(None, max_length=64)
    response: str | None = Field(None, max_length=40)
    comment: str | None = Field(None, max_length=1000)

UI_DIR = Path(__file__).resolve().parent / "ui"
UI_FILE = UI_DIR / "index.html"
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


def create_app(memory: SharedMemory, settings: Settings, coordinator: Coordinator | None = None,
               vision_worker=None, chat=None) -> FastAPI:
    app = FastAPI(title="Tico&Tech: casa por divisões", version="1.0")
    coordinator = coordinator or Coordinator(memory, settings)
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

    app.mount("/static", StaticFiles(directory=UI_DIR / "static"), name="static")  # vendored Three.js
    app.mount("/twin/ui", StaticFiles(directory=UI_DIR / "twin"), name="twin-ui")
    app.mount("/chat/ui", StaticFiles(directory=UI_DIR / "chat"), name="chat-ui")

    @app.get("/twin", response_class=HTMLResponse, include_in_schema=False)
    def twin_page():
        return (UI_DIR / "twin" / "index.html").read_text(encoding="utf-8")

    @app.get("/twin/state")
    def get_twin_state(n: int = Query(30, ge=0, le=500)):
        try:
            st = twin_state(memory, settings, n=n)
        except LayoutError as e:
            raise HTTPException(500, str(e)) from None
        if st is None:
            raise HTTPException(503, "Estado da casa ainda não disponível.")
        return st

    @app.get("/twin/stream")
    async def get_twin_stream(request: Request):
        return StreamingResponse(twin_stream(memory, settings, disconnected=request.is_disconnected),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    tools = HistoryTools(settings.history_db, lambda: memory.state, settings.store_interval_s)

    @app.get("/tools/consumo_por_hora")
    def tool_consumo(divisao: str | None = None, horas: int = Query(24, ge=1, le=168)):
        out = tools.call("consumo_por_hora", {"divisao": divisao, "horas": horas})
        if "erro" in out:
            raise HTTPException(400, out["erro"])
        return out

    @app.get("/tools/tendencia_temperatura")
    def tool_tendencia(divisao: str, horas: float = Query(3, ge=0.25, le=72),
                       periodo_min: int = Query(15, ge=1, le=240)):
        out = tools.call("tendencia_temperatura", {"divisao": divisao, "horas": horas, "periodo_min": periodo_min})
        if "erro" in out:
            raise HTTPException(400, out["erro"])
        return out

    @app.get("/tools/ultima_analise_camara")
    def tool_ultima_analise_camara(divisao: str | None = None):
        obs = memory.latest_vision(divisao)
        if obs is None:
            return {"disponivel": False, "mensagem": "Ainda não existe análise visual."}
        return {"disponivel": True, **obs.model_dump()}

    @app.get("/vision/frames/{filename}")
    def vision_frame(filename: str):
        path = resolve_frame_path(settings.vision_frames_dir, filename)
        if path is None:
            raise HTTPException(404, "Frame não encontrado.")
        media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(path, media_type=media)

    @app.post("/vision/analyze")
    def vision_analyze(req: VisionAnalyzeRequest | None = None):
        """Manual on-demand visual analysis (runs in FastAPI threadpool)."""
        if vision_worker is None:
            raise HTTPException(503, "Módulo de visão indisponível.")
        body = req or VisionAnalyzeRequest()
        return vision_worker.analyze_now(room=body.divisao, context=body.contexto).model_dump()

    @app.get("/agents/status")
    def agents_status():
        return memory.agent_status()

    events_db = EventStore(settings.history_db)
    decisions_db = coordinator.decisions or DecisionStore(settings.history_db)

    @app.get("/events")
    def list_events(n: int = Query(50, ge=1, le=500), divisao: str | None = None, since: float | None = None):
        return events_db.recent(n, room=room_id(divisao) if divisao else None, since=since)

    @app.get("/decisions")
    def list_decisions(n: int = Query(50, ge=1, le=500), status: str | None = None):
        return decisions_db.recent(n, status=status)

    # -- chat with the Coordinator ---------------------------------------------------------
    if chat is None and coordinator.memories is not None:
        from .chat.service import ChatService
        chat = ChatService(memory, settings, coordinator, coordinator.memories)
    app.state.chat = chat

    def need_chat():
        if chat is None:
            raise HTTPException(503, "Chat indisponível (sem memória de preferências).")
        return chat

    @app.post("/chat")
    def chat_post(msg: ChatIn):
        """One user message -> the new messages (the user's and the Coordinator's replies)."""
        return {"messages": need_chat().handle(msg.text)}

    @app.get("/chat/messages")
    def chat_messages(after: int = Query(0, ge=0), n: int = Query(100, ge=1, le=500)):
        return need_chat().store.after(after, n)

    @app.post("/chat/action")
    def chat_action(a: ChatActionIn):
        return {"messages": need_chat().action(a.action, a.id, a.response, a.comment)}

    @app.get("/chat/pending")
    def chat_pending():
        c = need_chat()
        return {"decisions": decisions_db.pending(), "confirm": c.confirm,
                "suggestions": c.insights.pending() if c.insights else []}

    @app.post("/coordinator/request")
    def coordinator_request(req: UserRequest):
        """Simple actions from the UI. Never writes directly: the Coordinator guards it and,
        with DRY_RUN=true (default), only records the decision."""
        try:
            return coordinator.request(req).model_dump()
        except UnknownDevice as e:
            raise HTTPException(404, str(e)) from None
        except RequestRefused as e:
            raise HTTPException(503, str(e)) from None

    return app


def main() -> None:
    p = argparse.ArgumentParser(description="API e UI por divisão")
    p.add_argument("--sim", action="store_true", help="tudo simulado (SIMULATE=true)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--agents", action="store_true",
                   help="arranca também os agentes LLM (python -m src.agents) num processo à parte")
    a = p.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "a2a", "agent_framework"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if a.sim:
        os.environ["SIMULATE"] = "true"
    os.environ.setdefault("API_URL", f"http://{a.host}:{a.port}")  # for the agents' read-only tools

    import uvicorn

    from .memory import EventBus
    from .perception.poller import Perception
    from .perception.sources import make_source
    from .storage.history import HistoryRecorder, import_legacy

    settings = get_settings()
    import_legacy(History(settings.history_db), settings.legacy_history_db)
    memory, bus = SharedMemory(), EventBus()
    recorder = HistoryRecorder(memory, History(settings.history_db), settings.store_interval_s)
    # every bus event goes to the events table (only while this process holds the writer lease)
    bus.subscribe(EventStore(settings.history_db, is_writer=lambda: not recorder.standby).on_event)
    memories = Memories.open(settings.history_db)  # data history + preferences (var/home.db)
    coordinator = Coordinator(memory, settings, memories=memories)
    from .chat.rules import RuleWatcher
    from .chat.service import ChatService
    from .llm import LLM
    chat_llm = LLM.from_settings(settings) if settings.chat_llm else None
    if chat_llm is not None:
        chat_llm.warmup_async()
    chat = ChatService(memory, settings, coordinator, memories, chat_llm)
    coordinator.on_pending = chat.notify_pending  # pending decisions show up in the chat
    bus.subscribe(RuleWatcher(memories.prefs, memory, lambda text: chat.say(text, kind="rule")).on_event)
    # fixed rules, straight from the bus, never through an LLM: smoke and intrusion
    bus.subscribe(coordinator.on_event, types=list(Coordinator.FIXED_RULE_EVENTS))
    vision_worker = None
    if settings.vision_enabled:
        from .vision.worker import VISION_TRIGGER_EVENTS, VisionWorker
        vision_worker = VisionWorker(memory, bus, settings).start()
        vision_worker.warmup_async()
        bus.subscribe(vision_worker.on_event, types=list(VISION_TRIGGER_EVENTS))
    bridge = None
    agents_proc = None
    if settings.agents_enabled:
        from .agents.bridge import AgentBridge
        bridge = AgentBridge(memory, settings, coordinator, bus, vision_worker=vision_worker).start()
        if a.agents:
            import subprocess

            from .agents.__main__ import port_in_use
            from .agents.specs import ALL_AGENTS
            ports = [settings.agents_base_port + s.port_offset for s in ALL_AGENTS.values()]
            if all(port_in_use(settings.agents_host, port) for port in ports):
                log.info("já há agentes a escutar nas portas %s; uso esses", ports)
            else:  # the child exits on its own if this process dies (--parent-pid)
                agents_proc = subprocess.Popen([sys.executable, "-m", "src.agents", "--parent-pid", str(os.getpid())],
                                               env=dict(os.environ))
    perception = Perception(make_source(settings), bus, memory, settings)
    try:
        perception.step()
    except Exception as e:  # the loop retries with backoff; the API answers 503 meanwhile
        log.warning("first poll failed: %s", e)
    perception.start_thread()
    recorder.step()
    recorder.start_thread()
    print(f"UI: http://{a.host}:{a.port}/   ·   3D: http://{a.host}:{a.port}/twin   ·   "
          f"agentes: {'A2A ' + settings.llm_model if settings.agents_enabled else 'desligados'}"
          f"   ·   visão: {'ativa' if vision_worker is not None else 'desligada'}"
          f"   ·   {'DRY-RUN' if settings.dry_run else 'ESCRITAS REAIS'}", flush=True)
    try:
        # open SSE streams would otherwise keep Ctrl+C waiting
        uvicorn.run(create_app(memory, settings, coordinator, vision_worker=vision_worker, chat=chat),
                    host=a.host, port=a.port, log_level="warning", timeout_graceful_shutdown=2)
    finally:
        if vision_worker is not None:
            vision_worker.stop()
        if bridge is not None:
            bridge.stop()
        if agents_proc is not None:
            agents_proc.terminate()


if __name__ == "__main__":
    main()
