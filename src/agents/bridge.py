"""Coordinator side of A2A: sends perception events to the LLM agents and hands their proposals
to the Coordinator.

- route(): which agent cares about which event (pure; smoke is never routed: emergency path);
- one worker per agent in a background asyncio loop, so the perception loop never waits for an
  LLM: events that arrive close together are sent as one batch, the same (event type, room) is
  not re-sent to an agent before AGENT_COOLDOWN_S, and while an agent is thinking new events
  queue up for its next call;
- each call is a real A2A request (agent_framework.a2a.A2AAgent -> JSON-RPC) to the agent's
  server; a failed task or a reply without valid JSON is reported, never acted upon;
- the status of each agent (online, busy, last latency/error) goes to SharedMemory for the UI.
"""

import asyncio
import json
import logging
import threading
import time
from typing import Callable

from ..config import Settings
from ..memory import Decision, EventBus, SharedMemory
from ..perception.state import Event, HomeState, _num, de
from .specs import AGENTS, AgentReply, BadReply, parse_reply

log = logging.getLogger(__name__)

BATCH_S = 1.5  # wait this long after the first event to collect related ones
MAX_BATCH = 8
SECURITY_EVENTS = {"security_alarm", "tamper_alarm", "camera_motion", "camera_sound", "hub_alarm",
                   "alarm_state_changed", "security_mode_changed"}
ALARM_EVENTS = {"security_alarm", "hub_alarm", "alarm_state_changed"}


def route(e: Event, state: HomeState | None, emergency_active: bool = False) -> list[str]:
    """Agent keys interested in an event. Smoke and alarms caused by it stay on the emergency path."""
    t = e.type
    if t in ("smoke_detected", "tick", "device_online", "device_offline", "light_changed", "alarm_cancelled"):
        return []
    armed = bool(state and state.armed)
    out: list[str] = []
    if t in SECURITY_EVENTS and not (emergency_active and t in ALARM_EVENTS):
        out.append("seguranca")
    if t == "contact_opened" and armed:
        out.append("seguranca")
    if t == "temperature_dropping":
        out += ["energia", "conforto"]
    if t in ("plug_switched", "plug_power_changed"):
        out.append("energia")
    if t == "contact_opened" and state is not None and state.any_plug_on:
        out.append("energia")
    if t == "temperature_changed" and state is not None:
        room = state.room(e.room) if e.room else None
        after = _num(e.data.get("after"))
        if room and room.setpoint_c is not None and after is not None and abs(after - room.setpoint_c) >= 1.0:
            out.append("conforto")
    if t == "humidity_changed":
        after = _num(e.data.get("after"))
        if after is not None and (after >= 70 or after <= 30):
            out.append("conforto")
    return list(dict.fromkeys(out))


def _actionable(state: HomeState) -> list[str]:
    lines = []
    for d in state.devices.values():
        if d.role not in ("light", "plug") or not d.online:
            continue
        on = d.caps.get("power") == "on"
        extra = ""
        if d.role == "plug":
            w = _num(d.caps.get("instantPower"))
            extra = f", {w:.0f} W" if w is not None else ""
        kind = "Luz" if d.role == "light" else "Tomada (aquecedor)"
        lines.append(f"- {d.id}: {kind} {de(d.room_name)}, {'ligada' if on else 'desligada'}{extra}")
    return lines


def build_prompt(events: list[Event], state: HomeState, now: float | None = None) -> str:
    """The A2A message for an agent: the events, the house in short, and what may be proposed."""
    now = time.time() if now is None else now
    ev_lines = [f"- [{time.strftime('%H:%M:%S', time.localtime(e.ts))}] {e.type}"
                f"{' (' + e.room_name + ')' if e.room_name else ''}: {e.message}" for e in events]
    summary = state.summary()
    house = {"seguranca": summary["seguranca"], "alarme_ativo": summary["alarme_ativo"],
             "divisoes": {name: {k: v for k, v in r.items() if k not in ("id", "dispositivos")}
                          for name, r in summary["divisoes"].items()}}
    actionable = _actionable(state) or ["- (nenhum)"]
    return (f"Hora atual: {time.strftime('%H:%M', time.localtime(now))}. Não assumas nada sobre luz natural "
            f"ou presença de pessoas além do que os dados dizem.\n\n"
            f"Eventos:\n" + "\n".join(ev_lines) + "\n\n"
            f"Estado da casa:\n{json.dumps(house, ensure_ascii=False)}\n\n"
            f"Dispositivos acionáveis:\n" + "\n".join(actionable) + "\n\n"
            "Analisa e responde só com o JSON pedido.")


