"""The director of the scenario mode: puts the house in the initial state of each pitch scenario, gives
scripted items the time of the recording, and replaces only the text generation of the LLMs.

The pipeline is the real one: perception -> bus -> agents (A2A bridge) -> Coordinator (guardrails,
preferences, autonomy) -> chat / timeline / 3D. What changes:
  - the house is MockMcpClient (src.cenarios.mock_mcp), never the real MCP;
  - the agents' and the Coordinator's replies come from fixtures.json (ScenarioBridge), as do the chat's
    LLM readings and the suggestion phrase (FixtureLLM);
  - the clock is simulated (src.cenarios.clock) and jumps to the recording's time when a scripted text
    shows up ("marcas");
  - the Coordinator "thinks" for a moment before each chat answer (ScenarioChat).
The emergency path (fixed rules: intrusion, smoke) is not simulated: the director injects the event
and the real code acts.
"""

import asyncio
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from ..agents.bridge import MAX_BATCH, AgentBridge
from ..agents.specs import AGENTS, AgentReply, BadReply, CoordinatorReply
from ..chat.service import ChatService
from ..coordinator import Coordinator
from ..insights.service import InsightService
from ..perception.poller import Perception
from ..perception.state import TemperatureTrend
from .clock import at

log = logging.getLogger(__name__)

FIXTURES = Path(__file__).with_name("fixtures.json")
SCENARIOS = ("1", "2", "3", "4", "5", "6")
RULE_TEXT = "Se abrir a janela com o aquecedor ligado, avisa-me."


def load_fixtures(path: Path = FIXTURES) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _key(text: str) -> str:
    from ..chat.intents import norm
    return norm(text).strip(" .!?…")


# ---------------------------------------------------------------------------- text generation


class FixtureLLM:
    """Stands in for src.llm.LLM: answers from fixtures.json, or raises LLMError (then the code falls back
    to what it does without an LLM: the PT-PT rules for the chat, the template for a suggestion)."""

    def __init__(self, fixtures: dict):
        self.fx = fixtures

    def warmup_async(self) -> None:
        pass

    def complete(self, *args, **kwargs) -> str:
        from ..llm import LLMError
        raise LLMError("modo de cenários: sem LLM")

    def structured(self, system: str, user: str, model, retries: int = 1, reasoning_effort: str | None = None):
        from ..chat.intents import ChatPlan
        from ..insights.service import Phrase
        from ..llm import LLMError
        if model is ChatPlan:
            found = self.fx.get("chat", {}).get(_key(user.rsplit("Mensagem:", 1)[-1]))
            if found is not None:
                return ChatPlan.model_validate(found)
        elif model is Phrase:
            for f in self.fx.get("frases", []):
                if f["modelo_contem"] in user:
                    return Phrase(frase=f["frase"])
        raise LLMError("modo de cenários: sem resposta fixa para este pedido")


class ScenarioBridge(AgentBridge):
    """The A2A bridge with the same routing, batching and hand-over to the Coordinator, but each agent's
    reply comes from the active scenario's fixtures (no fixture: the agent stays silent). The chips only
    show «em espera» / «a pensar»."""

    def __init__(self, memory, settings, coordinator, bus, director: "Director", batch_s: float = 1.5):
        self.director = director
        self.batch_s = batch_s
        super().__init__(memory, settings, coordinator, bus,
                         urls={k: f"fixture://{k}" for k in AGENTS}, coordinator_url="fixture://coordenador",
                         client_factory=lambda key: None)

    async def _worker(self, key: str) -> None:
        q = self._queues[key]
        while True:
            batch = [await q.get()]
            await asyncio.sleep(self.batch_s)
            while not q.empty() and len(batch) < MAX_BATCH:
                batch.append(q.get_nowait())
            state = self.memory.state
            if state is None:
                continue
            try:
                await self.ask(key, None, batch, state)
            except Exception:  # noqa: BLE001 - one bad step must not stop the agent
                log.exception("cenários: agente %s falhou", key)

    async def ask(self, key, client, batch, state):
        fx = self.director.agent_fixture(key, batch)
        if fx is None:
            return None
        self.set_busy(key, True)
        try:
            await asyncio.sleep(fx.get("atraso_s", 2.0) * self.director.delay_scale)
        finally:
            self.set_busy(key, False)
        reply = AgentReply.model_validate(fx["resposta"])
        await asyncio.to_thread(self.coordinator.consider, AGENTS[key].name, reply, batch)
        return reply

    def arbitrate(self, prompt: str) -> CoordinatorReply:
        fx = self.director.coordinator_fixture()
        if fx is None:
            raise BadReply("modo de cenários: sem veredicto fixo para esta proposta")
        self.set_busy("coordenador", True)
        try:
            time.sleep(fx.get("atraso_s", 2.0) * self.director.delay_scale)
        finally:
            self.set_busy("coordenador", False)
        return CoordinatorReply.model_validate(fx["resposta"])

    def set_busy(self, key: str, busy: bool) -> None:
        st = self.status[key]
        st["busy"] = busy
        if not busy:
            st["last_ts"] = time.time()
        self._publish(key)

    def reset_status(self) -> None:
        for key, st in self.status.items():
            st.update(online=None, busy=False, last_ts=None, last_latency_s=None, last_error=None)
            self._publish(key)

    def _publish(self, key: str) -> None:
        super()._publish(key)
        self.memory.touch()  # the chips refresh at once, not at the next poll


