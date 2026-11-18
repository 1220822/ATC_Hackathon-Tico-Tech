"""Async VisionWorker: room-aware trigger matrix, cooldown/single-flight, fallback and SSE-safe publishing."""

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable

from ..config import Settings
from ..memory import EventBus, SharedMemory
from ..perception.state import DeviceState, Event, HomeState
from .analyzer import (
    VisionAnalyzer,
    VisionInferenceError,
    VisionTimeoutError,
    make_analyzer,
)
from .capture import CaptureError, FrameSource, FrameStore, make_frame_source
from .models import VisionContext, VisionObservation, to_event

log = logging.getLogger(__name__)

VISION_TRIGGER_EVENTS = (
    "camera_motion",
    "camera_sound",
    "security_alarm",
    "tamper_alarm",
    "contact_opened",
    "smoke_detected",
)

PRIORITY_NORMAL = 1
PRIORITY_SMOKE = 2


def should_trigger_vision(
    event: Event,
    state: HomeState | None,
    settings: Settings,
) -> tuple[bool, VisionContext, DeviceState | None]:
    """Pure trigger policy matching docs/vision_architecture.md §4.1 (mode x event, room-aware)."""
    if not settings.vision_enabled or state is None:
        return False, "general", None

    # Room-aware camera resolution: only rooms with a camera (or house-level events without room)
    cam = state.by_role("camera", event.room) if event.room else state.by_role("camera")
    if cam is None:
        return False, "general", None

    t = event.type
    mode = state.security_status  # DISARMED | STAY | AWAY

    if t == "smoke_detected":
        return True, "smoke", cam
    if t in ("security_alarm", "tamper_alarm"):
        return True, "intrusion", cam
    if t == "contact_opened":
        return (True, "intrusion", cam) if mode in ("AWAY", "STAY") else (False, "general", None)
    if t == "camera_motion":
        if mode in ("AWAY", "STAY") or settings.vision_trigger_disarmed:
            return True, "intrusion", cam
        return False, "general", None
    if t == "camera_sound":
        return (True, "intrusion", cam) if mode == "AWAY" else (False, "general", None)
    return False, "general", None


@dataclass(frozen=True)
class _VisionJob:
    room_key: str
    room_id: str | None
    room_name: str
    camera_id: str | None
    trigger_type: str
    trigger_event_id: str | None
    context: VisionContext
    priority: int


