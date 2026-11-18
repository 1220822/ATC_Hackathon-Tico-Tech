"""Standalone recorder: perception + history only (no agents, no API, no writes to the house).
Meant to run all night on a laptop so the proactive suggestions have days of real data.

    python -m src.storage.recorder            # real MCP (read-only), var/home.db
    python -m src.storage.recorder --status   # what is in the database, then exit
    python -m src.storage.recorder --once     # one poll + one write, then exit (smoke test)

- Readings every STORE_INTERVAL_S (30 s); every event of the bus goes to the events table.
- MCP errors: exponential backoff up to 5 min, never exits; a status line every 10 min.
- Only real data: SIM_ROOMS is forced off (SIMULATE=true still works, into var/home_sim.db).
- Log: var/recorder.log (rotating, 3 x 2 MB) and the console.
- Leaves the API's own recorder in standby while it runs (writer lease in var/home.db).
"""

import argparse
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from ..config import VAR_DIR, get_settings
from ..memory import EventBus, SharedMemory
from ..perception.poller import Perception
from ..perception.sources import make_source
from .history import History, HistoryRecorder, import_legacy

log = logging.getLogger("recorder")
STATUS_EVERY_S = 600
MAX_BACKOFF_S = 300


def _setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(VAR_DIR / "recorder.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.handlers[:] = [console, file]
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def print_status(history: History) -> None:
    rows = history.summary()
    if not rows:
        print(f"{history.path}: sem leituras.")
        return
    print(f"{history.path}")
    for r in rows:
        first = time.strftime("%d/%m %H:%M", time.localtime(r["first"]))
        last = time.strftime("%d/%m %H:%M:%S", time.localtime(r["last"]))
        print(f"  {r['room'][:8]:8} {r['metric']:22} {r['n']:7d}  {first} → {last}")
    w = history.writer()
    if w:
        print(f"  gravador: {w['owner']} (última gravação há {time.time() - w['ts']:.0f} s)")
    try:
        from .events import EventStore
        n = EventStore(history.path).count()
        print(f"  eventos: {n}")
    except Exception:  # noqa: BLE001 - events table may not exist yet
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Perceção + gravação do histórico (só leitura do MCP)")
    p.add_argument("--status", action="store_true", help="mostra o que está gravado e sai")
    p.add_argument("--once", action="store_true", help="uma leitura e uma gravação, e sai")
    a = p.parse_args(argv)
    _setup_logging()
    os.environ["SIM_ROOMS"] = "false"  # only real data
    get_settings.cache_clear()
    settings = get_settings()
    history = History(settings.history_db)
    if a.status:
        print_status(history)
        return 0
    import_legacy(history, settings.legacy_history_db)

    bus, memory = EventBus(), SharedMemory()
    event_store = None
    try:
        from .events import EventStore
        event_store = EventStore(settings.history_db)
        bus.subscribe(event_store.on_event)
    except ImportError:
        pass
    bus.subscribe(lambda e: log.info("evento %s: %s", e.type, e.message))
    perception = Perception(make_source(settings), bus, memory, settings)
    recorder = HistoryRecorder(memory, history, settings.store_interval_s, owner=f"recorder:{os.getpid()}")
    log.info("gravador: fonte=%s, perceção a cada %.0f s, leituras a cada %.0f s em %s",
             perception.source.name, settings.poll_interval_s, settings.store_interval_s, history.path)

    stop = threading.Event()
    failures = 0
    last_store = last_status = 0.0
    while not stop.is_set():
        try:
            perception.step()
            if failures:
                log.info("MCP voltou a responder após %d falhas", failures)
            failures = 0
            delay = settings.poll_interval_s
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001 - MCP down, network, auth: keep going
            failures += 1
            delay = min(MAX_BACKOFF_S, settings.poll_interval_s * 2 ** min(failures, 6))
            log.warning("leitura falhou (%d): %s; nova tentativa em %.0f s", failures, e, delay)
        now = time.time()
        if memory.state is not None and (a.once or now - last_store >= settings.store_interval_s):
            try:
                n = recorder.step()
                last_store = now
                if a.once:
                    log.info("gravadas %d leituras", n)
            except Exception as e:  # noqa: BLE001
                log.warning("gravação falhou: %s", e)
        if a.once:
            break
        if now - last_status >= STATUS_EVERY_S and memory.state is not None:
            st = memory.state
            log.info("estado: %s · %d leituras gravadas nesta sessão%s",
                     ", ".join(f"{k}={v}" for k, v in {
                         "temp": st.temperature, "hum": st.humidity, "janela": st.contact_open,
                         "tomada": st.plug_on, "W": st.plug_power_w}.items()),
                     recorder.rows_written, " (em espera: outro gravador ativo)" if recorder.standby else "")
            last_status = now
        try:
            stop.wait(delay)
        except KeyboardInterrupt:
            break
    if a.once:
        print_status(history)
    log.info("gravador parado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
