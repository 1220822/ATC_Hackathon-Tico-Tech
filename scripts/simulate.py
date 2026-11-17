"""Inject events into the simulated home (SIMULATE=true). Run the app/perception in another
terminal; both share var/sim_home.json.

    python scripts/simulate.py reset                 # back to the seed (everything normal)
    python scripts/simulate.py show                  # print the simulated state
    python scripts/simulate.py open | close          # contact sensor (window/door)
    python scripts/simulate.py temp 19.5             # temperature in C
    python scripts/simulate.py humidity 70
    python scripts/simulate.py smoke | cancel        # smoke alarm / cancel alarm
    python scripts/simulate.py arm stay|away|disarm  # SIMULATED security mode only
    python scripts/simulate.py motion                # camera motion
    python scripts/simulate.py plug on|off [--w 1500]
    python scripts/simulate.py offline|online light  # device type: light, plug, contact, ...

Demo scenarios (timed sequences, --step seconds between steps, default 4):
    python scripts/simulate.py cenario janela        # 1. window open while heating
    python scripts/simulate.py cenario fumo          # 2. smoke
    python scripts/simulate.py cenario objetivo      # 3. preconditions for the NL goal
    python scripts/simulate.py cenario intrusao      # 4. armed + contact open
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_settings  # noqa: E402
from src.perception.state import normalize  # noqa: E402
from src.sim.home import SimHome  # noqa: E402

TYPES = {"light": "bulb", "plug": "plug", "contact": "contactSensor", "smoke": "smokeSensor",
         "temperature": "temperatureSensor", "hub": "gateway", "camera": "camera"}


def say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def scenario(home: SimHome, name: str, step: float) -> None:
    if name == "janela":
        say("Pré-condições: janela fechada, 21.5 °C, aquecedor ligado")
        home.set_contact(False)
        home.set_temperature(21.5)
        home.set_plug(True)
        time.sleep(step)
        say("Janela ABERTA")
        home.set_contact(True)
        t = 21.5
        for _ in range(6):
            time.sleep(step)
            t -= 0.2
            home.set_temperature(t)
            say(f"Temperatura {t:.1f} °C")
    elif name == "fumo":
        say("Botão de teste do detetor de fumo premido")
        home.smoke()
    elif name == "objetivo":
        say("Pré-condições: 19.0 °C, aquecedor desligado, janela fechada")
        home.set_contact(False)
        home.set_plug(False)
        home.set_temperature(19.0)
        say('Agora envia o objetivo, ex.: "quero o quarto a 21 °C a gastar pouco esta noite"')
    elif name == "intrusao":
        say("Casa armada (AWAY) [simulado]")
        home.set_contact(False)
        home.set_security("away")
        time.sleep(step)
        say("Contacto ABERTO com a casa armada")
        home.set_contact(True)
        time.sleep(step / 2)
        say("Movimento na câmara")
        home.camera(motion=True)
    else:
        raise SystemExit(f"cenário desconhecido: {name}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd")
    p.add_argument("arg", nargs="?")
    p.add_argument("--w", type=float, default=None, help="plug power in W")
    p.add_argument("--step", type=float, default=4.0, help="seconds between scenario steps")
    a = p.parse_args()

    home = SimHome(get_settings().sim_state_file)
    cmd = a.cmd
    if cmd == "reset":
        home.reset()
    elif cmd == "show":
        print(json.dumps(normalize(home.snapshot(), get_settings()).summary(), indent=2, ensure_ascii=False))
        return
    elif cmd in ("open", "close"):
        home.set_contact(cmd == "open")
    elif cmd == "temp":
        home.set_temperature(float(a.arg))
    elif cmd == "humidity":
        home.set_humidity(float(a.arg))
    elif cmd == "smoke":
        home.smoke()
    elif cmd == "cancel":
        home.cancel_alarm()
    elif cmd == "arm":
        home.set_security(a.arg or "away")
    elif cmd == "motion":
        home.camera(motion=True)
    elif cmd == "plug":
        home.set_plug(a.arg == "on", a.w)
    elif cmd in ("offline", "online"):
        home.set_device_online(TYPES[a.arg], cmd == "online")
    elif cmd == "cenario":
        scenario(home, a.arg, a.step)
        return
    else:
        raise SystemExit(__doc__)
    say(f"ok: {cmd} {a.arg or ''}".strip())


if __name__ == "__main__":
    main()
