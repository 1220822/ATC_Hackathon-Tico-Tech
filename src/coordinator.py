"""Coordinator: the only component that writes to the house, through execute().

This is the minimal part needed by the 3D view: user requests (POST /coordinator/request) for
simple actions. The full Coordinator (agent proposals, conflict resolution, LLM) builds on the
same request() -> guard -> execute() path. The emergency path does not go through here.

Rules kept:
  - only simple, reversible actions from the UI: power on/off of a light or a plug;
    security (arm/disarm) and anything else is refused here
  - guard(): the device must exist in the latest perceived state, be online and allow the action
  - dry-run by default (DRY_RUN in .env): the decision is recorded, nothing is sent
  - writes go where the device lives (route_write: "mcp" -> MCP tool, "sim" -> SimHome)
  - every request becomes a Decision in SharedMemory, so it shows in the UI timeline
"""

import logging
import threading
from typing import Literal

from pydantic import BaseModel, Field

from .config import Settings
from .memory import Decision, SharedMemory
from .perception.state import DeviceState, HomeState, de
from .writes import UnknownDevice, guard_device, route_write

log = logging.getLogger(__name__)

# role -> what the UI may ask for (capability type and allowed values)
UI_ACTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "light": {"power": ("on", "off")},
    "plug": {"power": ("on", "off")},
}
NOUN = {"light": "a luz", "plug": "a tomada"}


class UserRequest(BaseModel):
    device_id: str = Field(min_length=1)
    action: Literal["on", "off"]
    origin: str = Field("3D", max_length=40)


class RequestRefused(ValueError):
    pass


class Coordinator:
    def __init__(self, memory: SharedMemory, settings: Settings, mcp=None, sim=None):
        self.memory, self.s = memory, settings
        self._mcp = mcp  # write-enabled McpClient, created only when a real write is due
        self._sim = sim
        self._lock = threading.Lock()  # one write at a time

    # -- guard ---------------------------------------------------------------
    def guard(self, state: HomeState, device_id: str, cap: str, value: str) -> DeviceState:
        dev = guard_device(state, device_id)  # UnknownDevice if not in the perceived state
        allowed = UI_ACTIONS.get(dev.role, {})
        if value not in allowed.get(cap, ()):
            raise RequestRefused(f"Ação não permitida para {dev.name}.")
        if not dev.online:
            raise RequestRefused(f"{dev.name} {de(dev.room_name)} está offline.")
        return dev

    # -- entry point for the UI -----------------------------------------------
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
                agent="Coordenador", message=f"Pedido do utilizador ({req.origin}) recusado: {e}",
                device_id=req.device_id, room=d.room_id if d else None, room_name=d.room_name if d else None,
                action=body, status="rejeitada"))
        what = f"{'ligar' if req.action == 'on' else 'desligar'} {NOUN[dev.role]} {de(dev.room_name)}"
        try:
            result = self.execute(state, dev, body)
            dry = result["dry_run"]
            status = "dry-run" if dry else "executada"
            msg = f"Pedido do utilizador ({req.origin}): {what}. " + \
                ("Dry-run: nada foi enviado." if dry else "Feito.")
        except Exception as e:  # MCP/sim failure: report, do not crash the API
            log.warning("write failed for %s: %s", dev.id, e)
            status, msg = "erro", f"Pedido do utilizador ({req.origin}): {what}. Falhou: {e}"
        return self.memory.add_decision(Decision(
            agent="Coordenador", message=msg, device_id=dev.id, room=dev.room_id, room_name=dev.room_name,
            action=body, status=status))

    # -- the only write path ----------------------------------------------------
    def execute(self, state: HomeState, dev: DeviceState, body: dict) -> dict:
        with self._lock:
            mcp = None
            if not self.s.dry_run and dev.source == "mcp":
                if self._mcp is None:
                    from .mcp_client import McpClient
                    self._mcp = McpClient(allow_write=True)
                mcp = self._mcp
            return route_write(state, dev.id, body, self.s, mcp=mcp, sim=self._sim)
