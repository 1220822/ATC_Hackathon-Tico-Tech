"""Coordinator: the only component that writes to the house, through execute().

Three inputs, one write path (guard -> execute -> route_write, dry-run by default), and every
outcome recorded as a Decision in SharedMemory (UI timeline / 3D balloons):

  1. user requests from the UI (POST /coordinator/request): power on/off of a light or plug;
  2. proposals of the LLM agents (Segurança, Energia, Conforto) received over A2A by
     src.agents.bridge -> consider(): hard guardrails, then the Coordinator agent (LLM, over A2A,
     self.arbiter) approves or rejects each one, then the guardrails again on the latest state
     (priority Emergência > Utilizador > Segurança > Energia > Conforto) and execute();
  3. fixed rules that never go through an LLM (on_event): smoke -> emergency protocol;
     intrusion alarm -> lights red at full brightness, put back when the alarm is cancelled.

Security (arm/disarm) is never written from here.
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from .agents.specs import AGENTS
from .config import Settings
from .memory import Decision, SharedMemory
from .perception.state import DeviceState, Event, HomeState, _num, de, em, hue_rgb
from .prefs.models import CATEGORY_PT
from .storage.decisions import decision_kind, map_status
from .writes import UnknownDevice, guard_device, route_write

log = logging.getLogger(__name__)

# role -> what users and agents may ask for (capability type and allowed values)
UI_ACTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "light": {"power": ("on", "off")},
    "plug": {"power": ("on", "off")},
}
NOUN = {"light": "a luz", "plug": "a tomada"}
PRIORITY = {"Emergência": 0, "Utilizador": 1, **{a.name: a.priority for a in AGENTS.values()}}  # lower wins
CONFLICT_WINDOW_S = 300  # an approved action blocks opposite proposals of lower priority this long
EMERGENCY_RGB = (255, 0, 0)
LIGHT_CHECK_S = 2.0  # read a light back this long after setting it (and re-send what did not stick)


class UserRequest(BaseModel):
    device_id: str = Field(min_length=1)
    action: Literal["on", "off"]
    origin: str = Field("3D", max_length=40)


class RequestRefused(ValueError):
    pass


AGENT_CATEGORY = {"Energia": "energia", "Conforto": "conforto", "Segurança": "seguranca"}
TIE_AGENTS = frozenset({"Energia", "Conforto"})  # their conflicts are settled by the user's preferences


@dataclass
class _Prop:
    ts: float
    agent: str
    device_id: str
    acao: str
    conflicts: list[str]
    motivo: str = ""


@dataclass
class PrefVerdict:
    """Outcome of decide(): ok=False drops the proposal; override lets it reverse a recent
    higher-priority proposal because the preferences say so. notes are PT-PT sentences."""
    ok: bool = True
    override: bool = False
    respects: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    other: _Prop | None = None

    @property
    def cited(self) -> list[str]:
        return [r["id"] for r in self.respects + self.conflicts]

    def proposals(self, agent: str, p) -> list[dict]:
        """The debate, for decisions.proposals: this proposal (+ the opposite one, if any)."""
        out = [{"agent": agent, **p.model_dump(), "respects_preferences": [r["id"] for r in self.respects],
                "conflicts_with_preferences": [r["id"] for r in self.conflicts]}]
        if self.other is not None:
            out.append({"agent": self.other.agent, "device_id": self.other.device_id, "acao": self.other.acao,
                        "motivo": self.other.motivo, "conflicts_with_preferences": self.other.conflicts})
        return out


def category_for(agent: str, dev: DeviceState | None) -> str:
    """Autonomy category of an action: lights are "luz" (unless it is a security action), the rest
    follows the agent that proposed it."""
    if agent == "Emergência":
        return "emergencia"
    if agent == "Utilizador":
        return "utilizador"
    if dev is not None and dev.role == "light" and agent != "Segurança":
        return "luz"
    return AGENT_CATEGORY.get(agent, "energia")


def trigger_dict(trigger: Event | dict | None) -> dict | None:
    if trigger is None:
        return None
    if isinstance(trigger, Event):
        return {"id": trigger.id, "type": trigger.type, "ts": trigger.ts, "room": trigger.room,
                "room_name": trigger.room_name, "device_id": trigger.device_id, "message": trigger.message}
    return dict(trigger)


def _what(dev: DeviceState, action: str) -> str:
    return f"{'ligar' if action == 'on' else 'desligar'} {NOUN.get(dev.role, dev.name)} {de(dev.room_name)}".strip()


class Coordinator:
    def __init__(self, memory: SharedMemory, settings: Settings, mcp=None, sim=None,
                 arbiter: Callable[[str], Any] | None = None, decisions=None, memories=None):
        """arbiter: prompt -> CoordinatorReply (the Coordinator LLM; set by AgentBridge.start()).
        memories: src.context.Memories (history, events, decisions, preferences). Without it there
        are no preferences and approved agent proposals run on their own (no autonomy levels)."""
        self.memory, self.s = memory, settings
        self.arbiter = arbiter
        self.memories = memories
        # src.storage.decisions.DecisionStore (None = not persisted)
        self.decisions = decisions if decisions is not None else (memories.decisions if memories else None)
        self.prefs = memories.prefs if memories else None
        self.on_pending: Callable[[dict], None] | None = None  # a decision now waits for the user
        self._props: deque[_Prop] = deque(maxlen=100)  # recent agent proposals (for decide())
        self._arb_lock = threading.Lock()
        self._mcp = mcp  # write-enabled McpClient, created only when a real write is due
        self._sim = sim
        self._lock = threading.Lock()  # one write at a time
        self._approved: deque[tuple[float, str, str, str]] = deque(maxlen=200)  # ts, device, value, source
        self.emergency_active = False
        self._light_before: dict[str, dict] = {}  # light state before an intrusion alarm
        self._verify: list = []  # (light, rgb, brightness) to read back after the next steps

    # -- guard ---------------------------------------------------------------
    def guard(self, state: HomeState, device_id: str, cap: str, value: str) -> DeviceState:
        dev = guard_device(state, device_id)  # UnknownDevice if not in the perceived state
        allowed = UI_ACTIONS.get(dev.role, {})
        if value not in allowed.get(cap, ()):
            raise RequestRefused(f"ação não permitida para {dev.name}")
        if not dev.online:
            raise RequestRefused(f"{dev.name} {de(dev.room_name)} está offline")
        return dev

    def _conflict(self, device_id: str, value: str, source: str, now: float) -> tuple[str, float] | None:
        """A recent approved opposite action by a higher-priority source on the same device."""
        prio = PRIORITY.get(source, 9)
        with self._lock:
            for ts, dev, val, src in reversed(self._approved):
                if dev == device_id and now - ts <= CONFLICT_WINDOW_S and val != value \
                        and PRIORITY.get(src, 9) < prio:
                    return src, now - ts
        return None

    def _remember(self, device_id: str, value: str, source: str) -> None:
        with self._lock:
            self._approved.append((time.time(), device_id, value, source))

    def _persist(self, *, status: str, explanation: str, dev: DeviceState | None = None,
                 body: dict | None = None, trigger: Event | dict | None = None, proposals: list | None = None,
                 autonomy: str | None = None, agent: str | None = None, decision: dict | None = None,
                 room: str | None = None, room_name: str | None = None) -> dict | None:
        """Record one outcome in the decisions table (data memory). Never raises."""
        if self.decisions is None:
            return None
        trig = trigger_dict(trigger)
        try:
            return self.decisions.add(
                trigger=trig, proposals=proposals, explanation=explanation, status=status, autonomy=autonomy,
                decision=decision if decision is not None else ({"device_id": dev.id, **body} if dev and body else {}),
                kind=decision_kind((trig or {}).get("type"), dev.id if dev else None, body),
                category=category_for(agent or "", dev), agent=agent,
                room=dev.room_id if dev else room, room_name=dev.room_name if dev else room_name,
                device_id=dev.id if dev else None)
        except Exception as e:  # noqa: BLE001 - the house must keep working without the database
            log.warning("could not record decision: %s", e)
            return None

    # -- 1. user requests ------------------------------------------------------
    def request(self, req: UserRequest) -> Decision:
        state = self.memory.state
        if state is None:
            raise RequestRefused("Estado da casa ainda não disponível.")
        body = {"type": "power", "value": req.action}
        try:
            dev = self.guard(state, req.device_id, "power", req.action)
        except UnknownDevice:
            raise
        except RequestRefused as e:
            d = state.devices.get(req.device_id)
            self._persist(status="vetoed", explanation=f"Pedido do utilizador recusado: {e}.", dev=d, body=body,
                          trigger={"type": "user_request", "origin": req.origin}, agent="Utilizador",
                          autonomy="utilizador")
            return self.memory.add_decision(Decision(
                agent="Coordenador", message=f"Pedido do utilizador ({req.origin}) recusado: {e}.",
                device_id=req.device_id, room=d.room_id if d else None, room_name=d.room_name if d else None,
                action=body, status="rejeitada"))
        status, note = self._run(state, dev, body)
        if status != "erro":
            self._remember(dev.id, req.action, "Utilizador")
        self._persist(status=map_status(status), explanation=f"Pedido do utilizador ({req.origin}): "
                      f"{_what(dev, req.action)}. {note}", dev=dev, body=body,
                      trigger={"type": "user_request", "origin": req.origin}, agent="Utilizador",
                      autonomy="utilizador")
        return self.memory.add_decision(Decision(
            agent="Coordenador", message=f"Pedido do utilizador ({req.origin}): {_what(dev, req.action)}. {note}",
            device_id=dev.id, room=dev.room_id, room_name=dev.room_name, action=body, status=status))

    # -- 2. proposals of the LLM agents -------------------------------------------
    def consider(self, agent: str, reply, events: list[Event] | None = None) -> list[Decision]:
        """reply: src.agents.specs.AgentReply. Records the agent's analysis; each proposal goes through
        the hard guardrails, then decide() (preferences, Energia/Conforto conflicts), then the
        Coordinator agent (LLM, via self.arbiter), then the guardrails again on the latest state and
        finally the autonomy level of its category (execute / ask the user / only inform)."""
        state = self.memory.state
        if state is None:
            return []
        events = events or []
        trig = events[-1] if events else None
        props = list(reply.propostas)
        devs = [state.devices.get(p.device_id) for p in props]
        anchor = next((d for d in devs if d), None) or (state.devices.get(trig.device_id) if trig and trig.device_id else None)
        msg = reply.resumo.strip().rstrip(".") + "."
        described = [_what(d, p.acao) if d else f"{p.acao} {p.device_id}" for d, p in zip(devs, props)]
        if described:
            msg += " Proponho " + "; ".join(described) + "."
        out = [self.memory.add_decision(Decision(
            agent=agent, message=msg, status="proposta" if props else "informação",
            device_id=anchor.id if anchor else None,
            room=anchor.room_id if anchor else (trig.room if trig else None),
            room_name=anchor.room_name if anchor else (trig.room_name if trig else None),
            action={"propostas": [p.model_dump() for p in props]} if props else None))]
        candidates: list[tuple[Any, DeviceState, PrefVerdict]] = []
        for p in props:
            checked = self._precheck(agent, p, state, check_conflict=False)
            if isinstance(checked, Decision):
                out.append(checked)
                if checked.status == "rejeitada":
                    self._persist(status="vetoed", explanation=checked.message, dev=state.devices.get(p.device_id),
                                  body={"type": "power", "value": p.acao}, trigger=trig, agent=agent,
                                  proposals=[{"agent": agent, **p.model_dump()}])
                continue
            verdict = self.decide(agent, p, checked, state)
            if not verdict.override:
                blocked = self._precheck(agent, p, state)  # recent higher-priority action
                if isinstance(blocked, Decision):
                    out.append(blocked)
                    self._persist(status="vetoed", explanation=blocked.message, dev=checked,
                                  body={"type": "power", "value": p.acao}, trigger=trig, agent=agent,
                                  proposals=[{"agent": agent, **p.model_dump()}])
                    continue
            if not verdict.ok:
                body = {"type": "power", "value": p.acao}
                text = f"Recuso a proposta {de(agent)} ({_what(checked, p.acao)}): " + " ".join(verdict.notes)
                out.append(self.memory.add_decision(Decision(**self._common(checked, body), status="rejeitada",
                                                             message=text)))
                self._persist(status="rejected", explanation=text, dev=checked, body=body, trigger=trig, agent=agent,
                              proposals=verdict.proposals(agent, p))
                continue
            candidates.append((p, checked, verdict))
        if candidates:
            out += self._arbitrate(agent, reply, candidates, events)
        return out

    def _precheck(self, agent: str, p, state: HomeState, check_conflict: bool = True) -> DeviceState | Decision:
        """Hard guardrails, before and after the LLM: known device, allowed action, online, not a
        no-op, no switching on during a smoke emergency, no reversing a recent higher-priority
        action. Returns the device, or the (recorded) Decision that stops the proposal."""
        body = {"type": "power", "value": p.acao}
        of = f"Proposta {de(agent)}"
        try:
            dev = self.guard(state, p.device_id, "power", p.acao)
        except UnknownDevice:
            return self.memory.add_decision(Decision(
                agent="Coordenador", message=f"{of} recusada: dispositivo desconhecido ({p.device_id}).",
                action=body, status="rejeitada"))
        except RequestRefused as e:
            d = state.devices.get(p.device_id)
            return self.memory.add_decision(Decision(
                agent="Coordenador", message=f"{of} recusada: {e}.", device_id=p.device_id,
                room=d.room_id if d else None, room_name=d.room_name if d else None, action=body, status="rejeitada"))
        common = self._common(dev, body)
        if dev.caps.get("power") == p.acao:
            return self.memory.add_decision(Decision(
                **common, status="informação",
                message=f"{of} ({_what(dev, p.acao)}): já está {'ligada' if p.acao == 'on' else 'desligada'}, nada a fazer."))
        if self.emergency_active and p.acao == "on":
            return self.memory.add_decision(Decision(
                **common, status="rejeitada",
                message=f"{of} recusada ({_what(dev, p.acao)}): há uma emergência de fumo ativa."))
        clash = self._conflict(dev.id, p.acao, agent, time.time()) if check_conflict else None
        if clash:
            src, age = clash
            return self.memory.add_decision(Decision(
                **common, status="rejeitada",
                message=f"{of} recusada ({_what(dev, p.acao)}): {src} decidiu o contrário há "
                        f"{int(age // 60)} min {int(age % 60)} s e tem prioridade."))
        return dev

    @staticmethod
    def _common(dev: DeviceState, body: dict) -> dict:
        return {"agent": "Coordenador", "device_id": dev.id, "room": dev.room_id, "room_name": dev.room_name,
                "action": body}

    def _recent(self, now: float) -> list[Decision]:
        return [d for d in self.memory.recent_decisions(40)
                if now - d.ts <= CONFLICT_WINDOW_S and d.status != "informação"][-12:]

    # -- decide(): preferences and conflicts between Energia and Conforto ------------------
    def evaluate_prefs(self, agent: str, p, dev: DeviceState, state: HomeState,
                       when: datetime | None = None) -> tuple[list[dict], list[dict]]:
        """(respected, conflicting) active preferences for one proposal. Computed in code from the
        real temperature; ids the agent cited are added only if they exist and apply."""
        if self.prefs is None:
            return [], []
        from .context import applicable_prefs
        when = when or datetime.now()
        applicable = {r["id"]: r for r in applicable_prefs(self.prefs, state, [dev.room_id])}
        respects: dict[str, dict] = {}
        conflicts: dict[str, dict] = {}
        if dev.role == "plug":  # the plug is the heater
            temp = _num(state.cap("temperature", "temperatureValue", room=dev.room_id))
            if temp is None:
                temp = state.temperature
            target, sp = self.prefs.setpoint(dev.room_id, when)
            if temp is not None and target is not None and sp is not None:
                if p.acao == "on":
                    (respects if temp < target else conflicts if temp >= target + 0.5 else {})[sp["id"]] = sp
                else:
                    (conflicts if temp < target - 0.5 else respects if temp >= target else {})[sp["id"]] = sp
            rng, cr = self.prefs.comfort_range(dev.room_id)
            if temp is not None and rng is not None:
                lo, hi = rng
                if p.acao == "off" and temp < lo:
                    conflicts[cr["id"]] = cr
                elif p.acao == "on" and temp > hi:
                    conflicts[cr["id"]] = cr
                elif lo <= temp <= hi or (p.acao == "on" and temp < lo) or (p.acao == "off" and temp > hi):
                    respects[cr["id"]] = cr
        # ids the agent cited: only real, applicable ones, and never for temperature preferences
        # (those depend on the time window and the real temperature, so only the code above decides)
        computed = {"setpoint_c", "comfort_range_c", "autonomy", "layout"}
        for pid in getattr(p, "respects_preferences", []) or []:
            if pid in applicable and pid not in conflicts and applicable[pid]["key"] not in computed:
                respects[pid] = applicable[pid]
        for pid in getattr(p, "conflicts_with_preferences", []) or []:
            if pid in applicable and pid not in respects and applicable[pid]["key"] not in computed:
                conflicts[pid] = applicable[pid]
        return list(respects.values()), list(conflicts.values())

    def decide(self, agent: str, p, dev: DeviceState, state: HomeState, now: float | None = None) -> "PrefVerdict":
        """Preferences first, then priority: when Energia and Conforto want opposite things for the same
        device (within CONFLICT_WINDOW_S), the proposal that respects the user's preferences wins; if
        both or neither do, the higher priority wins (Energia > Conforto). The notes cite the preferences."""
        from .context import describe_pref
        now = time.time() if now is None else now
        respects, conflicts = self.evaluate_prefs(agent, p, dev, state)
        notes: list[str] = []
        if respects:
            notes.append("Respeita a tua preferência: " + "; ".join(f"{describe_pref(r)} [{r['id']}]" for r in respects) + ".")
        if conflicts:
            notes.append("Vai contra a tua preferência: " + "; ".join(f"{describe_pref(r)} [{r['id']}]" for r in conflicts) + ".")
        ok, override, other = True, False, None
        if agent in TIE_AGENTS:
            with self._lock:
                for prop in reversed(self._props):
                    if prop.device_id == dev.id and prop.acao != p.acao and prop.agent != agent \
                            and prop.agent in TIE_AGENTS and now - prop.ts <= CONFLICT_WINDOW_S:
                        other = prop
                        break
        if other is not None:
            other_what = _what(dev, other.acao)
            if conflicts and not other.conflicts:
                ok = False
                notes.append(f"A proposta {de(other.agent)} ({other_what}) respeita as tuas preferências e esta não.")
            elif other.conflicts and not conflicts:
                override = True
                notes.append(f"Contraria a proposta {de(other.agent)} ({other_what}), mas as tuas preferências "
                             "desempatam a favor desta.")
            elif PRIORITY.get(agent, 9) > PRIORITY.get(other.agent, 9):
                ok = False
                notes.append(f"Sem preferência que desempate, prevalece a proposta {de(other.agent)} ({other_what}) "
                             "por prioridade.")
            else:
                override = True
                notes.append(f"Sem preferência que desempate, prevalece esta proposta por prioridade sobre a "
                             f"{de(other.agent)} ({other_what}).")
        verdict = PrefVerdict(ok=ok, override=override, respects=respects, conflicts=conflicts, notes=notes,
                              other=other)
        with self._lock:
            self._props.append(_Prop(now, agent, dev.id, p.acao, [c["id"] for c in conflicts],
                                     getattr(p, "motivo", "")))
        return verdict

    def autonomy(self, category: str, kind: str | None = None) -> str:
        """auto | perguntar | nunca. Without a preference memory (tests, old setups) agent proposals
        approved by the Coordinator run on their own, as before."""
        if self.prefs is None:
            return "auto"
        return self.prefs.autonomy(category, kind)

    def _arbitrate(self, agent: str, reply, candidates: list, events: list[Event]) -> list[Decision]:
        """Ask the Coordinator agent (one call for this agent's proposals; one arbitration at a time,
        so the next one sees this one's outcome). Without a valid answer nothing runs on its own: with
        a preference memory the user is asked, without one the proposal is dropped."""
        from .agents.bridge import build_coordinator_prompt
        from .agents.specs import BadReply
        with self._arb_lock:
            state = self.memory.state
            failure = None
            verdicts: dict[int, Any] = {}
            if self.arbiter is None:
                failure = "Coordenador LLM não está ligado (agentes desligados)"
            else:
                prompt = build_coordinator_prompt(
                    agent, reply, [(p, _what(dev, p.acao)) for p, dev, _ in candidates], events, state,
                    self._recent(time.time()), self.emergency_active, PRIORITY,
                    prefs_notes=[(n, " ".join(v.notes)) for n, (_, _, v) in enumerate(candidates, 1) if v.notes])
                try:
                    verdicts = {v.n: v for v in self.arbiter(prompt).decisoes}
                except BadReply as e:
                    failure = f"resposta inválida do Coordenador LLM ({str(e)[:120]})"
                except Exception as e:  # noqa: BLE001 - down, timeout
                    failure = f"Coordenador LLM indisponível ({type(e).__name__})"
            out = []
            trig = events[-1] if events else None
            for n, (p, dev, pv) in enumerate(candidates, 1):
                body = {"type": "power", "value": p.acao}
                what = _what(dev, p.acao)
                v = verdicts.get(n)
                props = pv.proposals(agent, p)
                if failure or v is None:
                    why = failure or "o Coordenador LLM não decidiu esta proposta"
                    if self.prefs is not None:  # nothing runs alone: the user decides
                        out.append(self._gate(agent, p, dev, body, trig, pv, motivo="", force_ask=True,
                                              extra=f"Não consegui confirmar com o Coordenador LLM ({why})."))
                        continue
                    d = self.memory.add_decision(Decision(
                        **self._common(dev, body), status="erro" if failure else "rejeitada",
                        message=f"Proposta {de(agent)} ({what}) não executada: {why}."))
                    out.append(d)
                    self._persist(status="rejected", explanation=d.message, dev=dev, body=body, trigger=trig,
                                  agent=agent, proposals=props)
                    continue
                motivo = (v.motivo or "").strip().rstrip(".")
                if not v.aprovar:
                    d = self.memory.add_decision(Decision(
                        **self._common(dev, body), status="rejeitada",
                        message=f"Recuso a proposta {de(agent)} ({what})" + (f": {motivo}." if motivo else ".")
                                + (" " + " ".join(pv.notes) if pv.notes else "")))
                    out.append(d)
                    self._persist(status="rejected", explanation=d.message, dev=dev, body=body, trigger=trig,
                                  agent=agent, proposals=props)
                    continue
                fresh = self.memory.state or state  # the house may have changed while the LLM thought
                checked = self._precheck(agent, p, fresh, check_conflict=not pv.override)
                if isinstance(checked, Decision):
                    out.append(checked)
                    if checked.status == "rejeitada":
                        self._persist(status="vetoed", explanation=checked.message, dev=dev, body=body,
                                      trigger=trig, agent=agent, proposals=props)
                    continue
                out.append(self._gate(agent, p, checked, body, trig, pv, motivo=motivo,
                                      force_ask=bool(pv.conflicts), state=fresh))
            return out

    def _gate(self, agent: str, p, dev: DeviceState, body: dict, trig: Event | None, pv: "PrefVerdict",
              motivo: str, force_ask: bool = False, extra: str = "", state: HomeState | None = None) -> Decision:
        """Autonomy level of the action's category: auto -> execute (DRY_RUN respected);
        perguntar -> pending decision shown in the chat; nunca -> only recorded and reported.
        force_ask (goes against a preference, or the LLM could not confirm) turns auto into perguntar."""
        category = category_for(agent, dev)
        kind = decision_kind(trig.type if trig else None, dev.id, body)
        level = self.autonomy(category, kind)
        if force_ask and level == "auto":
            level = "perguntar"
        what = _what(dev, p.acao)
        why = " ".join(x for x in [
            f"{trig.message}" if trig is not None else "",
            f"{agent} propõe {what}" + (f": {p.motivo.strip().rstrip('.')}." if getattr(p, "motivo", "") else "."),
            " ".join(pv.notes), f"Coordenador: {motivo}." if motivo else "", extra] if x).strip()
        props = pv.proposals(agent, p)
        if level == "auto":
            status, note = self._run(state or self.memory.state, dev, body)
            if status != "erro":
                self._remember(dev.id, p.acao, agent)
            d = self.memory.add_decision(Decision(
                **self._common(dev, body), status=status,
                message=f"Aprovo a proposta {de(agent)}: {what}." + (f" {motivo}." if motivo else "")
                        + (" " + " ".join(pv.notes) if pv.notes else "") + f" {note}"))
            self._persist(status=map_status(status), explanation=f"{why} {note}".strip(), dev=dev, body=body,
                          trigger=trig, agent=agent, proposals=props, autonomy="auto")
            return d
        if level == "perguntar":
            # one question per device and action, whatever event triggered it (no spam in the chat)
            if self.decisions is not None and any(
                    x["kind"] == kind or (x.get("device_id") == dev.id and (x.get("decision") or {}).get("value") == body.get("value"))
                    for x in self.decisions.pending()):
                return self.memory.add_decision(Decision(**self._common(dev, body), status="informação",
                                                         message=f"Já estou à espera da tua resposta para {what}."))
            rec = self._persist(status="pending", explanation=why, dev=dev, body=body, trigger=trig, agent=agent,
                                proposals=props, autonomy="perguntar")
            d = self.memory.add_decision(Decision(
                **self._common(dev, body), status="proposta",
                message=f"Proposta {de(agent)}: {what}. À espera da tua aprovação no chat."))
            if rec is not None and self.on_pending is not None:
                try:
                    self.on_pending(rec)
                except Exception:  # noqa: BLE001
                    log.exception("on_pending failed")
            return d
        self._persist(status="rejected", explanation=f"{why} Não executado: autonomia «nunca» para "
                      f"{CATEGORY_PT.get(category, category)}.", dev=dev, body=body, trigger=trig, agent=agent,
                      proposals=props, autonomy="nunca")
        return self.memory.add_decision(Decision(
            **self._common(dev, body), status="informação",
            message=f"Proposta {de(agent)} ({what}) não executada: pediste para eu nunca agir em "
                    f"{CATEGORY_PT.get(category, category)}. Fica registada."))

    # -- 3. fixed rules (deterministic, no LLM): smoke and intrusion -----------------
    FIXED_RULE_EVENTS = ("smoke_detected", "security_alarm", "alarm_cancelled")

    def on_event(self, event: Event) -> None:
        """Bus subscriber for FIXED_RULE_EVENTS."""
        if event.type == "smoke_detected":
            self.emergency(event)
        elif event.type == "security_alarm" and event.data.get("kind") == "intrusion":
            self.intrusion(event)
        elif event.type == "alarm_cancelled":
            if self.emergency_active:
                self.emergency_active = False
                self.memory.add_decision(Decision(agent="Emergência", status="informação",
                                                  message="Alarme cancelado: fim do protocolo de emergência."))
            if self._light_before:
                self.end_intrusion()

    @staticmethod
    def _light_steps(d: DeviceState, rgb: tuple[int, int, int] | None, brightness: float | None,
                     power: str | None = None) -> list[tuple[DeviceState, dict]]:
        """Steps to set a light. Brightness BEFORE colour: on the real bulb, a brightness write sent
        right after a colour write re-applies the old colour (the cloud still has it), so the light
        ended up at 100 % in its previous colour (2/2 trials); brightness then colour works (2/2).
        power "on" goes first, "off" last."""
        steps: list[tuple[DeviceState, dict]] = []
        if power == "on":
            steps.append((d, {"type": "power", "value": "on"}))
        if brightness is not None:
            steps.append((d, {"type": "brightness", "value": brightness}))
        if rgb is not None:
            r, g, b = rgb
            steps.append((d, {"type": "color_rgb", "redValue": r, "greenValue": g, "blueValue": b}))
        if power == "off":
            steps.append((d, {"type": "power", "value": "off"}))
        return steps

    def _red_light_steps(self, d: DeviceState) -> list[tuple[DeviceState, dict]]:
        self._verify.append((d, EMERGENCY_RGB, 100))
        return self._light_steps(d, EMERGENCY_RGB, 100, power="on")

    def _check_lights_later(self) -> None:
        """Read the lights back a moment later and re-send what did not stick (real MCP only)."""
        todo, self._verify = self._verify, []
        todo = [x for x in todo if x[0].source == "mcp"]
        if todo and not self.s.dry_run:
            threading.Thread(target=self._check_lights, args=(todo,), name="light-check", daemon=True).start()

    def _check_lights(self, todo: list, attempts: int = 2, wait_s: float | None = None) -> int:
        """Returns how many corrective writes were sent."""
        wait_s = LIGHT_CHECK_S if wait_s is None else wait_s
        sent = 0
        for _ in range(attempts):
            time.sleep(wait_s)
            missing = []
            for dev, rgb, brightness in todo:
                try:
                    d = self._mcp.call("get_device_details", deviceId=dev.id)
                    caps = {c["type"]: c.get("value") for c in (d.get("data", d) or {}).get("capabilities", [])}
                except Exception as e:  # noqa: BLE001
                    log.warning("could not read %s back: %s", dev.id, e)
                    continue
                ok_b = brightness is None or _num(caps.get("brightness")) == brightness
                ok_rgb = rgb is None or hue_rgb(caps.get("color_rgb")) == tuple(rgb)
                if not (ok_b and ok_rgb):
                    log.warning("luz %s não ficou como pedido (cor %s, brilho %s): a reenviar",
                                dev.id, caps.get("color_rgb"), caps.get("brightness"))
                    missing.append((dev, rgb, brightness))
            if not missing:
                return sent
            state = self.memory.state
            for dev, rgb, brightness in missing:
                for _, body in self._light_steps(dev, rgb, brightness):
                    try:
                        self.execute(state, dev, body)
                        sent += 1
                    except Exception as e:  # noqa: BLE001
                        log.warning("light re-send failed on %s: %s", dev.id, e)
            todo = missing
        return sent

    def _run_steps(self, state: HomeState, steps: list[tuple[DeviceState, dict]], source: str) -> tuple[str, str]:
        """Execute every step (keep going on failures) -> (status, note) for one Decision."""
        results = []
        for dev, body in steps:
            try:
                results.append(self.execute(state, dev, body)["dry_run"])
            except Exception as e:  # every step counts
                log.warning("%s: step failed on %s: %s", source, dev.id, e)
                results.append(None)
            if body["type"] == "power":
                self._remember(dev.id, body["value"], source)
        if None in results:
            return "erro", "Alguns passos falharam (ver o log)."
        if results and all(results):
            return "dry-run", "Dry-run: nada foi enviado."
        if results:
            return "executada", "Feito."
        return "informação", "Não havia nada a mudar."

    def emergency(self, event: Event) -> Decision | None:
        state = self.memory.state
        if state is None:
            return None
        self.emergency_active = True
        steps: list[tuple[DeviceState, dict]] = []
        for d in state.devices_by_role("plug"):
            if d.online and d.caps.get("power") == "on":
                steps.append((d, {"type": "power", "value": "off"}))
        for d in state.devices_by_role("light"):
            if d.online:
                steps += self._red_light_steps(d)
        status, note = self._run_steps(state, steps, "Emergência")
        self._check_lights_later()
        n_plugs = sum(1 for dev, b in steps if dev.role == "plug")
        actions = []
        if n_plugs:
            actions.append("desligar " + ("a tomada" if n_plugs == 1 else f"{n_plugs} tomadas"))
        if any(dev.role == "light" for dev, _ in steps):
            actions.append("pôr a luz a vermelho no máximo")
        if status == "informação":
            note = "Não havia nada para desligar."
        where = em(event.room_name) if event.room_name else "em casa"
        return self._persist_fixed(event, "Emergência", Decision(
            agent="Emergência", status=status, device_id=event.device_id, room=event.room,
            room_name=event.room_name,
            action={"passos": [{"device_id": d.id, **b} for d, b in steps]},
            message=f"Fumo {where}: protocolo de emergência (sem LLM)"
                    + (": " + " e ".join(actions) if actions else "") + f". {note}"))

    def intrusion(self, event: Event) -> Decision | None:
        """Intrusion alarm (house armed, a sensor fired): lights red at full brightness. The state
        of each light before the first alarm is kept and put back when the alarm is cancelled.
        Re-triggers of the same alarm do nothing if the light is already red."""
        state = self.memory.state
        if state is None:
            return None
        steps: list[tuple[DeviceState, dict]] = []
        for d in state.devices_by_role("light"):
            if not d.online:
                continue
            already = (d.caps.get("power") == "on" and hue_rgb(d.caps.get("color_rgb")) == EMERGENCY_RGB
                       and _num(d.caps.get("brightness")) == 100)
            if already:
                continue
            self._light_before.setdefault(d.id, {"power": d.caps.get("power"),
                                                 "hue": hue_rgb(d.caps.get("color_rgb")),
                                                 "brightness": _num(d.caps.get("brightness"))})
            steps += self._red_light_steps(d)
        if not steps:
            return None
        status, note = self._run_steps(state, steps, "Emergência")  # top priority for 5 min
        self._check_lights_later()
        where = em(event.room_name) if event.room_name else "em casa"
        light = steps[0][0]
        return self._persist_fixed(event, "Segurança", Decision(
            agent="Segurança", status=status, device_id=light.id, room=light.room_id, room_name=light.room_name,
            action={"passos": [{"device_id": d.id, **b} for d, b in steps]},
            message=f"Intrusão {where}: luz a vermelho no máximo (regra fixa, sem LLM). {note}"))

    def end_intrusion(self) -> Decision | None:
        """Alarm cancelled: put each light back as it was before the intrusion alarm."""
        state = self.memory.state
        before, self._light_before = self._light_before, {}
        if state is None:
            return None
        steps: list[tuple[DeviceState, dict]] = []
        for dev_id, prev in before.items():
            d = state.devices.get(dev_id)
            if d is None or not d.online:
                continue
            steps += self._light_steps(d, prev["hue"], prev["brightness"],
                                       power="off" if prev["power"] == "off" else None)
            if prev["power"] != "off":  # an off light cannot be checked for colour
                self._verify.append((d, prev["hue"], prev["brightness"]))
        if not steps:
            return None
        status, note = self._run_steps(state, steps, "Segurança")
        self._check_lights_later()
        light = steps[0][0]
        return self._persist_fixed({"type": "alarm_cancelled"}, "Segurança", Decision(
            agent="Segurança", status=status, device_id=light.id, room=light.room_id, room_name=light.room_name,
            action={"passos": [{"device_id": d.id, **b} for d, b in steps]},
            message=f"Alarme cancelado: luz reposta como estava antes da intrusão. {note}"))

    def _persist_fixed(self, trigger, agent: str, decision: Decision) -> Decision:
        """Fixed rules (no LLM, no preferences, no autonomy level): always executed."""
        self.memory.add_decision(decision)
        if decision.status != "informação":
            self._persist(status=map_status(decision.status), explanation=decision.message, trigger=trigger,
                          agent=agent, autonomy="emergencia", decision=decision.action or {},
                          room=decision.room, room_name=decision.room_name)
        return decision

    # -- the only write path ----------------------------------------------------
    def _run(self, state: HomeState, dev: DeviceState, body: dict) -> tuple[str, str]:
        try:
            dry = self.execute(state, dev, body)["dry_run"]
            return ("dry-run", "Dry-run: nada foi enviado.") if dry else ("executada", "Feito.")
        except Exception as e:  # MCP/sim failure: report, do not crash the caller
            log.warning("write failed for %s: %s", dev.id, e)
            return "erro", f"Falhou: {e}"

    def execute(self, state: HomeState, dev: DeviceState, body: dict) -> dict:
        with self._lock:
            mcp = None
            if self.s.simulate and dev.source != "sim" and self._mcp is None:
                # SIMULATE=true: the simulated house reuses the real device ids, so a real MCP client is
                # never created and the write goes to the simulator (an injected fake client still works)
                dev = dev.model_copy(update={"source": "sim"})
                state = state.model_copy(update={"devices": {**state.devices, dev.id: dev}})
            if not self.s.dry_run and dev.source == "mcp":
                if self._mcp is None:
                    from .mcp_client import McpClient
                    self._mcp = McpClient(allow_write=True)
                mcp = self._mcp
            return route_write(state, dev.id, body, self.s, mcp=mcp, sim=self._sim)

    # -- 4. answers from the chat: pending decisions, rules (MCP scenarios), security -------
    PENDING_TTL_S = 30 * 60  # an unanswered proposal goes stale: the house has moved on

    def resolve_pending(self, decision_id: str, response: str, comment: str | None = None) -> dict:
        """The user answered a pending decision (Aprovar / Rejeitar / Alterar). Approve re-checks the
        guardrails on the latest state and executes; the answer is stored with the decision."""
        if self.decisions is None:
            raise RequestRefused("Sem memória de decisões.")
        rec = self.decisions.get(decision_id)
        if rec is None:
            raise RequestRefused("Decisão desconhecida.")
        if rec["status"] != "pending" or rec.get("user_response"):
            raise RequestRefused("Esta decisão já não está à espera de resposta.")
        self.decisions.respond(decision_id, response, comment)
        if response != "approved":
            self.decisions.set_status(decision_id, "rejected",
                                      explanation=(rec["explanation"] or "") + (" Rejeitada por ti." if response == "rejected"
                                                                               else " Alterada por ti."))
            return self.decisions.get(decision_id)
        if time.time() - rec["ts"] > self.PENDING_TTL_S:
            self.decisions.set_status(decision_id, "vetoed", explanation=(rec["explanation"] or "") +
                                      " Expirou: passou demasiado tempo e a casa pode ter mudado.")
            raise RequestRefused("Esta proposta expirou (mais de 30 min). Se ainda fizer sentido, os agentes voltam a propor.")
        state = self.memory.state
        body = {k: v for k, v in (rec["decision"] or {}).items() if k != "device_id"}
        dev_id = (rec["decision"] or {}).get("device_id")
        try:
            dev = self.guard(state, dev_id, body.get("type", "power"), body.get("value"))
        except (UnknownDevice, RequestRefused) as e:
            self.decisions.set_status(decision_id, "vetoed", explanation=(rec["explanation"] or "") + f" Não executada: {e}.")
            return self.decisions.get(decision_id)
        if self.emergency_active and body.get("value") == "on":
            self.decisions.set_status(decision_id, "vetoed", explanation=(rec["explanation"] or "")
                                      + " Não executada: há uma emergência de fumo ativa.")
            return self.decisions.get(decision_id)
        if dev.caps.get(body.get("type")) == body.get("value"):
            self.decisions.set_status(decision_id, "executed", explanation=(rec["explanation"] or "") + " Já estava assim.")
            return self.decisions.get(decision_id)
        status, note = self._run(state, dev, body)
        if status != "erro":
            self._remember(dev.id, body.get("value"), "Utilizador")
        self.decisions.set_status(decision_id, map_status(status), explanation=f"{rec['explanation'] or ''} Aprovada por ti. {note}".strip())
        self.memory.add_decision(Decision(**self._common(dev, body), status=status,
                                          message=f"Aprovado por ti: {_what(dev, body.get('value'))}. {note}"))
        for other in self.decisions.pending():  # twins of this question are answered by it
            if other["id"] != decision_id and other.get("device_id") == dev.id \
                    and (other.get("decision") or {}).get("value") == body.get("value"):
                self.decisions.set_status(other["id"], "vetoed",
                                          explanation=(other["explanation"] or "") + f" Resolvida pela decisão {decision_id}.")
        return self.decisions.get(decision_id)

    def guard_scenario(self, state: HomeState, payload: dict) -> None:
        """Rules for scenarios we create: TT_ name, only devices that exist, only light/plug actions."""
        name = str(payload.get("name", ""))
        if not name.startswith("TT_"):
            raise RequestRefused("os cenários criados pelo Coordenador têm de começar por TT_")
        if not payload.get("triggers"):
            raise RequestRefused("um cenário precisa de pelo menos uma condição")
        for part in ("triggers", "startActions", "endActions"):
            for item in payload.get(part) or []:
                dev_id = (item.get("device") or {}).get("id")
                dev = state.devices.get(dev_id)
                if dev is None:
                    raise RequestRefused(f"dispositivo desconhecido no cenário: {dev_id}")
                if part != "triggers" and dev.role not in ("light", "plug"):
                    raise RequestRefused(f"o cenário só pode atuar em luzes e tomadas, não em {dev.name}")

    def execute_tool(self, tool: str, args: dict) -> dict:
        """The only path for scenario / security writes (DRY_RUN respected; SIMULATE keeps them in a file)."""
        with self._lock:
            if self.s.dry_run:
                return {"tool": tool, "args": args, "dry_run": True, "executed": False}
            if self.s.simulate:
                return {"tool": tool, "args": args, "dry_run": False, "executed": True,
                        "result": _sim_tool(self.s, tool, args)}
            if self._mcp is None:
                from .mcp_client import McpClient
                self._mcp = McpClient(allow_write=True)
            return {"tool": tool, "args": args, "dry_run": False, "executed": True,
                    "result": self._mcp.call(tool, **args)}

    def create_scenario(self, payload: dict, explanation: str) -> dict:
        """User-confirmed rule -> post_new_scenario (guard + execute). Returns {status, scenario_id, note}."""
        state = self.memory.state
        if state is None:
            raise RequestRefused("Estado da casa ainda não disponível.")
        self.guard_scenario(state, payload)
        try:
            out = self.execute_tool("post_new_scenario", payload)
        except Exception as e:  # noqa: BLE001
            self._persist(status="rejected", explanation=f"{explanation} Falhou: {e}", agent="Utilizador",
                          trigger={"type": "chat_rule"}, decision={"tool": "post_new_scenario", **payload})
            raise RequestRefused(f"o MCP recusou o cenário ({str(e)[:120]})") from None
        result = out.get("result") or {}
        if isinstance(result, dict):
            result = result.get("data", result)
        scenario_id = result.get("id") if isinstance(result, dict) else None
        status = "dry_run" if out["dry_run"] else "executed"
        note = ("Dry-run: o cenário não foi criado no MCP." if out["dry_run"] else
                "Cenário criado no simulador." if self.s.simulate else "Cenário criado no MCP.")
        self._persist(status=status, explanation=f"{explanation} {note}", agent="Utilizador", autonomy="utilizador",
                      trigger={"type": "chat_rule"}, decision={"tool": "post_new_scenario", "scenario_id": scenario_id,
                                                               **payload})
        self.memory.add_decision(Decision(agent="Coordenador", status="dry-run" if out["dry_run"] else "executada",
                                          message=f"Regra {payload['name']}: {note}"))
        return {"status": status, "scenario_id": scenario_id, "note": note}

    def set_scenario_active(self, scenario_id: str | None, active: bool, name: str = "") -> dict:
        """Revoking a rule deactivates its scenario (there is no tool to delete scenarios)."""
        if not scenario_id:
            return {"status": "skipped", "note": "A regra não tinha cenário no MCP."}
        out = self.execute_tool("patch_scenario_activation", {"scenarioId": scenario_id, "isActive": active})
        status = "dry_run" if out["dry_run"] else "executed"
        note = ("Dry-run: " if out["dry_run"] else "") + f"cenário {name or scenario_id} {'ativado' if active else 'desativado'}."
        self._persist(status=status, explanation=note, agent="Utilizador", autonomy="utilizador",
                      trigger={"type": "chat_rule"}, decision={"tool": "patch_scenario_activation",
                                                               "scenarioId": scenario_id, "isActive": active})
        return {"status": status, "note": note}

    SECURITY_MODES = {"armar_casa": "STAY", "armar_ausente": "AWAY", "desarmar": "DISARMED"}

    def set_security(self, mode: str, confirmed: bool) -> dict:
        """Arm/disarm: always needs the user's explicit confirmation (no autonomy level, no agent)."""
        if not confirmed:
            raise RequestRefused("armar ou desarmar exige sempre a tua confirmação")
        if mode not in ("STAY", "AWAY", "DISARMED"):
            raise RequestRefused("modo de segurança inválido")
        out = self.execute_tool("patch_home_security_status", {"status": mode})
        status = "dry_run" if out["dry_run"] else "executed"
        note = "Dry-run: nada foi enviado." if out["dry_run"] else "Feito."
        self._persist(status=status, explanation=f"Segurança → {mode}, confirmado por ti. {note}", agent="Utilizador",
                      autonomy="confirmacao", trigger={"type": "chat_security"},
                      decision={"tool": "patch_home_security_status", "status": mode})
        return {"status": status, "note": note}


def _sim_tool(settings, tool: str, args: dict):
    """SIMULATE=true: scenarios live in var/sim_scenarios.json (no MCP)."""
    import json
    import uuid
    path = settings.sim_state_file.with_name("sim_scenarios.json")
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"scenarios": [], "security": None}
    result: dict = {}
    if tool == "post_new_scenario":
        result = {**args, "id": uuid.uuid4().hex}
        data["scenarios"].append(result)
    elif tool == "patch_scenario_activation":
        for sc in data["scenarios"]:
            if sc["id"] == args.get("scenarioId"):
                sc["isActive"] = bool(args.get("isActive"))
                result = sc
    elif tool == "patch_home_security_status":
        data["security"] = args.get("status")
        result = {"status": args.get("status")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return result
