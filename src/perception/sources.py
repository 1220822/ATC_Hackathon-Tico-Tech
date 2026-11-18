"""Where raw home data comes from: the real MCP, the simulated home, or both. All return
{"home": <get_home>, "alarm_history": [...]} plus two annotations read by normalize():

    "source":    "mcp" | "sim"  -> default DeviceState.source
    "sim_rooms": [room ids]     -> rooms defined in config/home.json as simulated; their
                                   devices are always source "sim" (RoomState.simulated)

Only read tools are used here.
"""

from typing import Protocol

from ..config import Settings
from ..home_config import HomeConfig, load_home_config


class Source(Protocol):
    name: str

    def fetch(self) -> dict: ...


class McpSource:
    name = "mcp"

    def __init__(self, client=None):
        if client is None:
            from ..mcp_client import McpClient
            client = McpClient(allow_write=False)
        self.client = client

    def fetch(self) -> dict:
        home = self.client.call("get_home")
        history = self.client.call("get_home_security_alarm_history", page=0, size=10)
        return {"home": home, "alarm_history": (history or {}).get("history", []), "source": "mcp"}


class SimSource:
    name = "sim"

    def __init__(self, settings: Settings, config: HomeConfig | None = None):
        from ..sim.home import SimHome
        config = config if config is not None else load_home_config(settings.home_config_file)
        self.sim_rooms = config.sim_room_ids
        self.home = SimHome(settings.sim_state_file, config.simulated_rooms)

    def fetch(self) -> dict:
        raw = self.home.snapshot()
        raw.update(source="sim", sim_rooms=self.sim_rooms)
        return raw


class HybridSource:
    """SIMULATE=true: everything from SimHome (real rooms seeded from the dump + simulated rooms).
    SIMULATE=false: real get_home from the MCP plus the simulated rooms of config/home.json,
    whose state lives in the SimHome file (so scripts/simulate.py --room can drive them)."""

    def __init__(self, settings: Settings, client=None, config: HomeConfig | None = None):
        config = config if config is not None else load_home_config(settings.home_config_file)
        self.simulate = settings.simulate
        self.sim = SimSource(settings, config) if settings.simulate or config.sim_room_ids else None
        self.mcp = None if settings.simulate else McpSource(client)
        self.name = "sim" if self.simulate else ("mcp+sim" if self.sim else "mcp")

    def fetch(self) -> dict:
        if self.simulate:
            return self.sim.fetch()
        raw = self.mcp.fetch()
        if self.sim is None:
            return raw
        snap = self.sim.fetch()
        ids = set(self.sim.sim_rooms)
        sim_rooms = [r for r in snap["home"].get("rooms") or [] if r["id"] in ids]
        sim_devices = {d["id"] for r in sim_rooms for d in r.get("devices") or []}
        home = dict(raw.get("home") or {})
        home["rooms"] = list(home.get("rooms") or []) + sim_rooms
        alarms = list(raw.get("alarm_history") or []) + [
            e for e in snap.get("alarm_history") or []
            if ((e.get("alarmEventDetail") or {}).get("device") or {}).get("id") in sim_devices]
        alarms.sort(key=lambda e: e.get("eventTimestamp") or 0, reverse=True)
        return {"home": home, "alarm_history": alarms, "source": "mcp", "sim_rooms": self.sim.sim_rooms}


def make_source(settings: Settings) -> Source:
    return HybridSource(settings)
