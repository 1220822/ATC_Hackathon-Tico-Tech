"""Run the A2A agent servers:
    python -m src.agents                    (Segurança, Energia, Conforto)
    python -m src.agents --only energia     (just one)
    python -m src.agents --parent-pid 1234  (exit when that process ends; used by `src.api --agents`)
"""

import argparse
import asyncio
import logging
import os
import socket
import sys

from ..config import get_settings
from .server import serve
from .specs import AGENTS

log = logging.getLogger("src.agents")


def port_in_use(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def process_alive(pid: int) -> bool:
    if os.name == "nt":  # os.kill on Windows would terminate the process, so ask the kernel instead
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def _watch_parent(pid: int) -> None:
    while process_alive(pid):
        await asyncio.sleep(2)
    log.info("o processo %s terminou; a parar os agentes", pid)
    os._exit(0)


async def _main(keys: list[str], parent_pid: int | None) -> None:
    settings = get_settings()
    if parent_pid:
        asyncio.get_running_loop().create_task(_watch_parent(parent_pid))
    await serve(settings, keys)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Servidores A2A dos agentes")
    p.add_argument("--only", choices=list(AGENTS), action="append", help="só este agente (pode repetir)")
    p.add_argument("--parent-pid", type=int, default=None, help="terminar quando este processo terminar")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "a2a", "agent_framework"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = get_settings()
    keys = a.only or list(AGENTS)
    busy = [settings.agents_base_port + AGENTS[k].port_offset for k in keys
            if port_in_use(settings.agents_host, settings.agents_base_port + AGENTS[k].port_offset)]
    if busy:
        raise SystemExit(f"Porta(s) {', '.join(map(str, busy))} já em uso: provavelmente os agentes já estão "
                         f"a correr noutro terminal. Pára-os ou muda AGENTS_BASE_PORT.")
    try:
        asyncio.run(_main(keys, a.parent_pid))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
