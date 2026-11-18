"""Write routing for the Coordinator. Not wired yet: there is no execute()/Coordinator in the
repo. When it exists, its execute() must send every capability write through route_write().

Rules this keeps:
  - only the Coordinator writes: route_write() never creates a write-enabled MCP client; the
    Coordinator passes its own McpClient(allow_write=True)
  - dry-run by default (Settings.dry_run, DRY_RUN in .env): returns the plan, writes nothing
  - guard: a device that is not in the current HomeState is rejected (UnknownDevice)
  - the write goes where the device lives (DeviceState.source):
        "mcp" -> MCP tool update_device_capabilities(deviceId=..., body=...)
        "sim" -> SimHome.apply_capability(device_id, body)   (same body format)

Body format (tools_schema.json): {"type": "power", "value": "on"|"off"},
{"type": "brightness", "value": 1..100}, {"type": "color_rgb", "redValue":..,"greenValue":..,"blueValue":..}.
"""

from typing import Any

from .config import Settings
from .perception.state import DeviceState, HomeState


class UnknownDevice(ValueError):
    pass


def guard_device(state: HomeState, device_id: str) -> DeviceState:
    """The device must exist in the latest perceived state (no writes to invented ids)."""
    dev = state.devices.get(device_id)
    if dev is None:
        raise UnknownDevice(f"dispositivo desconhecido: {device_id}")
    return dev


def route_write(state: HomeState, device_id: str, body: dict, settings: Settings, *,
                mcp: Any = None, sim: Any = None, dry_run: bool | None = None) -> dict:
    """Apply one capability write to the device's source (or only plan it in dry-run).

    mcp: the Coordinator's McpClient(allow_write=True), needed for source "mcp".
    sim: a SimHome, needed for source "sim" (defaults to make_sim_home(settings)).
    Returns {"device_id", "device", "room", "source", "body", "dry_run", "executed"[, "result"]}.
    """
    dev = guard_device(state, device_id)
    if not isinstance(body, dict) or "type" not in body:
        raise ValueError("body tem de ter 'type' (power | brightness | color_rgb | temperature)")
    dry = settings.dry_run if dry_run is None else dry_run
    plan = {"device_id": dev.id, "device": dev.name, "room": dev.room_name, "source": dev.source,
            "body": body, "dry_run": dry, "executed": False}
    if dry:
        return plan
    if dev.source == "sim":
        if sim is None:
            from .sim.home import make_sim_home
            sim = make_sim_home(settings)
        result = sim.apply_capability(dev.id, body)
    else:
        if mcp is None:
            raise RuntimeError("route_write precisa do McpClient com escrita do Coordenador")
        result = mcp.call("update_device_capabilities", deviceId=dev.id, body=body)
    return {**plan, "executed": True, "result": result}
