"""Shared memory (latest home state + recent events) and a tiny in-process event bus."""

import logging
import threading
from collections import deque
from typing import Callable, Iterable

from .perception.state import Event, HomeState

log = logging.getLogger(__name__)

Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self):
        self._subs: list[tuple[Subscriber, frozenset[str] | None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Subscriber, types: Iterable[str] | None = None) -> None:
        with self._lock:
            self._subs.append((callback, frozenset(types) if types else None))

    def publish(self, events: Iterable[Event]) -> None:
        with self._lock:
            subs = list(self._subs)
        for event in events:
            for callback, types in subs:
                if types is None or event.type in types:
                    try:
                        callback(event)
                    except Exception:
                        log.exception("subscriber failed on %s", event.type)


class SharedMemory:
    """What agents read: the current state and the recent event history."""

    def __init__(self, max_events: int = 500):
        self._lock = threading.Lock()
        self._state: HomeState | None = None
        self._events: deque[Event] = deque(maxlen=max_events)

    def update_state(self, state: HomeState) -> None:
        with self._lock:
            self._state = state

    def add_events(self, events: Iterable[Event]) -> None:
        with self._lock:
            self._events.extend(events)

    @property
    def state(self) -> HomeState | None:
        with self._lock:
            return self._state

    def recent_events(self, n: int = 50) -> list[Event]:
        with self._lock:
            return list(self._events)[-n:]
