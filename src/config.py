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
    sim_rooms: bool  # add the simulated rooms of home_config_file to the real house (off: real data only)
    # history (SQLite, var/home.db): readings every store_interval_s, events, decisions, preferences
    history_db: Path
    legacy_history_db: Path  # old var/history.db, imported once into history_db
    store_interval_s: float
    # the house in 3D: where rooms and devices are drawn (versioned)
    layout_file: Path
    # LLM (OpenAI-compatible endpoint) and the A2A agents
    llm_base_url: str
    llm_model: str
    llm_api_key: str  # local servers ignore it, but the OpenAI client wants one
    llm_reasoning_effort: str  # the Coordinator (chat + agents) always thinks: "high" (default) | "medium" | "low" | "none" | "" = not sent
    chat_llm: bool  # the chat classifies messages with the LLM (false = PT-PT rules only)
    chat_timeout_s: float
    chat_reasoning_effort: str  # every chat message is read by the LLM thinking it over (default "high"); "" = fast reading only
    insight_min_days: int  # a pattern must be seen on at least this many days (demo: 2)
    insight_max_per_day: int
    insight_every_s: float
    agents_enabled: bool  # the API process sends events to the agents over A2A
    agents_host: str
    agents_base_port: int  # Segurança = base+1, Energia = base+2, Conforto = base+3
    agent_timeout_s: float
    agent_cooldown_s: float  # same event type + room is not sent to the same agent again before this
    api_url: str  # where the agents' read-only tools find the API
    # local camera vision (frame capture + VLM inference)
    vision_enabled: bool
    camera_rtsp_url: str
    vision_base_url: str
    vision_model: str
    vision_api_key: str
    vision_timeout_s: float
    vision_reasoning_effort: str  # the camera model: "none" (fast, default); not the Coordinator
    vision_keep_alive: str  # Ollama keep_alive sent during startup warmup (e.g. "30m", "-1"); "" disables
    vision_warmup: bool  # best-effort non-blocking Ollama warmup on API startup
    vision_cooldown_s: float
    vision_stay_cooldown_s: float
    vision_trigger_disarmed: bool
    vision_sim_image: Path | None
    vision_max_frames: int
    vision_frames_dir: Path


@lru_cache
def get_settings() -> Settings:
    _load_env_file()
    simulate = _bool("SIMULATE", False)
    llm_base_url = os.environ.get("LLM_BASE_URL") or "http://localhost:1234/v1"
    llm_model = os.environ.get("LLM_MODEL") or "qwen3.8:latest"
    llm_api_key = os.environ.get("LLM_API_KEY") or "local"
    sim_image_raw = (os.environ.get("VISION_SIM_IMAGE") or "").strip()
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
        sim_rooms=_bool("SIM_ROOMS", False),
        # SIMULATE=true has its own file: simulated Sala/Quarto share the real room ids
        history_db=Path(os.environ.get("HISTORY_DB", str(VAR_DIR / ("home_sim.db" if simulate else "home.db")))),
        legacy_history_db=VAR_DIR / ("history_sim.db" if simulate else "history.db"),
        store_interval_s=_float("STORE_INTERVAL_S", 30.0),
        layout_file=Path(os.environ.get("LAYOUT_FILE", str(ROOT / "config" / "layout.json"))),
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_reasoning_effort=os.environ.get("LLM_REASONING_EFFORT", "high").strip(),
        chat_llm=_bool("CHAT_LLM", True),
        chat_timeout_s=_float("CHAT_TIMEOUT_S", 600.0),
        chat_reasoning_effort=os.environ.get("CHAT_REASONING_EFFORT", "high").strip(),
        insight_min_days=int(_float("INSIGHT_MIN_DAYS", 2)),
        insight_max_per_day=int(_float("INSIGHT_MAX_PER_DAY", 2)),
        insight_every_s=_float("INSIGHT_EVERY_S", 1800),
        agents_enabled=_bool("AGENTS", True),
        agents_host=os.environ.get("AGENTS_HOST") or "127.0.0.1",
        agents_base_port=int(_float("AGENTS_BASE_PORT", 9100)),
        agent_timeout_s=_float("AGENT_TIMEOUT_S", 600.0),
        agent_cooldown_s=_float("AGENT_COOLDOWN_S", 60.0),
        api_url=(os.environ.get("API_URL") or "http://127.0.0.1:8000").rstrip("/"),
        vision_enabled=_bool("VISION_ENABLED", True),
        camera_rtsp_url=(os.environ.get("CAMERA_RTSP_URL") or "").strip(),
        vision_base_url=(os.environ.get("VISION_BASE_URL") or "").strip() or llm_base_url,
        vision_model=(os.environ.get("VISION_MODEL") or "").strip() or llm_model,
        vision_api_key=(os.environ.get("VISION_API_KEY") or "").strip() or llm_api_key,
        vision_timeout_s=_float("VISION_TIMEOUT_S", 45.0),
        vision_reasoning_effort=os.environ.get("VISION_REASONING_EFFORT", "none").strip(),
        vision_keep_alive=os.environ.get("VISION_KEEP_ALIVE", "30m").strip(),
        vision_warmup=_bool("VISION_WARMUP", True),
        vision_cooldown_s=_float("VISION_COOLDOWN_S", 10.0),
        vision_stay_cooldown_s=_float("VISION_STAY_COOLDOWN_S", 30.0),
        vision_trigger_disarmed=_bool("VISION_TRIGGER_DISARMED", False),
        vision_sim_image=Path(sim_image_raw) if sim_image_raw else None,
        vision_max_frames=int(_float("VISION_MAX_FRAMES", 50)),
        vision_frames_dir=Path(os.environ.get("VISION_FRAMES_DIR") or str(VAR_DIR / "frames")),
    )
