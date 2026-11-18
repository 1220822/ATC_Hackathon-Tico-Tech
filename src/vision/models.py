"""Immutable Pydantic data contracts and PT-PT formatting helpers for local camera vision."""

import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..perception.state import Event, Severity, de

VisionContext = Literal["intrusion", "smoke", "general"]
VisionStatus = Literal["ok", "capture_error", "inference_error", "timeout"]
VisionCategory = Literal["person", "pet", "smoke_or_fire", "empty", "other", "unclear"]

ALARM_TRIGGERS = frozenset({"security_alarm", "tamper_alarm"})


class VisionObservation(BaseModel):
    """Objective visual facts extracted from a single camera frame (immutable for thread-safety)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    ts: float = Field(default_factory=time.time)
    room_id: str | None = None
    room_name: str = ""
    camera_id: str | None = None
    # camera_motion | camera_sound | security_alarm | contact_opened | tamper_alarm | smoke_detected | manual
    trigger_type: str = "manual"
    trigger_event_id: str | None = None
    context: VisionContext = "general"
    status: VisionStatus = "ok"

    # Objective visual facts (populated when status == "ok")
    has_person: bool = False
    person_count: int = Field(default=0, ge=0)
    category: VisionCategory = "unclear"
    window_or_door_open: bool | None = None  # True=open, False=closed, None=not visible/unclear
    smoke_or_fire_visible: bool = False
    objects: tuple[str, ...] = ()
    # Self-assessed VLM confidence (0.0-1.0, uncalibrated; heuristic indicator for agents/UI)
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    scene_note_pt: str = ""  # short factual PT-PT description (~1 sentence, no recommendations)

    # Operational metadata
    frame_path: str | None = None  # filename or relative path under var/frames/
    error: str | None = None
    model: str = ""
    latency_ms: int = Field(default=0, ge=0)


def vision_severity(obs: VisionObservation, armed: bool) -> Severity:
    """Map a VisionObservation and house arming state to an Event severity."""
    if obs.smoke_or_fire_visible:
        return "critical"
    if obs.has_person and (armed or obs.trigger_type == "smoke_detected"):
        return "critical"
    if obs.trigger_type in ALARM_TRIGGERS:
        return "warning"
    if obs.status != "ok" and (armed or obs.trigger_type == "smoke_detected"):
        return "warning"
    if armed and (obs.window_or_door_open is True or obs.category == "unclear"):
        return "warning"
    return "info"


def format_vision_message(obs: VisionObservation) -> str:
    """Deterministic short PT-PT message for the UI timeline."""
    where = f" {de(obs.room_name)}" if obs.room_name else ""
    prefix = f"Análise visual{where}"
    if obs.status != "ok":
        reason = {
            "capture_error": "falha na captura do frame",
            "timeout": "tempo limite excedido na inferência",
            "inference_error": "falha na inferência visual",
        }.get(obs.status, obs.status)
        return f"{prefix}: {reason}."

    parts: list[str] = []
    if obs.smoke_or_fire_visible:
        parts.append("fumo ou chamas visíveis")
    if obs.has_person:
        n = max(obs.person_count, 1)
        parts.append("1 pessoa detetada" if n == 1 else f"{n} pessoas detetadas")
    elif obs.category == "pet":
        parts.append("animal detetado, sem pessoas")
    elif obs.category == "empty":
        parts.append("divisão vazia, sem pessoas")
    elif obs.category == "unclear":
        parts.append("imagem pouco conclusiva")
    elif not parts:
        parts.append("sem pessoas detetadas")

    if obs.window_or_door_open is True:
        parts.append("janela/porta aberta")
    elif obs.window_or_door_open is False and obs.context == "intrusion":
        parts.append("janela/porta fechada")

    conf = f" ({round(obs.overall_confidence * 100)}%)" if obs.overall_confidence > 0 else ""
    return f"{prefix}: {', '.join(parts)}{conf}."


def to_event(obs: VisionObservation, armed: bool) -> Event:
    """Convert a VisionObservation into a synthetic camera_analysis Event."""
    return Event(
        ts=obs.ts,
        type="camera_analysis",
        severity=vision_severity(obs, armed),
        device_id=obs.camera_id,
        role="camera",
        room=obs.room_id,
        room_name=obs.room_name or None,
        data=obs.model_dump(),
        message=format_vision_message(obs),
    )

