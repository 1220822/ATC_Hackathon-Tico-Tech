"""Coordinator side of A2A: sends perception events to the LLM agents and hands their proposals
to the Coordinator.

- route(): which agent cares about which event (pure; smoke is never routed: emergency path);
- one worker per agent in a background asyncio loop, so the perception loop never waits for an
  LLM: events that arrive close together are sent as one batch, the same (event type, room) is
  not re-sent to an agent before AGENT_COOLDOWN_S, and while an agent is thinking new events
  queue up for its next call;
- each call is a real A2A request (agent_framework.a2a.A2AAgent -> JSON-RPC) to the agent's
  server; a failed task or a reply without valid JSON is reported, never acted upon;
- the specialists' proposals go to the Coordinator agent (also over A2A): arbitrate() is the
  arbiter that src.coordinator.Coordinator calls (from a worker thread) to get its verdicts;
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
from .specs import AGENTS, ALL_AGENTS, AgentReply, BadReply, CoordinatorReply, parse_reply

log = logging.getLogger(__name__)

BATCH_S = 1.5  # wait this long after the first event to collect related ones
MAX_BATCH = 8
SECURITY_EVENTS = {"security_alarm", "tamper_alarm", "camera_motion", "camera_sound", "camera_analysis",
                   "hub_alarm", "alarm_state_changed", "security_mode_changed"}
ALARM_EVENTS = {"security_alarm", "hub_alarm", "alarm_state_changed"}
VISION_DEFERRED_EVENTS = frozenset({"camera_motion", "camera_sound"})


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


def _event_line(e: Event) -> str:
    base = (f"- [{time.strftime('%H:%M:%S', time.localtime(e.ts))}] {e.type}"
            f"{' (' + e.room_name + ')' if e.room_name else ''}: {e.message}")
    if e.type != "camera_analysis" or not isinstance(e.data, dict):
        return base
    d = e.data
    status = d.get("status", "ok")
    if status != "ok":
        return f"{base} [status={status}]"
    win = d.get("window_or_door_open")
    win_str = "sim" if win is True else ("não" if win is False else "n/d")
    smoke_str = "sim" if d.get("smoke_or_fire_visible") else "não"
    conf = d.get("overall_confidence")
    conf_str = f"{float(conf):.2f}" if isinstance(conf, (int, float)) else "n/d"
    note = str(d.get("scene_note_pt") or "").strip()
    note_part = f', nota="{note}"' if note else ""
    return (f"{base} [pessoas={d.get('person_count', 0)}, categoria={d.get('category', 'unclear')}, "
            f"janela_aberta={win_str}, fumo={smoke_str}, confiança={conf_str}{note_part}]")


def build_prompt(events: list[Event], state: HomeState, now: float | None = None,
                 memory_text: str | None = None) -> str:
    """The A2A message for an agent: the events, the house in short, what may be proposed and, if
    given, the two memories (src.context.render_context)."""
    now = time.time() if now is None else now
    ev_lines = [_event_line(e) for e in events]
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
            + (f"Memória (histórico, preferências com id, decisões semelhantes):\n{memory_text}\n\n" if memory_text else "")
            + "Analisa e responde só com o JSON pedido.")


def _decision_line(d) -> str:
    return f"- [{time.strftime('%H:%M:%S', time.localtime(d.ts))}] {d.agent} ({d.status}): {d.message}"


def build_coordinator_prompt(agent: str, reply: AgentReply, proposals: list, events: list[Event],
                             state: HomeState, recent: list, emergency_active: bool,
                             priorities: dict[str, int], now: float | None = None,
                             prefs_notes: list[tuple[int, str]] | None = None) -> str:
    """The A2A message for the Coordinator agent: what happened, the house, what was decided
    recently, and the numbered proposals it must approve or reject.
    proposals: [(Proposal, description)] in the order the verdicts refer to (n = 1, 2, ...)."""
    now = time.time() if now is None else now
    ev_lines = [_event_line(e) for e in events] or ["- (nenhum)"]
    summary = state.summary()
    house = {"seguranca": summary["seguranca"], "alarme_ativo": summary["alarme_ativo"],
             "divisoes": {name: {k: v for k, v in r.items() if k not in ("id", "dispositivos")}
                          for name, r in summary["divisoes"].items()}}
    order = " > ".join(name for name, _ in sorted(priorities.items(), key=lambda kv: kv[1]))
    props = [f"{i}. {agent}: {what} ({p.device_id} -> {p.acao}). Motivo: {p.motivo or '(sem motivo)'}"
             for i, (p, what) in enumerate(proposals, 1)]
    recent_lines = [_decision_line(d) for d in recent] or ["- (nenhuma)"]
    return (f"Hora atual: {time.strftime('%H:%M', time.localtime(now))}.\n"
            f"Prioridades (mais forte primeiro): {order}.\n"
            f"Emergência de fumo ativa: {'sim' if emergency_active else 'não'}.\n\n"
            "Eventos:\n" + "\n".join(ev_lines) + "\n\n"
            f"Estado da casa:\n{json.dumps(house, ensure_ascii=False)}\n\n"
            "Dispositivos acionáveis:\n" + "\n".join(_actionable(state) or ["- (nenhum)"]) + "\n\n"
            "Decisões recentes (últimos 5 min):\n" + "\n".join(recent_lines) + "\n\n"
            f"Análise do agente {agent}: {reply.resumo}\n\n"
            "Propostas a decidir:\n" + "\n".join(props) + "\n\n"
            + ("Preferências do utilizador (já verificadas em código):\n"
               + "\n".join(f"{n}. {text}" for n, text in prefs_notes) + "\n\n" if prefs_notes else "")
            + "Decide todas as propostas e responde só com o JSON pedido.")


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


def _is_transport_error(e: BaseException) -> bool:
    import httpx
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, (httpx.TransportError, ConnectionError)) or "Network communication error" in str(e):
            return True
        e = e.__cause__ or e.__context__
    return False


class AgentBridge:
    def __init__(self, memory: SharedMemory, settings: Settings, coordinator, bus: EventBus | None = None,
                 urls: dict[str, str] | None = None, client_factory: Callable | None = None,
                 coordinator_url: str | None = None, vision_worker=None,
                 vision_watchdog_s: float | None = None):
        """urls: specialist key -> A2A url; coordinator_url: the Coordinator agent (default port
        AGENTS_BASE_PORT). Once started, the bridge is the Coordinator's arbiter."""
        from .server import agent_url
        self.memory, self.s, self.coordinator = memory, settings, coordinator
        self.urls = urls or {k: agent_url(settings, k) for k in AGENTS}
        self.coordinator_url = coordinator_url or agent_url(settings, "coordenador")
        self._client_factory = client_factory or self._a2a_client
        self._vision_worker = vision_worker
        self._watchdog_s = (settings.vision_timeout_s + 5.0) if vision_watchdog_s is None else vision_watchdog_s
        self._loop = asyncio.new_event_loop()
        self._queues: dict[str, asyncio.Queue] = {}
        self._watchdogs: dict[str, asyncio.TimerHandle] = {}  # room_key -> TimerHandle (loop thread only)
        self._sent: dict[tuple[str, str, str | None], float] = {}
        self._thread: threading.Thread | None = None
        self._coord_client = None
        self.status = {k: {"key": k, "name": ALL_AGENTS[k].name, "url": u, "online": None, "busy": False,
                           "last_ts": None, "last_latency_s": None, "last_error": None}
                       for k, u in {"coordenador": self.coordinator_url, **self.urls}.items()}
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
        if self.coordinator is not None:
            self.coordinator.arbiter = self.arbitrate
        log.info("ponte A2A ativa: %s", ", ".join(f"{s['name']} {s['url']}" for s in self.status.values()))
        return self

    def stop(self) -> None:
        """Cancel the workers, stop the loop and wait for the thread (clean shutdown)."""
        if self._thread is None or self._loop.is_closed():
            return
        if self.coordinator is not None and getattr(self.coordinator, "arbiter", None) == self.arbitrate:
            self.coordinator.arbiter = None

        async def cancel_workers():
            for h in self._watchdogs.values():
                h.cancel()
            self._watchdogs.clear()
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

    # -- vision watchdog (loop thread only) -------------------------------------
    def _schedule_watchdog(self, room_key: str, e: Event) -> None:
        old = self._watchdogs.pop(room_key, None)
        if old is not None:
            old.cancel()
        self._watchdogs[room_key] = self._loop.call_later(
            self._watchdog_s, self._watchdog_fire, room_key, e
        )

    def _cancel_watchdog(self, room_key: str) -> None:
        old = self._watchdogs.pop(room_key, None)
        if old is not None:
            old.cancel()

    def _watchdog_fire(self, room_key: str, e: Event) -> None:
        self._watchdogs.pop(room_key, None)
        log.warning("watchdog de visão expirou em %s para %s; a reencaminhar evento original", room_key, e.type)
        if "seguranca" not in self._queues:
            return
        fallback = e.model_copy(update={"message": f"{e.message} (análise visual indisponível)"})
        if self._recently_sent("seguranca", fallback):
            return
        self._queues["seguranca"].put_nowait(fallback)

    # -- events from the perception loop (any thread) ---------------------------
    def on_event(self, e: Event) -> None:
        if self._loop.is_closed():  # stopped: the bus may still deliver events
            return
        room_key = e.room or "__home__"
        if e.type == "camera_analysis":
            self._loop.call_soon_threadsafe(self._cancel_watchdog, room_key)
        elif (
            e.type in VISION_DEFERRED_EVENTS
            and self._vision_worker is not None
            and self._vision_worker.is_alive()
            and self._vision_worker.will_handle(e, self.memory.state)
        ):
            self._loop.call_soon_threadsafe(self._schedule_watchdog, room_key, e)
            return
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
        url = self.coordinator_url if key == "coordenador" else self.urls[key]
        return A2AAgent(name=ALL_AGENTS[key].name, url=url, timeout=self.s.agent_timeout_s)

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
        """One specialist call; its proposals then go to the Coordinator (consider -> arbiter)."""
        try:
            reply = await self._call(key, client, build_prompt(batch, state, memory_text=self._memory_text(key, batch, state)),
                                     AgentReply)
        except BadReply as e:  # the agent answered, but not something we can act on
            self.memory.add_decision(Decision(agent=AGENTS[key].name, status="erro",
                                              message=f"Resposta inválida do agente: {str(e)[:160]}"))
            return None
        except Exception:  # down / timeout: already reported by _call
            return None
        await asyncio.to_thread(self.coordinator.consider, AGENTS[key].name, reply, batch)
        return reply

    def _memory_text(self, key: str, batch: list[Event], state: HomeState) -> str | None:
        """The two memories for this agent (data history + preferences), or None without a database."""
        mem = getattr(self.coordinator, "memories", None)
        if mem is None:
            return None
        try:
            from ..context import build_context, render_context
            return render_context(build_context(key, batch[-1] if batch else None, state, mem))
        except Exception as e:  # noqa: BLE001 - never block an agent on the context
            log.warning("context for %s failed: %s", key, e)
            return None

    # -- the Coordinator agent ---------------------------------------------------
    def arbitrate(self, prompt: str) -> CoordinatorReply:
        """Called by Coordinator.consider() from a worker thread: asks the Coordinator agent over
        A2A and returns its verdicts. Raises BadReply / transport errors (nothing is executed then)."""
        fut = asyncio.run_coroutine_threadsafe(self._ask_coordinator(prompt), self._loop)
        return fut.result(self.s.agent_timeout_s * 2 + 10)

    async def _ask_coordinator(self, prompt: str) -> CoordinatorReply:
        if self._coord_client is None:
            self._coord_client = self._client_factory("coordenador")
        return await self._call("coordenador", self._coord_client, prompt, CoordinatorReply)

    async def _call(self, key: str, client, prompt: str, model):
        """A2A call with status for the UI. Raises BadReply (answered, unusable) or the transport
        error (reported once as a 'Sistema' decision while the agent stays down)."""
        st = self.status[key]
        st["busy"] = True
        self._publish(key)
        t0 = time.time()
        try:
            response = await self._run_with_retry(client, prompt)
            err = _failed_task(response)
            if err:
                raise BadReply(err)
            try:
                parsed = parse_reply(response.text, model)
            except BadReply:
                log.warning("resposta inválida de %s: %r", key, (response.text or "")[:800])
                raise
            st.update(online=True, last_error=None)
            return parsed
        except BadReply as e:
            st.update(online=True, last_error=str(e)[:200])
            raise
        except Exception as e:  # server down, timeout, transport error
            was = st["online"]
            st.update(online=False, last_error=f"{type(e).__name__}: {e}"[:200])
            if was is not False:  # report the outage once
                self.memory.add_decision(Decision(
                    agent="Sistema", status="erro",
                    message=f"Agente {ALL_AGENTS[key].name} indisponível por A2A ({st['url']})."))
            log.warning("agente %s falhou: %s", key, e)
            raise
        finally:
            st.update(busy=False, last_ts=time.time(), last_latency_s=round(time.time() - t0, 1))
            self._publish(key)

    async def _run_with_retry(self, client, prompt: str):
        """One retry on transport errors (e.g. a stale keep-alive connection): the request never
        reached the agent, so sending it again does not duplicate work. Timeouts are not retried."""
        for attempt in (1, 2):
            try:
                return await asyncio.wait_for(client.run(prompt), self.s.agent_timeout_s)
            except asyncio.TimeoutError:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt == 2 or not _is_transport_error(e):
                    raise
                log.info("A2A: erro de transporte (%s), a repetir uma vez", e)
                await asyncio.sleep(0.3)

    def _publish(self, key: str) -> None:
        self.memory.set_agent_status(key, self.status[key])