class ScenarioChat(ChatService):
    """The Coordinator's chat; before each answer it «thinks» for a moment (chip «a pensar», and the chat
    shows «O Coordenador está a pensar…» while the request is open)."""

    director: "Director | None" = None

    def handle(self, text: str) -> list[dict]:
        if self.director is not None and (text or "").strip():
            self.director.think()
        return super().handle(text)

    def action(self, action: str, id: str | None = None, response: str | None = None,
               comment: str | None = None) -> list[dict]:
        if self.director is not None:
            self.director.think()
        return super().action(action, id, response, comment)


class ScenarioInsights(InsightService):
    """Suggestions as usual (patterns detected in code over the history); only the legend of «Porquê?»
    uses the recording's wording."""

    legends: dict[str, str] = {}

    def evidence(self, id: str) -> dict | None:
        ev = super().evidence(id)
        if ev is not None:
            ins = ev["insight"]
            legend = self.legends.get(f"{ins['type']}:{ins['evidence'].get('metric', '')}")
            if legend:
                ev["legenda"] = legend
        return ev


class ScenarioCoordinator(Coordinator):
    """The real Coordinator. Only the read-back of lights is off: it exists for the real bulb, which sometimes
    ignores a write; the mock applies every write exactly, and a late check could undo a newer write."""

    def _check_lights_later(self) -> None:
        self._verify = []


