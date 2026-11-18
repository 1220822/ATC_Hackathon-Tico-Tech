"""Validated values of each preference key (Pydantic). PrefStore.set() refuses anything else.

    setpoint_c        {"c": 21, "periods": [{"start": "07:00", "end": "09:00", "c": 21}]}
                      c = all day (optional); periods = time windows (optional, may cross midnight)
    comfort_range_c   {"min_c": 19, "max_c": 23}
    humidity_max_pct  {"pct": 65}
    autonomy          {"category": "energia" | "conforto" | "seguranca" | "luz", "level": "auto" | "perguntar" | "nunca"}
    quiet_hours       {"start": "23:00", "end": "07:30"}
    rule              {"name": "TT_...", "description": "...", "triggers": [...], "actions": [...], "logic": "AND"}
                      (the MCP scenario id is in the mcp_scenario_id column)
    layout            {"description": "...", "change": {...}, "before": {...}, "after": {...}}
    suggestion_muted  {"insight_key": "...", "description": "..."}
    note              {"text": "..."}
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Category = Literal["energia", "conforto", "seguranca", "luz"]
Level = Literal["auto", "perguntar", "nunca"]
CATEGORIES: tuple[str, ...] = ("energia", "conforto", "seguranca", "luz")
LEVELS: tuple[str, ...] = ("auto", "perguntar", "nunca")
DEFAULT_LEVEL: Level = "perguntar"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _hhmm(v: str) -> str:
    v = str(v).strip()
    if re.fullmatch(r"\d{1,2}", v):
        v = f"{int(v):02d}:00"
    if re.fullmatch(r"\d:\d\d", v):
        v = "0" + v
    if not _HHMM.match(v):
        raise ValueError(f"hora inválida {v!r} (usa HH:MM)")
    return v


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(start: str, end: str, when: datetime) -> bool:
    """start <= when < end, windows may cross midnight (23:00-07:00)."""
    t, a, b = when.hour * 60 + when.minute, minutes(start), minutes(end)
    return a <= t < b if a < b else (t >= a or t < b)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Period(_Strict):
    start: str
    end: str
    c: float = Field(ge=10, le=30)

    @field_validator("start", "end", mode="before")
    @classmethod
    def _time(cls, v):
        return _hhmm(v)


class SetpointValue(_Strict):
    c: float | None = Field(None, ge=10, le=30)
    periods: list[Period] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _something(self):
        if self.c is None and not self.periods:
            raise ValueError("setpoint_c precisa de 'c' ou de 'periods'")
        return self

    def target(self, when: datetime) -> float | None:
        for p in self.periods:
            if in_window(p.start, p.end, when):
                return p.c
        return self.c


class ComfortRangeValue(_Strict):
    min_c: float = Field(ge=10, le=30)
    max_c: float = Field(ge=10, le=30)

    @model_validator(mode="after")
    def _order(self):
        if self.min_c >= self.max_c:
            raise ValueError("min_c tem de ser menor que max_c")
        return self


class HumidityMaxValue(_Strict):
    pct: float = Field(ge=30, le=95)


class AutonomyValue(_Strict):
    category: Category
    level: Level
    # learned autonomy applies to one kind of decision only (src.storage.decisions.decision_kind)
    kind: str | None = Field(None, max_length=200)
    description: str | None = Field(None, max_length=300)


class QuietHoursValue(_Strict):
    start: str
    end: str

    @field_validator("start", "end", mode="before")
    @classmethod
    def _time(cls, v):
        return _hhmm(v)

    def active(self, when: datetime) -> bool:
        return in_window(self.start, self.end, when)


class RuleValue(_Strict):
    name: str = Field(pattern=r"^TT_[\w\-À-ÿ ]{1,60}$")
    description: str = Field(min_length=1, max_length=300)
    triggers: list[dict] = Field(min_length=1, max_length=4)
    actions: list[dict] = Field(default_factory=list, max_length=4)
    logic: Literal["AND", "OR"] = "AND"
    notify: bool = False


class LayoutValue(_Strict):
    description: str = Field(min_length=1, max_length=300)
    change: dict = Field(default_factory=dict)
    before: dict = Field(default_factory=dict)
    after: dict = Field(default_factory=dict)


class SuggestionMutedValue(_Strict):
    insight_key: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=300)


class NoteValue(_Strict):
    text: str = Field(min_length=1, max_length=500)


VALUE_MODELS: dict[str, type[BaseModel]] = {
    "setpoint_c": SetpointValue,
    "comfort_range_c": ComfortRangeValue,
    "humidity_max_pct": HumidityMaxValue,
    "autonomy": AutonomyValue,
    "quiet_hours": QuietHoursValue,
    "rule": RuleValue,
    "layout": LayoutValue,
    "suggestion_muted": SuggestionMutedValue,
    "note": NoteValue,
}
KEYS = tuple(VALUE_MODELS)
Source = Literal["explicit", "learned_confirmed"]
# keys where several active rows may coexist (a new one never replaces an old one by itself)
MULTI_KEYS = frozenset({"rule", "suggestion_muted", "note"})

KEY_LABEL_PT = {
    "setpoint_c": "Temperatura alvo", "comfort_range_c": "Intervalo de conforto",
    "humidity_max_pct": "Humidade máxima", "autonomy": "Autonomia", "quiet_hours": "Horas de silêncio",
    "rule": "Regra", "layout": "Disposição 3D", "suggestion_muted": "Sugestão silenciada", "note": "Nota",
}
CATEGORY_PT = {"energia": "Energia", "conforto": "Conforto", "seguranca": "Segurança", "luz": "Luz"}
LEVEL_PT = {"auto": "faz sozinha", "perguntar": "pergunta primeiro", "nunca": "nunca faz (só informa)"}


def validate_scope(scope: str) -> str:
    if scope == "global" or re.fullmatch(r"(room|device):[\w\-]{1,80}", scope or ""):
        return scope
    raise ValueError(f"scope inválido {scope!r}: usa global, room:<id> ou device:<id>")


def validate_value(key: str, value: dict) -> dict:
    model = VALUE_MODELS.get(key)
    if model is None:
        raise ValueError(f"tipo de preferência desconhecido {key!r}; usa um de {list(KEYS)}")
    return model.model_validate(value).model_dump(exclude_none=True)


def identity(scope: str, key: str, value: dict) -> tuple | None:
    """Rows with the same identity replace each other (versioning). None = never auto-replaced."""
    if key in MULTI_KEYS:
        return None
    if key == "autonomy":
        return (scope, key, value.get("category"), value.get("kind"))
    return (scope, key)