def _failed_task(response) -> str | None:
    """Error text if the A2A task ended in TASK_STATE_FAILED (the client returns the history then)."""
    from a2a.types import TaskState
    raws = response.raw_representation if isinstance(response.raw_representation, list) else [response.raw_representation]
    for raw in raws:
        for item in (raw if isinstance(raw, list) else [raw]):
            status = getattr(item, "status", None)
            if status is not None and getattr(status, "state", None) == TaskState.TASK_STATE_FAILED:
                msg = getattr(status, "message", None)
                parts = getattr(msg, "parts", None) or []
                return " ".join(getattr(p, "text", "") for p in parts) or "tarefa A2A falhou"
    if not any(m.role == "assistant" and (m.text or "").strip() for m in response.messages):
        return "o agente não respondeu"
    return None


class AgentBridge:
    def __init__(self, memory: SharedMemory, settings: Settings, coordinator, bus: EventBus | None = None,
                 urls: dict[str, str] | None = None, client_factory: Callable | None = None):
        from .server import agent_url
        self.memory, self.s, self.coordinator = memory, settings, coordinator
        self.urls = urls or {k: agent_url(settings, k) for k in AGENTS}
        self._client_factory = client_factory or self._a2a_client
        self._loop = asyncio.new_event_loop()
        self._queues: dict[str, asyncio.Queue] = {}
        self._sent: dict[tuple[str, str, str | None], float] = {}
        self._thread: threading.Thread | None = None
        self.status = {k: {"key": k, "name": AGENTS[k].name, "url": u, "online": None, "busy": False,
                           "last_ts": None, "last_latency_s": None, "last_error": None}
                       for k, u in self.urls.items()}
        for k in self.status:
            self._publish(k)
        if bus is not None:
            bus.subscribe(self.on_event)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "AgentBridge":
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self._loop)
            for k in self.urls:
                self._queues[k] = asyncio.Queue()
                self._loop.create_task(self._worker(k))
            self._loop.call_soon(ready.set)
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="a2a-bridge", daemon=True)
        self._thread.start()
        ready.wait(5)
        log.info("ponte A2A ativa: %s", ", ".join(f"{AGENTS[k].name} {u}" for k, u in self.urls.items()))
        return self

    def stop(self) -> None:
        """Cancel the workers, stop the loop and wait for the thread (clean shutdown)."""
        if self._thread is None or self._loop.is_closed():
            return

        async def cancel_workers():
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(cancel_workers(), self._loop).result(5)
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()

    # -- events from the perception loop (any thread) ---------------------------
    def on_event(self, e: Event) -> None:
        for key in route(e, self.memory.state, getattr(self.coordinator, "emergency_active", False)):
            if key not in self._queues:
                continue
            if self._recently_sent(key, e):
                continue
            self._loop.call_soon_threadsafe(self._queues[key].put_nowait, e)

    def _recently_sent(self, key: str, e: Event) -> bool:
        k = (key, e.type, e.room)
        now = time.time()
        if now - self._sent.get(k, 0) < self.s.agent_cooldown_s and e.severity == "info":
            return True
        self._sent[k] = now
        return False

    # -- workers (bridge loop) ----------------------------------------------------
    def _a2a_client(self, key: str):
        from agent_framework.a2a import A2AAgent
        return A2AAgent(name=AGENTS[key].name, url=self.urls[key], timeout=self.s.agent_timeout_s)

    async def _worker(self, key: str) -> None:
        q = self._queues[key]
        client = None
        while True:
            batch = [await q.get()]
            await asyncio.sleep(BATCH_S)
            while not q.empty() and len(batch) < MAX_BATCH:
                batch.append(q.get_nowait())
            state = self.memory.state
            if state is None:
                continue
            if client is None:
                client = self._client_factory(key)
            await self.ask(key, client, batch, state)

    async def ask(self, key: str, client, batch: list[Event], state: HomeState) -> AgentReply | None:
        st = self.status[key]
        st["busy"] = True
        self._publish(key)
        t0 = time.time()
        reply = None
        try:
            response = await asyncio.wait_for(client.run(build_prompt(batch, state)), self.s.agent_timeout_s)
            err = _failed_task(response)
            if err:
                raise BadReply(err)
            reply = parse_reply(response.text)
            st.update(online=True, last_error=None)
        except BadReply as e:  # the agent answered, but not something we can act on
            st.update(online=True, last_error=str(e)[:200])
            self.memory.add_decision(Decision(agent=AGENTS[key].name, status="erro",
                                              message=f"Resposta inválida do agente: {str(e)[:160]}"))
        except Exception as e:  # server down, timeout, transport error
            was = st["online"]
            st.update(online=False, last_error=f"{type(e).__name__}: {e}"[:200])
            if was is not False:  # report the outage once
                self.memory.add_decision(Decision(
                    agent="Sistema", status="erro",
                    message=f"Agente {AGENTS[key].name} indisponível por A2A ({self.urls[key]})."))
            log.warning("agente %s falhou: %s", key, e)
        finally:
            st.update(busy=False, last_ts=time.time(), last_latency_s=round(time.time() - t0, 1))
            self._publish(key)
        if reply is not None:
            await asyncio.to_thread(self.coordinator.consider, AGENTS[key].name, reply, batch)
        return reply

    def _publish(self, key: str) -> None:
        self.memory.set_agent_status(key, self.status[key])
