"""Local daily schedules (rules with a {"type": "time", "at": "HH:MM"} trigger).

The MCP timers answer HTTP 404 on this account (docs/capabilities/probes/README.md), so an accepted
"liga o aquecedor às 7h" is kept as a rule preference and run here, every day, through the
Coordinator (guard + execute, DRY_RUN respected). Revoking the rule stops it.
"""

import logging
import threading
import time
from datetime import datetime

from ..coordinator import RequestRefused, UserRequest
from ..writes import UnknownDevice

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, prefs, coordinator, chat=None, every_s: float = 20.0):
        self.prefs, self.coordinator, self.chat, self.every_s = prefs, coordinator, chat, every_s
        self._done: dict[str, str] = {}  # rule id -> date it last ran

    def due(self, now: datetime) -> list[dict]:
        out = []
        for p in self.prefs.get_active(None, "rule"):
            for t in p["value"].get("triggers") or []:
                if t.get("type") != "time":
                    continue
                h, m = (int(x) for x in t["at"].split(":"))
                at = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if 0 <= (now - at).total_seconds() < 300 and self._done.get(p["id"]) != now.date().isoformat():
                    out.append(p)
        return out

    def step(self, now: datetime | None = None) -> int:
        now = now or datetime.now()
        n = 0
        for p in self.due(now):
            self._done[p["id"]] = now.date().isoformat()
            for a in p["value"].get("actions") or []:
                dev_id = (a.get("device") or {}).get("id")
                value = next((c.get("value") for c in a.get("capabilities") or [] if c.get("type") == "power"), None)
                if not dev_id or value not in ("on", "off"):
                    continue
                try:
                    d = self.coordinator.request(UserRequest(device_id=dev_id, action=value, origin="horário"))
                    n += 1
                    if self.chat is not None:
                        self.chat.say(f"Horário {p['value']['name']}: {d.message}", kind="rule")
                except (UnknownDevice, RequestRefused) as e:
                    log.warning("schedule %s failed: %s", p["value"]["name"], e)
        return n

    def start_thread(self) -> threading.Thread:
        def loop():
            while True:
                try:
                    self.step()
                except Exception:  # noqa: BLE001
                    log.exception("scheduler step failed")
                time.sleep(self.every_s)
        t = threading.Thread(target=loop, name="scheduler", daemon=True)
        t.start()
        return t