class LockedPerception(Perception):
    """Perception whose steps the director can run (and hold) itself."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()

    def step(self, now: float | None = None):
        with self.lock:
            return super().step(now)

    def resync(self) -> None:
        """Read the house again without emitting events (the director changed it behind the scenes)."""
        with self.lock:
            self.prev = None
            self.trend = TemperatureTrend(self.s)
            super().step()


# ---------------------------------------------------------------------------- the director


class Director:
    def __init__(self, *, settings, memory, bus, memories, coordinator, chat, insights, mcp, perception,
                 rules, clock=None, fixtures: dict | None = None, delay_scale: float = 1.0, think_s: float = 2.0):
        self.s, self.memory, self.bus, self.mem = settings, memory, bus, memories
        self.coordinator, self.chat, self.insights, self.mcp = coordinator, chat, insights, mcp
        self.perception, self.rules, self.clock = perception, rules, clock
        self.fx = fixtures if fixtures is not None else load_fixtures()
        self.delay_scale, self.think_s = delay_scale, think_s
        self.bridge: ScenarioBridge | None = None
        self.active: str | None = None
        self._pins: list[tuple[str, str]] = []
        self._agent_fx: list[dict] = []
        self._coord_fx: list[dict] = []
        self._pin_lock = threading.Lock()
        self._action_lock = threading.RLock()
        memory.on_record = self._on_record
        memories.prefs.id_factory = self._pref_id
        chat.director = self
        insights.legends = self.fx.get("legendas", {})
        store_add = chat.store.add

        def add(role: str, text: str, **meta):  # chat messages of the recording get its time too
            self._pin(text)
            return store_add(role, text, **meta)

        chat.store.add = add

    # -- time ------------------------------------------------------------------------------
    def set_time(self, hms: str) -> None:
        if self.clock is not None:
            self.clock.set(at(hms))

    def _pin(self, text: str) -> float | None:
        with self._pin_lock:
            for i, (t, hms) in enumerate(self._pins):
                if t == text:
                    del self._pins[i]
                    ts = at(hms)
                    if self.clock is not None:
                        self.clock.set(ts)
                    return ts
        return None

    def _on_record(self, item) -> None:
        ts = self._pin(getattr(item, "message", ""))
        if ts is not None:
            item.ts = ts

    def think(self) -> None:
        s = self.think_s * self.delay_scale
        if s <= 0:
            return
        if self.bridge is not None:
            self.bridge.set_busy("coordenador", True)
        try:
            time.sleep(s)
        finally:
            if self.bridge is not None:
                self.bridge.set_busy("coordenador", False)

    # -- fixtures --------------------------------------------------------------------------
    def agent_fixture(self, key: str, batch: list) -> dict | None:
        with self._pin_lock:
            for i, fx in enumerate(self._agent_fx):
                if fx["agente"] != key:
                    continue
                for e in batch:
                    if e.type != fx["evento"]:
                        continue
                    if "mensagem" in fx and e.message != fx["mensagem"]:
                        continue
                    if "contem" in fx and fx["contem"] not in e.message:
                        continue
                    return self._agent_fx.pop(i)
        return None

    def coordinator_fixture(self) -> dict | None:
        with self._pin_lock:
            return self._coord_fx.pop(0) if self._coord_fx else None

    def _pref_id(self, scope: str, key: str, value: dict) -> str | None:
        """The «Luz: nunca» preference has the id seen in the recordings (the agents cite it)."""
        pid = self.fx.get("preferencia_luz_id")
        if pid and key == "autonomy" and value.get("category") == "luz" and value.get("level") == "nunca" \
                and not value.get("kind") and self.mem.prefs.get(pid) is None:
            return pid
        return None

    # -- what the panel shows --------------------------------------------------------------
    def view(self) -> dict:
        scen = self.fx["cenarios"]
        return {"ativo": self.active, "hora": time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(time.time())),
                "cenarios": [{"id": k, "titulo": scen[k]["titulo"], "slide": scen[k]["slide"],
                              "inicio": scen[k]["inicio"][:5]} for k in SCENARIOS],
                "marcas_por_usar": len(self._pins)}

    # -- scenarios -------------------------------------------------------------------------
    def scenario(self, n: str, fresh: bool = True) -> dict:
        """fresh: «Repor tudo» + the preconditions of scenario n (what the earlier scenarios left);
        otherwise only the clock and the scenario's own lead-in, on top of the current state (1 -> 6)."""
        if n not in SCENARIOS:
            raise ValueError(f"cenário desconhecido: {n}")
        with self._action_lock:
            if fresh:
                self.reset()
                self._preconditions(n)
            self._enter(n)
        return self.view()

    def _enter(self, n: str) -> None:
        sc = self.fx["cenarios"][n]
        with self._pin_lock:
            self.active = n
            self._pins = [tuple(p) for p in sc.get("marcas", [])]
            self._agent_fx = [dict(a) for a in sc.get("agentes", [])]
            self._coord_fx = [dict(c) for c in sc.get("coordenador", [])]
        self.set_time(sc["inicio"])
        if n == "3":
            # lead-in: the heater was just switched on; the real pipeline does the rest (Energia proposes,
            # the Coordinator asks the user)
            with self.perception.lock:
                self.mcp.set_contact(False)
                self.mcp.set_plug(False)
                self.perception.resync()
                self.mcp.set_plug(True)
                self.perception.step()
        elif n == "4":
            with self.perception.lock:
                self.mcp.set_contact(False)
                self.mcp.set_plug(False)
                self.mcp.set_security("DISARMED", alarmed=False)
                self.mcp.light_normal()
                self.perception.resync()
            self.memory.silence_alarm(None)
        elif n == "5":
            self.load_humidity()

    def _preconditions(self, n: str) -> None:
        if n >= "2":
            self._add_rule(at("16:30:40"))
        if n >= "3":
            self.mem.prefs.set("global", "autonomy", {"category": "luz", "level": "nunca"}, "explicit",
                               "Nunca acendas as luzes sozinha.", now=at("16:38:21"))
        if n == "6":
            self._add_muted_suggestion(at("17:06:12"))
            self._add_answered_decisions()

    def _add_rule(self, ts: float) -> None:
        from ..chat.intents import extract_rule
        from ..chat.rules import build_rule, scenario_payload
        state = self.memory.state
        rule, _ = build_rule(extract_rule(RULE_TEXT, [r.name for r in state.rooms.values()]), state, set())
        res = self.mcp.call("post_new_scenario", **scenario_payload(rule))
        self.mem.prefs.set("global", "rule", rule, "explicit", RULE_TEXT, mcp_scenario_id=res["data"]["id"], now=ts)

    def _add_muted_suggestion(self, ts: float) -> None:
        quarto = self._room("Quarto")
        self.mem.prefs.set("global", "suggestion_muted",
                           {"insight_key": f"daily_pattern:{quarto}:humidity_rise:noite",
                            "description": "criar a regra «se a humidade no Quarto passar de 70 %, avisa-me», para "
                                           "arejares a divisão a tempo"},
                           "explicit", "Não voltes a sugerir isto", now=ts)

    def _add_answered_decisions(self) -> None:
        """Scenario 3's two answers (rejected, then approved), for «Respondeste a 2 decisões»."""
        plug = self.memory.state.by_role("plug")
        for when, response, status in (("16:44:51", "rejected", "rejected"), ("16:46:34", "approved", "executed")):
            rec = self.mem.decisions.add(
                trigger={"type": "plug_switched"}, proposals=[], decision={"device_id": plug.id, "type": "power", "value": "off"},
                explanation="Proposta da Energia: desligar a tomada do Quarto.", status="pending", autonomy="perguntar",
                kind=f"plug_switched:{plug.id}:power=off", category="energia", agent="Energia", room=plug.room_id,
                room_name=plug.room_name, device_id=plug.id, ts=at(when))
            self.mem.decisions.respond(rec["id"], response, now=at(when) + 18)
            self.mem.decisions.set_status(rec["id"], status)

    def _room(self, name: str) -> str:
        return next(r.id for r in self.memory.state.rooms.values() if r.name == name)

    # -- buttons -----------------------------------------------------------------------------
    def door(self, open_: bool) -> None:
        with self._action_lock, self.perception.lock:
            self.mcp.set_contact(open_)
            self.perception.step()

    def plug_on(self) -> None:
        with self._action_lock, self.perception.lock:
            self.mcp.set_plug(True)
            self.perception.step()

    def camera(self, people: int = 2, confidence: float = 0.98) -> None:
        """What the vision model saw in the recording: 2 people in the Sala (98 %). It enters as the vision
        worker's camera_analysis event; the fixed intrusion rule then acts on its own."""
        from ..vision.models import VisionObservation, to_event
        with self._action_lock:
            state = self.memory.state
            cam = state.by_role("camera")
            obs = VisionObservation(room_id=cam.room_id, room_name=cam.room_name, camera_id=cam.id,
                                    trigger_type="camera_motion", context="intrusion" if state.armed else "general",
                                    has_person=True, person_count=people, category="person",
                                    overall_confidence=confidence, scene_note_pt="Duas pessoas junto à porta da Sala.",
                                    model="fixture")
            self.memory.set_vision(obs)
            ev = to_event(obs, state.armed)
            self.memory.add_events([ev])
            self.bus.publish([ev])

    def load_humidity(self) -> int:
        """3 nights of humidity in the Quarto (22:00–07:00): +10, +10, +11 points over a ~56,5 % base.
        The suggestion comes from the real detector over these readings."""
        from ..storage.history import History
        h = self.fx["humidade"]
        state = self.memory.state
        sensor = state.by_role("temperature", self._room("Quarto"))
        base, peak, temp = h["base_pct"], h["pico_extra_pct"], h["temperatura_c"]
        rows = []
        spans = []
        for day, rise in h["noites"]:
            y, m, d = (int(x) for x in day.split("-"))
            start = time.mktime((y, m, d, 22, 0, 0, 0, 0, -1))
            end = start + 9 * 3600
            spans.append((start, end))
            for t in range(int(start), int(end), 60):
                k = (t - start) / 3600  # hours since 22:00
                if k < 0.5:
                    v = base
                elif k < 6:
                    v = base + (rise + peak) * (k - 0.5) / 5.5
                elif k < 8.5:
                    v = base + rise + peak - peak * (k - 6) / 2.5
                else:
                    v = base + rise
                rows += [(t, sensor.room_id, sensor.id, "humidity", round(v, 1)),
                         (t, sensor.room_id, sensor.id, "temperature", temp)]
        with closing(sqlite3.connect(self.s.history_db)) as db, db:
            for a, b in spans:  # idempotent: the button can be pressed twice
                db.execute("DELETE FROM readings WHERE room = ? AND ts >= ? AND ts < ? AND metric IN "
                           "('humidity', 'temperature')", (sensor.room_id, a, b))
        return History(self.s.history_db).insert(rows)

    # -- «Repor tudo» ------------------------------------------------------------------------
    def reset(self) -> None:
        """Back to the common initial state. Clears only the scenario mode's own files
        (var/cenarios/: sim.db, the mock house and its scenarios, the 3D layout edits)."""
        from ..twin.layout import user_layout_file
        with self._action_lock:
            with self._pin_lock:
                self.active = None
                self._pins, self._agent_fx, self._coord_fx = [], [], []
            c = self.coordinator
            c.emergency_active = False
            c._light_before.clear()
            c._approved.clear()
            c._props.clear()
            c._disarmed_at = 0.0
            c._verify = []
            self.chat.confirm, self.chat.queue = None, []
            self.chat.store.clear()
            self.mem.prefs.erase_all()
            self.mem.decisions.clear()
            self.insights.store.clear()
            self.mem.events.execute("DELETE FROM events")
            with closing(sqlite3.connect(self.s.history_db)) as db, db:
                db.execute("DELETE FROM readings")
            user_layout_file(self.s).unlink(missing_ok=True)
            self.rules._last.clear()
            self.set_time("16:30:00")
            with self.perception.lock:
                self.mcp.reset()
                self.memory.clear_history()
                self.perception.resync()
            if self.bridge is not None:
                self.bridge.reset_status()


