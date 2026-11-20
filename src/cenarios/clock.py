"""Simulated clock of the scenario mode: time.time() = real time + an offset.

Every timestamp of the app (events, decisions, chat, preferences, readings) comes from time.time(), so
moving the offset puts the whole app at the hour of the recording (19/11/2026 16:30, ...). The clock keeps
running in real time between two set() calls. time.monotonic() (timeouts, asyncio) is not touched.
"""

import threading
import time

DAY = (2026, 11, 19)  # day of the recordings


def at(hms: str, day: tuple[int, int, int] = DAY) -> float:
    """"16:30" / "16:30:40" of the recording day -> epoch seconds (local time)."""
    parts = [int(x) for x in hms.split(":")] + [0, 0]
    h, m, s = parts[:3]
    return time.mktime((*day, h, m, s, 0, 0, -1))


class SimClock:
    def __init__(self):
        self._real = time.time  # the real clock, kept before install() replaces time.time
        self._offset = 0.0
        self._lock = threading.Lock()
        self.installed = False

    def now(self) -> float:
        return self._real() + self._offset

    def set(self, epoch: float) -> None:
        with self._lock:
            self._offset = epoch - self._real()

    def install(self) -> "SimClock":
        """time.time() becomes this clock for the whole process (scenario mode only)."""
        time.time = self.now
        self.installed = True
        return self

    def uninstall(self) -> None:
        if self.installed:
            time.time = self._real
            self.installed = False
