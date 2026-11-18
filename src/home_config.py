"""Home configuration: config/home.json (versioned, no secrets), validated with Pydantic.

    {
      "rooms": {"Quarto": {"setpoint_c": 21.0}},          # per-room parameters, by id or name
      "simulated_rooms": [                                 # rooms that exist only in the simulator
        {"name": "Cozinha", "type": "kitchen", "devices": ["temperature", "contact", "plug"],
         "setpoint_c": 19.0, "plug_power_change_w": 100}
      ]
    }

Per-room parameters override the global ones from .env (Settings). A simulated room gets the
id "sim-<slug(name)>" and its devices "sim-<slug(name)>-<role>" (e.g. sim-cozinha-temperature),
so ids are stable across restarts. Real rooms are never created, renamed or moved from here.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .config import Settings

SimRole = Literal["temperature", "contact", "plug", "light", "smoke", "camera", "hub"]


def slug(text: str) -> str:
    text = (text.casefold().replace("á", "a").replace("à", "a").replace("â", "a").replace("ã", "a")
            .replace("é", "e").replace("ê", "e").replace("í", "i").replace("ó", "o").replace("ô", "o")
            .replace("õ", "o").replace("ú", "u").replace("ç", "c"))
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


class RoomParams(BaseModel):
    """Parameters that override Settings for one room (None = use .env)."""
    model_config = ConfigDict(extra="forbid")

    setpoint_c: float | None = Field(None, ge=5, le=30)
    temp_change_c: float | None = Field(None, gt=0)
    temp_drop_c: float | None = Field(None, gt=0)
    temp_drop_window_s: float | None = Field(None, gt=0)
    humidity_change_pct: float | None = Field(None, gt=0)
    plug_power_change_w: float | None = Field(None, gt=0)

    def overrides(self) -> dict:
        return {k: v for k, v in self.model_dump(include=set(RoomParams.model_fields)).items()
                if v is not None}


class SimRoom(RoomParams):
    name: str = Field(min_length=1)
    id: str = ""  # default "sim-" + slug(name)
    type: str = "other"
    devices: list[SimRole] = Field(min_length=1)
    initial_temperature_c: float = 20.0
    initial_humidity_pct: float = Field(60.0, ge=0, le=100)

    @model_validator(mode="after")
    def _check(self):
        if not self.id:
            self.id = f"sim-{slug(self.name)}"
        if not self.id.startswith("sim-"):
            raise ValueError(f"o id da divisão simulada {self.name!r} tem de começar por 'sim-'")
        if len(set(self.devices)) != len(self.devices):
            raise ValueError(f"tipo de dispositivo repetido na divisão simulada {self.name!r}")
        return self

    def device_id(self, role: str) -> str:
        return f"{self.id}-{role}"


class HomeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: str = ""
    rooms: dict[str, RoomParams] = Field(default_factory=dict)
    simulated_rooms: list[SimRoom] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self):
        for attr in ("id", "name"):
            values = [getattr(r, attr).casefold() for r in self.simulated_rooms]
            dup = {v for v in values if values.count(v) > 1}
            if dup:
                raise ValueError(f"divisões simuladas com {attr} repetido: {sorted(dup)}")
        return self

    @property
    def sim_room_ids(self) -> list[str]:
        return [r.id for r in self.simulated_rooms]

    def params_for(self, room_id: str | None, room_name: str | None = None) -> RoomParams:
        """Simulated-room parameters, then rooms[name], then rooms[id] (later wins)."""
        merged: dict = {}
        sim = next((r for r in self.simulated_rooms if r.id == room_id), None)
        if sim:
            merged.update(sim.overrides())
        by_key = {k.casefold(): v for k, v in self.rooms.items()}
        for key in (room_name, room_id):
            if key and key.casefold() in by_key:
                merged.update(by_key[key.casefold()].overrides())
        return RoomParams(**merged)


class HomeConfigError(ValueError):
    pass


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> HomeConfig:
    try:
        return HomeConfig.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError) as e:
        raise HomeConfigError(f"{path} inválido:\n{e}") from None


def load_home_config(path: Path | str | None) -> HomeConfig:
    """Config at `path` (re-read when the file changes); empty config if it does not exist."""
    if path is None:
        return HomeConfig()
    p = Path(path)
    if not p.exists():
        return HomeConfig()
    return _load(str(p), p.stat().st_mtime)


def room_settings(settings: Settings, room_id: str | None, room_name: str | None = None,
                  config: HomeConfig | None = None) -> Settings:
    """Settings with this room's overrides from config/home.json applied."""
    config = config if config is not None else load_home_config(settings.home_config_file)
    over = config.params_for(room_id, room_name).overrides()
    return settings.model_copy(update=over) if over else settings
