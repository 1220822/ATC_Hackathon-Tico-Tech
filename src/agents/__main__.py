"""Run the A2A agent servers:
    python -m src.agents                    (Segurança, Energia, Conforto)
    python -m src.agents --only energia     (just one)
"""

import argparse
import asyncio
import logging
import sys

from ..config import get_settings
from .server import serve
from .specs import AGENTS


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Servidores A2A dos agentes")
    p.add_argument("--only", choices=list(AGENTS), action="append", help="só este agente (pode repetir)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "a2a", "agent_framework"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        asyncio.run(serve(get_settings(), a.only or list(AGENTS)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
