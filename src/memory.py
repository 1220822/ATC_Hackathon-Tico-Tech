"""Shared memory (latest home state + recent events) and a tiny in-process event bus."""

import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, Field

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


class Decision(BaseModel):
    """A proposal or decision of an agent / the Coordinator, shown in the UI timeline.
    The Coordinator (not in the repo yet) records them with SharedMemory.add_decision()."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    ts: float = Field(default_factory=time.time)
    agent: str  # Coordenador | Energia | Conforto | Segurança | Emergência
    message: str  # PT-PT, e.g. "janela aberta com o aquecedor ligado: proponho desligar"
    device_id: str | None = None
    room: str | None = None
    room_name: str | None = None
    action: dict[str, Any] | None = None  # capability body, e.g. {"type": "power", "value": "off"}
    status: Literal["proposta", "informação", "aprovada", "rejeitada", "dry-run", "executada", "erro"] = "proposta"


class SharedMemory:
    """What agents read: the current state and the recent event history.

    Every state update bumps `version`; events and decisions get a sequence number, so readers
    (e.g. the /twin/stream SSE endpoint) can ask for what is new without polling the MCP."""

    def __init__(self, max_events: int = 500):
        self._lock = threading.Lock()
        self._state: HomeState | None = None
        self._version = 0
        self._seq = 0
        self._events: deque[tuple[int, Event]] = deque(maxlen=max_events)
        self._decisions: deque[tuple[int, Decision]] = deque(maxlen=max_events)
        self._agents: dict[str, dict] = {}  # A2A agents as seen by the Coordinator's bridge

    def update_state(self, state: HomeState | None) -> None:
        with self._lock:
            self._state = state
            self._version += 1

    def add_events(self, events: Iterable[Event]) -> None:
        with self._lock:
            for e in events:
                self._seq += 1
                self._events.append((self._seq, e))

    def add_decision(self, decision: Decision) -> Decision:
        with self._lock:
            self._seq += 1
            self._decisions.append((self._seq, decision))
        return decision

    @property
    def state(self) -> HomeState | None:
        with self._lock:
            return self._state

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def set_agent_status(self, key: str, status: dict) -> None:
        with self._lock:
            self._agents[key] = dict(status)

    def agent_status(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._agents.values()]

    def recent_events(self, n: int = 50) -> list[Event]:
        with self._lock:
            return [e for _, e in list(self._events)[-n:]]

    def recent_decisions(self, n: int = 50) -> list[Decision]:
        with self._lock:
            return [d for _, d in list(self._decisions)[-n:]]

    def events_after(self, seq: int) -> list[tuple[int, Event]]:
        with self._lock:
            return [(i, e) for i, e in self._events if i > seq]

    def decisions_after(self, seq: int) -> list[tuple[int, Decision]]:
        with self._lock:
            return [(i, d) for i, d in self._decisions if i > seq]
