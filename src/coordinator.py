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
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from .agents.specs import AGENTS
from .config import Settings
from .memory import Decision, SharedMemory
from .perception.state import DeviceState, Event, HomeState, _num, de, em, hue_rgb
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
                 arbiter: Callable[[str], Any] | None = None, decisions=None):
        """arbiter: prompt -> CoordinatorReply (the Coordinator LLM; set by AgentBridge.start()).
        Without it, agent proposals are never executed."""
        self.memory, self.s = memory, settings
        self.arbiter = arbiter
        self.decisions = decisions  # src.storage.decisions.DecisionStore (None = not persisted)
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
        """reply: src.agents.specs.AgentReply. Records the agent's analysis; proposals that pass the
        guardrails go to the Coordinator agent (LLM, via self.arbiter); the approved ones are
        checked again against the latest state and executed."""
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
        candidates: list[tuple[Any, DeviceState]] = []
        for p in props:
            checked = self._precheck(agent, p, state)
            if isinstance(checked, Decision):
                out.append(checked)
            else:
                candidates.append((p, checked))
        if candidates:
            out += self._arbitrate(agent, reply, candidates, events)
        return out

    def _precheck(self, agent: str, p, state: HomeState) -> DeviceState | Decision:
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
        clash = self._conflict(dev.id, p.acao, agent, time.time())
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

    def _arbitrate(self, agent: str, reply, candidates: list, events: list[Event]) -> list[Decision]:
        """Ask the Coordinator agent (one call for this agent's proposals; one arbitration at a time,
        so the next one sees this one's outcome). Without a valid answer nothing is executed."""
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
                    agent, reply, [(p, _what(dev, p.acao)) for p, dev in candidates], events, state,
                    self._recent(time.time()), self.emergency_active, PRIORITY)
                try:
                    verdicts = {v.n: v for v in self.arbiter(prompt).decisoes}
                except BadReply as e:
                    failure = f"resposta inválida do Coordenador LLM ({str(e)[:120]})"
                except Exception as e:  # noqa: BLE001 - down, timeout
                    failure = f"Coordenador LLM indisponível ({type(e).__name__})"
            out = []
            for n, (p, dev) in enumerate(candidates, 1):
                body = {"type": "power", "value": p.acao}
                what = _what(dev, p.acao)
                v = verdicts.get(n)
                trig = events[-1] if events else None
                prop = [{"agent": agent, **p.model_dump()}]
                if failure or v is None:
                    why = failure or "o Coordenador LLM não decidiu esta proposta"
                    d = self.memory.add_decision(Decision(
                        **self._common(dev, body), status="erro" if failure else "rejeitada",
                        message=f"Proposta {de(agent)} ({what}) não executada: {why}."))
                    out.append(d)
                    self._persist(status="rejected", explanation=d.message, dev=dev, body=body, trigger=trig,
                                  agent=agent, proposals=prop)
                    continue
                motivo = (v.motivo or "").strip().rstrip(".")
                if not v.aprovar:
                    d = self.memory.add_decision(Decision(
                        **self._common(dev, body), status="rejeitada",
                        message=f"Recuso a proposta {de(agent)} ({what})" + (f": {motivo}." if motivo else ".")))
                    out.append(d)
                    self._persist(status="rejected", explanation=d.message, dev=dev, body=body, trigger=trig,
                                  agent=agent, proposals=prop)
                    continue
                fresh = self.memory.state or state  # the house may have changed while the LLM thought
                checked = self._precheck(agent, p, fresh)
                if isinstance(checked, Decision):
                    out.append(checked)
                    if checked.status == "rejeitada":
                        self._persist(status="vetoed", explanation=checked.message, dev=dev, body=body,
                                      trigger=trig, agent=agent, proposals=prop)
                    continue
                status, note = self._run(fresh, checked, body)
                if status != "erro":
                    self._remember(checked.id, p.acao, agent)
                d = self.memory.add_decision(Decision(
                    **self._common(checked, body), status=status,
                    message=f"Aprovo a proposta {de(agent)}: {what}." + (f" {motivo}." if motivo else "") + f" {note}"))
                out.append(d)
                self._persist(status=map_status(status), explanation=d.message, dev=checked, body=body,
                              trigger=trig, agent=agent, proposals=prop, autonomy="auto")
            return out

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
            if not self.s.dry_run and dev.source == "mcp":
                if self._mcp is None:
                    from .mcp_client import McpClient
                    self._mcp = McpClient(allow_write=True)
                mcp = self._mcp
            return route_write(state, dev.id, body, self.s, mcp=mcp, sim=self._sim)
