"""Run only the perception layer and print events:
    python -m src.perception            (uses SIMULATE from .env)
    python -m src.perception --sim      (force the simulated home)
"""

import json
import logging
import os
import sys
import time

from ..config import get_settings
from ..memory import EventBus, SharedMemory
from .poller import Perception
from .sources import McpSource, SimSource

ICON = {"info": " ", "warning": "!", "critical": "X"}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if "--sim" in sys.argv:
        os.environ["SIMULATE"] = "true"
    settings = get_settings()
    source = SimSource(settings) if settings.simulate else McpSource()
    bus, memory = EventBus(), SharedMemory()
    bus.subscribe(lambda e: print(
        f"{time.strftime('%H:%M:%S', time.localtime(e.ts))} [{ICON[e.severity]}] {e.type:22} {e.message}",
        flush=True))
    perception = Perception(source, bus, memory, settings)
    perception.step()
    print("Estado inicial:", json.dumps(memory.state.summary(), ensure_ascii=False), flush=True)
    try:
        perception.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
