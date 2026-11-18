"""Settings loaded from environment variables and the repo's .env (never hardcode secrets here)."""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
VAR_DIR = ROOT / "var"  # runtime files (simulated home, logs); gitignored


def _load_env_file(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "sim")


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class Settings(BaseModel):
    simulate: bool
    dry_run: bool
    poll_interval_s: float
    tick_interval_s: float
    offline_after_s: float
    # perception thresholds
    temp_change_c: float
    temp_drop_c: float
    temp_drop_window_s: float
    humidity_change_pct: float
    plug_power_change_w: float
    # alarm history entries whose alarmEventDetail.type is in this set are treated as smoke.
    # Hypothesis (2026-11-17): the smoke test shows up as type EMERGENCY with an empty device.
    smoke_alarm_types: frozenset[str]
    sim_state_file: Path
    # comfort target; config/home.json can override it per room
    setpoint_c: float
    # rooms, per-room parameters and simulated rooms (versioned, no secrets)
    home_config_file: Path
    # history (SQLite): numeric readings stored every store_interval_s
    history_db: Path
    store_interval_s: float


@lru_cache
def get_settings() -> Settings:
    _load_env_file()
    simulate = _bool("SIMULATE", False)
    return Settings(
        simulate=simulate,
        dry_run=_bool("DRY_RUN", True),
        poll_interval_s=_float("POLL_INTERVAL_S", 2.0 if simulate else 7.0),
        tick_interval_s=_float("TICK_INTERVAL_S", 90.0),
        offline_after_s=_float("OFFLINE_AFTER_S", 24 * 3600),
        temp_change_c=_float("TEMP_CHANGE_C", 0.3),
        temp_drop_c=_float("TEMP_DROP_C", 0.5),
        temp_drop_window_s=_float("TEMP_DROP_WINDOW_S", 600),
        humidity_change_pct=_float("HUMIDITY_CHANGE_PCT", 5.0),
        plug_power_change_w=_float("PLUG_POWER_CHANGE_W", 50.0),
        smoke_alarm_types=frozenset(
            t.strip().upper()
            for t in os.environ.get("SMOKE_ALARM_TYPES", "EMERGENCY,SMOKE,FIRE").split(",")
            if t.strip()
        ),
        sim_state_file=Path(os.environ.get("SIM_STATE_FILE", str(VAR_DIR / "sim_home.json"))),
        setpoint_c=_float("SETPOINT_C", 21.0),
        home_config_file=Path(os.environ.get("HOME_CONFIG_FILE", str(ROOT / "config" / "home.json"))),
        # SIMULATE=true has its own file: simulated Sala/Quarto share the real room ids
        history_db=Path(os.environ.get("HISTORY_DB", str(VAR_DIR / ("history_sim.db" if simulate else "history.db")))),
        store_interval_s=_float("STORE_INTERVAL_S", 30.0),
    )
