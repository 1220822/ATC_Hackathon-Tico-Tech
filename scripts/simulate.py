"""Inject events into the simulated home. Run the app/perception in another terminal; both share
var/sim_home.json. With SIMULATE=true every room is simulated; with SIMULATE=false only the
simulated rooms of config/home.json (none at the moment) are read from here.

    python scripts/simulate.py reset                 # back to the seed (everything normal)
    python scripts/simulate.py show                  # print the simulated state
    python scripts/simulate.py rooms                 # list rooms and their devices
    python scripts/simulate.py open | close          # contact sensor (window/door)
    python scripts/simulate.py temp 19.5             # temperature in C
    python scripts/simulate.py humidity 70
    python scripts/simulate.py smoke | cancel        # smoke alarm / cancel alarm
    python scripts/simulate.py arm stay|away|disarm  # SIMULATED security mode only
    python scripts/simulate.py motion                # camera motion
    python scripts/simulate.py plug on|off [--w 1500]
    python scripts/simulate.py offline|online light  # device type: light, plug, contact, ...

--room <id|nome> picks the room (e.g. --room sala, --room Quarto).
By default each command uses the first room that has the device type it needs.

Demo scenarios (timed sequences, --step seconds between steps, default 4):
    python scripts/simulate.py cenario janela        # 1. window open while heating
    python scripts/simulate.py cenario fumo          # 2. smoke
    python scripts/simulate.py cenario objetivo      # 3. preconditions for the NL goal
    python scripts/simulate.py cenario intrusao      # 4. armed + contact open
Without --room each sensor comes from the first room that has it (janela: Sala window, Quarto heater).
With --room, every sensor of the scenario is taken from that room.
The same scenarios run from the browser at http://127.0.0.1:8000/simulador (src/sim/scenarios.py).
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_settings  # noqa: E402
from src.home_config import load_home_config  # noqa: E402
from src.perception.sources import SimSource  # noqa: E402
from src.perception.state import normalize  # noqa: E402
from src.sim.home import TYPE_BY_ROLE  # noqa: E402
from src.sim.scenarios import run_scenario  # noqa: E402


def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd")
    p.add_argument("arg", nargs="?")
    p.add_argument("--room", default=None, help="room id or name (default: first room with the device)")
    p.add_argument("--w", type=float, default=None, help="plug power in W")
    p.add_argument("--step", type=float, default=4.0, help="seconds between scenario steps")
    a = p.parse_args()

    settings = get_settings()
    source = SimSource(settings)
    home, room = source.home, a.room
    cmd = a.cmd
    dev = None
    try:
        if cmd == "reset":
            home.reset()
        elif cmd == "show":
            print(json.dumps(normalize(source.fetch(), settings).summary(), indent=2, ensure_ascii=False))
            return
        elif cmd == "rooms":
            for r in home.snapshot()["home"]["rooms"]:
                tag = " [simulado]" if r["id"] in source.sim_rooms else ""
                print(f"{r['id']}  {r['name']}{tag}: " + ", ".join(d["name"] for d in r["devices"]))
            return
        elif cmd in ("open", "close"):
            dev = home.set_contact(cmd == "open", room)
        elif cmd == "temp":
            dev = home.set_temperature(float(a.arg), room)
        elif cmd == "humidity":
            dev = home.set_humidity(float(a.arg), room)
        elif cmd == "smoke":
            dev = home.smoke(room)
        elif cmd == "cancel":
            home.cancel_alarm()
        elif cmd == "arm":
            home.set_security(a.arg or "away")
        elif cmd == "motion":
            dev = home.camera(motion=True, room=room)
        elif cmd == "plug":
            dev = home.set_plug(a.arg == "on", a.w, room)
        elif cmd in ("offline", "online"):
            dev = home.set_device_online(TYPE_BY_ROLE[a.arg], cmd == "online", room)
        elif cmd == "cenario":
            run_scenario(home, a.arg, a.step, room, say=say)
            return
        else:
            raise SystemExit(__doc__)
    except KeyError as e:
        raise SystemExit(f"erro: {e.args[0]}") from None
    where = f" ({dev['roomName']})" if dev else ""
    say(f"ok: {cmd} {a.arg or ''}".strip() + where)
    sim_ids = set(load_home_config(settings.home_config_file).sim_room_ids)
    if dev and not settings.simulate and not dev["id"].startswith(tuple(f"{i}-" for i in sim_ids)):
        say("aviso: SIMULATE=false, por isso só as divisões simuladas do config/home.json são lidas daqui; "
            "usa --room com uma delas (ver 'rooms')")


if __name__ == "__main__":
    main()