class VisionWorker:
    """Dedicated background worker that enriches camera/security/smoke events with visual facts.

    Never blocks EventBus.publish(), never writes to MCP, and always publishes a camera_analysis
    event (including structured error/timeout fallbacks) in the exact order:
        memory.set_vision(obs) -> memory.add_events([ev]) -> bus.publish([ev])
    """

    def __init__(
        self,
        memory: SharedMemory,
        bus: EventBus,
        settings: Settings,
        source: FrameSource | None = None,
        analyzer: VisionAnalyzer | None = None,
        store: FrameStore | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.memory = memory
        self.bus = bus
        self.s = settings
        self._source = source if source is not None else make_frame_source(settings)
        self._analyzer = analyzer if analyzer is not None else make_analyzer(settings)
        self._store = store if store is not None else FrameStore(settings.vision_frames_dir, settings.vision_max_frames)
        self._clock = clock or time.monotonic

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._io_lock = threading.Lock()  # serialises source.capture + analyzer.analyze across worker & analyze_now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._queue: deque[_VisionJob] = deque()
        self._active_rooms: dict[str, int] = {}  # room_key -> priority (queued or in-flight)
        # Single slot per room for informational smoke enrichment arriving while a lower-priority job runs.
        # Note: the critical emergency response runs deterministically in Coordinator.emergency() via EventBus.
        self._pending_priority: dict[str, _VisionJob] = {}
        self._last_done: dict[str, tuple[float, int]] = {}  # room_key -> (monotonic_ts, priority)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "VisionWorker":
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="vision-worker", daemon=True)
            self._thread.start()
        log.info(
            "vision worker ativo (source=%s, model=%s)",
            self._source.name,
            getattr(self._analyzer, "model_name", ""),
        )
        return self

    def is_alive(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive() and not self._stop.is_set())

    def warmup_async(self) -> threading.Thread | None:
        """Best-effort background warmup to pre-load the Ollama model into VRAM with keep_alive."""
        fn = getattr(self._analyzer, "warmup", None)
        if not self.s.vision_warmup or not self.s.vision_keep_alive or not callable(fn):
            return None

        def _do():
            try:
                with self._io_lock:
                    if self._stop.is_set():
                        return
                    ok = fn()
                if ok:
                    log.info(
                        "warmup de visão concluído (%s, keep_alive=%s)",
                        getattr(self._analyzer, "model_name", ""),
                        self.s.vision_keep_alive,
                    )
                else:
                    log.debug("warmup de visão ignorado (servidor não-Ollama ou indisponível)")
            except Exception:  # noqa: BLE001
                log.debug("warmup de visão falhou", exc_info=True)

        t = threading.Thread(target=_do, name="vision-warmup", daemon=True)
        t.start()
        return t

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout_s)
        try:
            self._analyzer.close()
        except Exception:  # noqa: BLE001
            log.debug("erro ao fechar analyzer de visão", exc_info=True)

    # -- event bus interface (non-blocking, any thread) ----------------------
    def will_handle(self, event: Event, state: HomeState | None) -> bool:
        """True if this event matches the trigger policy (used by AgentBridge to decide deferral)."""
        ok, _, _ = should_trigger_vision(event, state, self.s)
        return ok

    def on_event(self, event: Event) -> None:
        state = self.memory.state
        ok, context, cam = should_trigger_vision(event, state, self.s)
        if not ok or cam is None or state is None:
            return

        room_key = cam.room_id or "__home__"
        priority = PRIORITY_SMOKE if event.type == "smoke_detected" else PRIORITY_NORMAL
        cooldown = (
            self.s.vision_stay_cooldown_s
            if event.type == "camera_motion" and state.security_status == "STAY"
            else self.s.vision_cooldown_s
        )
        job = _VisionJob(
            room_key=room_key,
            room_id=cam.room_id or None,
            room_name=cam.room_name or "",
            camera_id=cam.id,
            trigger_type=event.type,
            trigger_event_id=event.id,
            context=context,
            priority=priority,
        )

        now = self._clock()
        with self._cond:
            if self._stop.is_set():
                return
            active_prio = self._active_rooms.get(room_key)
            if active_prio is not None:
                # Single-flight per room; allow smoke_detected informational enrichment to queue in priority slot
                if priority > active_prio:
                    self._pending_priority[room_key] = job
                return
            last = self._last_done.get(room_key)
            if last is not None:
                last_ts, last_prio = last
                if (now - last_ts) < cooldown and priority <= last_prio:
                    return
            self._active_rooms[room_key] = priority
            self._queue.append(job)
            self._cond.notify()

    # -- synchronous on-demand analysis (POST /vision/analyze) ---------------
    def analyze_now(
        self,
        room: str | None = None,
        context: VisionContext = "general",
    ) -> VisionObservation:
        state = self.memory.state
        cam = None
        if state is not None:
            cam = state.by_role("camera", room) if room else state.by_role("camera")
            if cam is None and room is None:
                cam = state.by_role("camera")
        job = _VisionJob(
            room_key=(cam.room_id if cam else None) or "__home__",
            room_id=(cam.room_id if cam and cam.room_id else None),
            room_name=(cam.room_name if cam else (room or "")),
            camera_id=(cam.id if cam else None),
            trigger_type="manual",
            trigger_event_id=None,
            context=context,
            priority=PRIORITY_NORMAL,
        )
        return self._process_job(job)

    # -- worker loop ---------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stop.is_set():
                    self._cond.wait(timeout=0.5)
                if self._stop.is_set():
                    return
                job = self._queue.popleft()
            try:
                self._process_job(job)
            except Exception:  # noqa: BLE001 - worker thread must never die on a single bad job
                log.exception("erro inesperado no vision-worker (%s)", job.trigger_type)
            finally:
                done_ts = self._clock()
                with self._cond:
                    self._last_done[job.room_key] = (done_ts, job.priority)
                    pending = self._pending_priority.pop(job.room_key, None)
                    if pending is not None and not self._stop.is_set():
                        self._active_rooms[job.room_key] = pending.priority
                        self._queue.append(pending)
                    else:
                        self._active_rooms.pop(job.room_key, None)

    def _process_job(self, job: _VisionJob) -> VisionObservation:
        state = self.memory.state
        armed = bool(state and state.armed)
        security_status = state.security_status if state is not None else "DISARMED"
        obs_id = uuid.uuid4().hex[:10]
        model_name = getattr(self._analyzer, "model_name", "")

        base_meta = dict(
            id=obs_id,
            room_id=job.room_id,
            room_name=job.room_name,
            camera_id=job.camera_id,
            trigger_type=job.trigger_type,
            trigger_event_id=job.trigger_event_id,
            context=job.context,
            model=model_name,
        )

        with self._io_lock:
            obs = self._capture_and_analyze(job, obs_id, security_status, base_meta)

        # Invariant 4: set_vision -> add_events -> bus.publish (required for SSE /twin/stream)
        self.memory.set_vision(obs)
        ev = to_event(obs, armed=armed)
        self.memory.add_events([ev])
        self.bus.publish([ev])
        return obs

    def _capture_and_analyze(
        self,
        job: _VisionJob,
        obs_id: str,
        security_status: str,
        base_meta: dict,
    ) -> VisionObservation:
        try:
            image_bytes = self._source.capture(job.room_id)
        except CaptureError as e:
            log.warning("captura visual falhou (%s): %s", job.trigger_type, e)
            return VisionObservation(**base_meta, status="capture_error", error=str(e)[:250])
        except Exception as e:  # noqa: BLE001
            log.warning("erro inesperado na captura visual (%s): %s", job.trigger_type, e)
            return VisionObservation(**base_meta, status="capture_error", error=str(e)[:250])

        frame_path: str | None = None
        try:
            frame_path = self._store.save(image_bytes, room_name=job.room_name, obs_id=obs_id)
        except Exception as e:  # noqa: BLE001 - still run inference even if snapshot storage fails
            log.warning("falha ao guardar snapshot em disco: %s", e)

        t0 = time.perf_counter()
        try:
            payload = self._analyzer.analyze(
                image_bytes,
                context=job.context,
                room_name=job.room_name,
                security_status=security_status,
                trigger_type=job.trigger_type,
            )
            latency_ms = int(round((time.perf_counter() - t0) * 1000))
            return VisionObservation(
                **base_meta,
                status="ok",
                frame_path=frame_path,
                latency_ms=latency_ms,
                has_person=payload.has_person,
                person_count=payload.person_count,
                category=payload.category,
                window_or_door_open=payload.window_or_door_open,
                smoke_or_fire_visible=payload.smoke_or_fire_visible,
                objects=payload.objects,
                overall_confidence=payload.overall_confidence,
                scene_note_pt=payload.scene_note_pt,
            )
        except VisionTimeoutError as e:
            latency_ms = int(round((time.perf_counter() - t0) * 1000))
            log.warning("inferência visual expirou (%s): %s", job.trigger_type, e)
            return VisionObservation(
                **base_meta, status="timeout", frame_path=frame_path, latency_ms=latency_ms, error=str(e)[:250]
            )
        except (VisionInferenceError, Exception) as e:  # noqa: BLE001
            latency_ms = int(round((time.perf_counter() - t0) * 1000))
            log.warning("inferência visual falhou (%s): %s", job.trigger_type, e)
            return VisionObservation(
                **base_meta, status="inference_error", frame_path=frame_path, latency_ms=latency_ms, error=str(e)[:250]
            )

