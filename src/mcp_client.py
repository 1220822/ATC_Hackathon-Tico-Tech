"""Thin adapter over scripts/mcp.py: reuses its Session (and therefore the auth helper).

The CLI raises SystemExit on transport/JSON-RPC errors; here they become McpError so a
long-running process survives them.
"""

import importlib.util
import json
import os
import sys
import threading
from typing import Any

from .config import ROOT

_SCRIPTS = ROOT / "scripts"


def _load_cli():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))  # scripts/mcp.py imports connected_home_auth
    spec = importlib.util.spec_from_file_location("connected_home_mcp_cli", _SCRIPTS / "mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cli = _load_cli()

READ_TOOLS = frozenset({
    "get_home", "get_home_devices", "get_home_rooms", "get_home_status_brief",
    "get_home_detail_report", "get_device_details", "get_device_recommendations",
    "get_device_timers", "get_home_scenarios", "get_scenario",
    "get_home_security_status", "get_home_security_alarm_history",
})
WRITE_TOOLS = frozenset({
    "update_device_capabilities", "post_timer_device", "post_new_scenario",
    "patch_scenario_activation", "post_execute_scenario", "patch_home_security_status",
})


class McpError(RuntimeError):
    pass


class McpClient:
    """Thread-safe MCP client. `allow_write` must be True to call write tools
    (only the coordinator's execute() creates such a client)."""

    def __init__(self, allow_write: bool = False):
        self.allow_write = allow_write
        self._session = None
        self._lock = threading.Lock()

    def _ensure_session(self):
        if self._session is None:
            try:
                self._session = _cli.Session()
            except SystemExit as e:
                raise McpError(str(e)) from None
        return self._session

    def call(self, tool: str, **args: Any) -> Any:
        if tool in WRITE_TOOLS and not self.allow_write:
            raise PermissionError(f"{tool} is a write tool; only the coordinator may call it")
        if tool in WRITE_TOOLS and (os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TT_NO_REAL_WRITES")):
            # tests (and anything that sets TT_NO_REAL_WRITES) can never write to the real house
            raise PermissionError(f"{tool}: escritas reais bloqueadas durante testes")
        if tool not in READ_TOOLS | WRITE_TOOLS:
            raise ValueError(f"unknown MCP tool {tool}")
        with self._lock:
            for attempt in (1, 2):  # retry once with a fresh session (expired Mcp-Session-Id)
                try:
                    result = self._ensure_session().rpc("tools/call", {"name": tool, "arguments": args})
                    break
                except SystemExit as e:
                    self._session = None
                    if attempt == 2:
                        raise McpError(f"{tool}: {e}") from None
        data = _parse(result)
        if result.get("isError"):
            raise McpError(f"{tool}: {json.dumps(data, ensure_ascii=False)[:300]}")
        return data


def _parse(result: dict) -> Any:
    texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
    if not texts:
        return result.get("structuredContent")
    parsed = []
    for t in texts:
        try:
            parsed.append(json.loads(t))
        except ValueError:
            parsed.append(t)
    return parsed[0] if len(parsed) == 1 else parsed
