"""Demo scenarios of the simulated home: timed sequences of sensor changes.

Shared by scripts/simulate.py (terminal) and the /simulador page (src.sim.control), so a browser
agent can run the same case a person would run by hand. Each sensor of a scenario is taken from the
room that has it (by default the first one): "janela" opens the Sala window while the Quarto heater is on
and the Quarto temperature drops.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .home import SimHome


class Stopped(Exception):
    """The scenario was stopped half-way (button «Parar»)."""


@dataclass(frozen=True)
class ScenarioInfo:
    id: str
    titulo: str
    descricao: str
    dispositivos: tuple[str, ...]  # device types the house must have (SimHome types), each in some room
    global_: bool = False  # touches the security hub too (only when the whole house is simulated)


SCENARIOS: dict[str, ScenarioInfo] = {s.id: s for s in (
    ScenarioInfo("janela", "Janela aberta com o aquecedor ligado",
                 "Fecha a janela, põe 21,5 °C e liga o aquecedor; depois abre a janela e a temperatura desce "
                 "0,2 °C por passo (6 passos). A Energia deve propor desligar o aquecedor (caso da Dona Maria).",
                 ("contactSensor", "temperatureSensor", "plug")),
    ScenarioInfo("objetivo", "Preparar um objetivo em linguagem natural",
                 "Janela fechada, aquecedor desligado, 19,0 °C. Depois escreve no chat, por exemplo "
                 "«quero o quarto a 21 °C a gastar pouco esta noite».",
                 ("contactSensor", "temperatureSensor", "plug")),
    ScenarioInfo("fumo", "Fumo detetado",
                 "Carrega no botão de teste do detetor de fumo: a casa entra em alarme (caminho de emergência).",
                 ("smokeSensor",), global_=True),
    ScenarioInfo("intrusao", "Intrusão com a casa armada",
                 "Arma a casa (ausente), abre o contacto e há movimento na câmara.",
                 ("contactSensor", "camera"), global_=True),
)}


def run_scenario(home: SimHome, name: str, step: float = 4.0, room: str | None = None,
                 say: Callable[[str], None] = print, stop: threading.Event | None = None,
                 where: dict[str, str] | None = None) -> None:
    """Runs scenario `name` on `home`. `where` maps a device type to the room (id or name) of that sensor;
    otherwise `room` is used for every sensor; otherwise the first room that has it."""

    def at(dtype: str) -> str | None:
        return (where or {}).get(dtype, room)

    def wait(s: float) -> None:
        if stop is None:
            time.sleep(s)
        elif stop.wait(s):
            raise Stopped

    if name == "janela":
        say("Pré-condições: janela fechada, 21,5 °C, aquecedor ligado")
        home.set_contact(False, at("contactSensor"))
        home.set_temperature(21.5, at("temperatureSensor"))
        home.set_plug(True, room=at("plug"))
        wait(step)
        c = home.set_contact(True, at("contactSensor"))
        say(f"Janela ABERTA ({c['roomName']})")
        t = 21.5
        for _ in range(6):
            wait(step)
            t -= 0.2
            dev = home.set_temperature(t, at("temperatureSensor"))
            say(f"Temperatura {t:.1f} °C ({dev['roomName']})")
    elif name == "fumo":
        say("Botão de teste do detetor de fumo premido")
        home.smoke(at("smokeSensor"))
    elif name == "objetivo":
        say("Pré-condições: 19,0 °C, aquecedor desligado, janela fechada")
        home.set_contact(False, at("contactSensor"))
        home.set_plug(False, room=at("plug"))
        home.set_temperature(19.0, at("temperatureSensor"))
        say('Agora envia o objetivo, ex.: "quero o quarto a 21 °C a gastar pouco esta noite"')
    elif name == "intrusao":
        say("Casa armada (ausente) [simulado]")
        home.set_contact(False, at("contactSensor"))
        home.set_security("away")
        wait(step)
        say("Contacto ABERTO com a casa armada")
        home.set_contact(True, at("contactSensor"))
        wait(step / 2)
        say("Movimento na câmara")
        home.camera(motion=True, room=at("camera"))
    else:
        raise KeyError(f"cenário desconhecido: {name}")
