"""Perception loop: poll the source, normalise, diff -> events, update shared memory.

No LLM is called here. Agents subscribe to the bus and only wake on relevant events or on
the slow "tick" event.
"""

import logging
import threading
import time

from ..config import Settings
from ..memory import EventBus, SharedMemory
from .sources import Source
from .state import Event, HomeState, TemperatureTrend, diff, normalize

log = logging.getLogger(__name__)


class Perception:
    def __init__(self, source: Source, bus: EventBus, memory: SharedMemory, settings: Settings):
        self.source, self.bus, self.memory, self.s = source, bus, memory, settings
        self.prev: HomeState | None = None
        self.trend = TemperatureTrend(settings)
        self.last_tick = 0.0
        self.failures = 0

    def step(self, now: float | None = None) -> list[Event]:
        now = time.time() if now is None else now
        raw = self.source.fetch()
        state = normalize(raw, self.s, now)
        events = diff(self.prev, state, self.s) + self.trend.update(state)
        if self.prev is not None and now - self.last_tick >= self.s.tick_interval_s:
            events.append(Event(type="tick", ts=now, message="Verificação periódica.",
                                data=state.summary()))
            self.last_tick = now
        elif self.prev is None:
            self.last_tick = now
        self.prev = state
        self.memory.update_state(state)
        if events:
            self.memory.add_events(events)
            self.bus.publish(events)
        return events

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        log.info("perception started (source=%s, every %.1fs)", self.source.name, self.s.poll_interval_s)
        while not stop.is_set():
            try:
                self.step()
                self.failures = 0
                delay = self.s.poll_interval_s
            except Exception as e:  # keep running on transient MCP errors, with backoff
                self.failures += 1
                delay = min(60.0, self.s.poll_interval_s * 2 ** min(self.failures, 4))
                log.warning("poll failed (%d): %s; retrying in %.0fs", self.failures, e, delay)
            stop.wait(delay)

    def start_thread(self) -> tuple[threading.Thread, threading.Event]:
        stop = threading.Event()
        t = threading.Thread(target=self.run, args=(stop,), name="perception", daemon=True)
        t.start()
        return t, stop
