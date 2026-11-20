"""Scenario mode of the pitch videos: the real app on a simulated house, deterministic.

    python -m src.api --cenarios            # (or python -m src.cenarios) UI in http://127.0.0.1:8000/twin
    python -m src.api --cenarios --port 8010

No real house, no real MCP (MockMcpClient), no LLM (fixtures.json), its own database
(var/cenarios/sim.db) and a simulated clock at 19/11/2026. Director panel and shortcuts on /twin
(«D» shows/hides it). Without the flag the app is exactly as before.
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..config import ROOT, VAR_DIR
from .clock import SimClock, at

CENARIOS_DIR = VAR_DIR / "cenarios"
LAYOUT = ROOT / "config" / "layout_cenarios.json"
UI = Path(__file__).with_name("ui")
log = logging.getLogger(__name__)


def scenario_env(var_dir: Path = CENARIOS_DIR) -> dict[str, str]:
    """Settings of the scenario mode (they win over .env): the MCP path with writes, but into the mock."""
    return {
        "SIMULATE": "false", "SIM_ROOMS": "false", "DRY_RUN": "false",
        "HISTORY_DB": str(var_dir / "sim.db"), "LAYOUT_FILE": str(LAYOUT),
        "SIM_STATE_FILE": str(var_dir / "sim_home_unused.json"),
        "VISION_ENABLED": "false", "AGENTS": "true", "AGENT_COOLDOWN_S": "0", "POLL_INTERVAL_S": "1",
        "CHAT_LLM": "true", "CHAT_REASONING_EFFORT": "high", "INSIGHT_MIN_DAYS": "2",
        "TICK_INTERVAL_S": "90", "SETPOINT_C": "21",
        "TT_NO_REAL_WRITES": "1",  # belt and braces: a real McpClient could never write
    }


@dataclass
class Stage:
    settings: Any
    memory: Any
    bus: Any
    memories: Any
    coordinator: Any
    chat: Any
    insights: Any
    mcp: Any
    perception: Any
    rules: Any
    recorder: Any
    director: Any
    bridge: Any = None


def build_stage(settings, *, clock: SimClock | None = None, fixtures: dict | None = None, agents: bool = True,
                delay_scale: float = 1.0, think_s: float = 2.0, batch_s: float = 1.5) -> Stage:
    """Every component of the app, wired like src.api.main, with the mock MCP and the fixtures."""
    from ..context import Memories
    from ..chat.rules import RuleWatcher
    from ..memory import EventBus, SharedMemory
    from ..perception.sources import McpSource
    from ..storage.events import EventStore
    from ..storage.history import History, HistoryRecorder
    from ..twin.edit import LayoutEditor
    from .director import (Director, FixtureLLM, LockedPerception, ScenarioBridge, ScenarioChat,
                           ScenarioCoordinator, ScenarioInsights, load_fixtures)
    from .mock_mcp import MockMcpClient

    _forbid_real_mcp()
    fixtures = fixtures if fixtures is not None else load_fixtures()
    var_dir = Path(settings.history_db).parent
    mcp = MockMcpClient(var_dir / "mcp_home.json")
    if not mcp.path.exists():
        mcp.reset()
    memory, bus = SharedMemory(), EventBus()
    History(settings.history_db)
    recorder = HistoryRecorder(memory, History(settings.history_db), settings.store_interval_s)
    bus.subscribe(EventStore(settings.history_db, is_writer=lambda: not recorder.standby).on_event)
    memories = Memories.open(settings.history_db)
    coordinator = ScenarioCoordinator(memory, settings, mcp=mcp, memories=memories)
    llm = FixtureLLM(fixtures)
    chat = ScenarioChat(memory, settings, coordinator, memories, llm)
    coordinator.on_pending = chat.notify_pending
    insights = ScenarioInsights(memory, settings, coordinator, memories, llm, settings.insight_min_days,
                                settings.insight_max_per_day, settings.insight_every_s)
    insights.chat, chat.insights = chat, insights
    chat.layout = LayoutEditor(memory, settings, memories.prefs)
    rules = RuleWatcher(memories.prefs, memory, lambda text: chat.say(text, kind="rule"))
    bus.subscribe(rules.on_event)
    bus.subscribe(coordinator.on_event, types=list(ScenarioCoordinator.FIXED_RULE_EVENTS))  # emergency path: real code
    perception = LockedPerception(McpSource(mcp), bus, memory, settings)
    perception.step()
    director = Director(settings=settings, memory=memory, bus=bus, memories=memories, coordinator=coordinator,
                        chat=chat, insights=insights, mcp=mcp, perception=perception, rules=rules, clock=clock,
                        fixtures=fixtures, delay_scale=delay_scale, think_s=think_s)
    bridge = None
    if agents:
        bridge = ScenarioBridge(memory, settings, coordinator, bus, director, batch_s=batch_s).start()
        director.bridge = bridge
    return Stage(settings, memory, bus, memories, coordinator, chat, insights, mcp, perception, rules, recorder,
                 director, bridge)


def _forbid_real_mcp() -> None:
    """Any McpClient created from here on is refused: the scenario mode only talks to the mock."""
    import src.mcp_client as real

    class NoRealMcp:
        def __init__(self, *a, **k):
            raise RuntimeError("modo de cenários: o MCP real está desligado")

    real.McpClient = NoRealMcp


class DirectorAction(BaseModel):
    acao: Literal["cenario", "continuar", "abrir_porta", "fechar_porta", "camara", "tomada", "historico", "repor"]
    cenario: str | None = Field(None, max_length=2)


def add_director_routes(app, director) -> None:
    from fastapi import HTTPException
    from fastapi.staticfiles import StaticFiles

    app.mount("/cenarios/ui", StaticFiles(directory=UI), name="cenarios-ui")

    @app.get("/cenarios")
    def cenarios_view():
        return director.view()

    @app.post("/cenarios/acao")
    def cenarios_action(a: DirectorAction):
        try:
            if a.acao in ("cenario", "continuar"):
                return director.scenario(a.cenario or "", fresh=a.acao == "cenario")
            if a.acao in ("abrir_porta", "fechar_porta"):
                director.door(a.acao == "abrir_porta")
            elif a.acao == "camara":
                director.camera()
            elif a.acao == "tomada":
                director.plug_on()
            elif a.acao == "historico":
                director.load_humidity()
            elif a.acao == "repor":
                director.reset()
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return director.view()


def twin_snippet(fixtures: dict) -> str:
    import json
    examples = json.dumps(fixtures.get("sugestoes_rapidas") or [], ensure_ascii=False)
    return (f'<script>window.TT_CHAT_EXAMPLES = {examples};</script>\n'
            '<link rel="stylesheet" href="/cenarios/ui/realizador.css">\n'
            '<script type="module" src="/cenarios/ui/realizador.js"></script>\n')


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Modo de cenários do pitch (casa simulada, sem MCP real, sem LLM)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    a, _ = p.parse_known_args(argv)  # --sim / --agents of src.api are irrelevant here
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "a2a", "agent_framework"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    clock = SimClock().install()
    clock.set(at("16:30:00"))
    os.environ.update(scenario_env())
    from ..config import get_settings
    get_settings.cache_clear()
    settings = get_settings()

    import uvicorn

    from ..api import create_app
    stage = build_stage(settings, clock=clock)
    stage.director.reset()
    stage.perception.start_thread()
    stage.recorder.step()
    stage.recorder.start_thread()
    app = create_app(stage.memory, settings, stage.coordinator, chat=stage.chat,
                     twin_extra_html=twin_snippet(stage.director.fx))
    add_director_routes(app, stage.director)
    print(f"MODO DE CENÁRIOS · casa simulada (mock do MCP), sem LLM, base {settings.history_db}\n"
          f"3D: http://{a.host}:{a.port}/twin   ·   painel de realizador: tecla D", flush=True)
    try:
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning", timeout_graceful_shutdown=2)
    finally:
        if stage.bridge is not None:
            stage.bridge.stop()


if __name__ == "__main__":
    main()
