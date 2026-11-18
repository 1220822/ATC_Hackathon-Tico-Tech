"""Coordinator: the only component that writes to the house, through execute().

Three inputs, one write path (guard -> execute -> route_write, dry-run by default), and every
outcome recorded as a Decision in SharedMemory (UI timeline / 3D balloons):

  1. user requests from the UI (POST /coordinator/request): power on/off of a light or plug;
  2. proposals of the LLM agents (Segurança, Energia, Conforto) received over A2A by
     src.agents.bridge -> consider(): checked device by device and arbitrated by priority
     (Emergência > Utilizador > Segurança > Energia > Conforto);
  3. smoke: a deterministic emergency path (on_event) that never goes through an LLM.

Security (arm/disarm) is never written from here.
"""

import logging
import threading
import time
from collections import deque
from typing import Literal

from pydantic import BaseModel, Field

from .agents.specs import AGENTS
from .config import Settings
from .memory import Decision, SharedMemory
from .perception.state import DeviceState, Event, HomeState, de, em
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


class UserRequest(BaseModel):
    device_id: str = Field(min_length=1)
    action: Literal["on", "off"]
    origin: str = Field("3D", max_length=40)


class RequestRefused(ValueError):
    pass


def _what(dev: DeviceState, action: str) -> str:
    return f"{'ligar' if action == 'on' else 'desligar'} {NOUN.get(dev.role, dev.name)} {de(dev.room_name)}".strip()


class Coordinator:
    def __init__(self, memory: SharedMemory, settings: Settings, mcp=None, sim=None):
        self.memory, self.s = memory, settings
        self._mcp = mcp  # write-enabled McpClient, created only when a real write is due
        self._sim = sim
        self._lock = threading.Lock()  # one write at a time
        self._approved: deque[tuple[float, str, str, str]] = deque(maxlen=200)  # ts, device, value, source
        self.emergency_active = False

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
            return self.memory.add_decision(Decision(
                agent="Coordenador", message=f"Pedido do utilizador ({req.origin}) recusado: {e}.",
                device_id=req.device_id, room=d.room_id if d else None, room_name=d.room_name if d else None,
                action=body, status="rejeitada"))
        status, note = self._run(state, dev, body)
        if status != "erro":
            self._remember(dev.id, req.action, "Utilizador")
        return self.memory.add_decision(Decision(
            agent="Coordenador", message=f"Pedido do utilizador ({req.origin}): {_what(dev, req.action)}. {note}",
            device_id=dev.id, room=dev.room_id, room_name=dev.room_name, action=body, status=status))

    # -- 2. proposals of the LLM agents -------------------------------------------
    def consider(self, agent: str, reply, events: list[Event] | None = None) -> list[Decision]:
        """reply: src.agents.specs.AgentReply. Records the agent's analysis, then decides on each
        proposal (guard, emergency, conflicts by priority, no-ops) and executes the approved ones."""
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
        for p in props:
            out.append(self._arbitrate(agent, p, state))
        return out

    def _arbitrate(self, agent: str, p, state: HomeState) -> Decision:
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
        common = {"agent": "Coordenador", "device_id": dev.id, "room": dev.room_id, "room_name": dev.room_name,
                  "action": body}
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
        status, note = self._run(state, dev, body)
        if status != "erro":
            self._remember(dev.id, p.acao, agent)
        return self.memory.add_decision(Decision(
            **common, status=status, message=f"Aprovo a proposta {de(agent)}: {_what(dev, p.acao)}. {note}"))

    # -- 3. emergency (deterministic, no LLM) -------------------------------------
    def on_event(self, event: Event) -> None:
        """Bus subscriber for smoke_detected / alarm_cancelled."""
        if event.type == "smoke_detected":
            self.emergency(event)
        elif event.type == "alarm_cancelled" and self.emergency_active:
            self.emergency_active = False
            self.memory.add_decision(Decision(agent="Emergência", status="informação",
                                              message="Alarme cancelado: fim do protocolo de emergência."))

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
                r, g, b = EMERGENCY_RGB  # colour first, then brightness (see docs/capabilities/probes)
                steps += [(d, {"type": "power", "value": "on"}),
                          (d, {"type": "color_rgb", "redValue": r, "greenValue": g, "blueValue": b}),
                          (d, {"type": "brightness", "value": 100})]
        results = []
        for dev, body in steps:
            try:
                results.append(self.execute(state, dev, body)["dry_run"])
            except Exception as e:  # keep going: every step counts in an emergency
                log.warning("emergency step failed on %s: %s", dev.id, e)
                results.append(None)
            if body["type"] == "power":
                self._remember(dev.id, body["value"], "Emergência")
        n_plugs = sum(1 for dev, b in steps if dev.role == "plug")
        actions = []
        if n_plugs:
            actions.append("desligar " + ("a tomada" if n_plugs == 1 else f"{n_plugs} tomadas"))
        if any(dev.role == "light" for dev, _ in steps):
            actions.append("pôr a luz a vermelho no máximo")
        where = em(event.room_name) if event.room_name else "em casa"
        if None in results:
            status, note = "erro", "Alguns passos falharam (ver o log)."
        elif results and all(results):
            status, note = "dry-run", "Dry-run: nada foi enviado."
        elif results:
            status, note = "executada", "Feito."
        else:
            status, note = "informação", "Não havia nada para desligar."
        return self.memory.add_decision(Decision(
            agent="Emergência", status=status, device_id=event.device_id, room=event.room,
            room_name=event.room_name,
            action={"passos": [{"device_id": d.id, **b} for d, b in steps]},
            message=f"Fumo {where}: protocolo de emergência (sem LLM)"
                    + (": " + " e ".join(actions) if actions else "") + f". {note}"))

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
