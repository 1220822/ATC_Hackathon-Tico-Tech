"""Where raw home data comes from: the real MCP or the simulated home. Both return
{"home": <get_home>, "alarm_history": [...]}."""

from typing import Protocol

from ..config import Settings


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
        return {"home": home, "alarm_history": (history or {}).get("history", [])}


class SimSource:
    name = "sim"

    def __init__(self, settings: Settings):
        from ..sim.home import SimHome
        self.home = SimHome(settings.sim_state_file)

    def fetch(self) -> dict:
        return self.home.snapshot()


def make_source(settings: Settings) -> Source:
    return SimSource(settings) if settings.simulate else McpSource()
