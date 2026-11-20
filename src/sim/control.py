"""Control of the simulated home over HTTP: what scripts/simulate.py does in a terminal, for the
/simulador page (and so for a browser agent that cannot touch a physical sensor).

    GET  /sim                         rooms and devices it may change, scenarios, what is running, log
    POST /sim/acao     {acao, divisao?, valor?}
    POST /sim/cenario  {cenario, divisao?, passo_s?}   runs in the background; the page shows each step
    POST /sim/parar                                    stops the running scenario

With SIMULATE=true every room is simulated. With the real house (SIMULATE=false) only the simulated
rooms of config/home.json (SIM_ROOMS=true) can be changed here; the alarm and the security mode belong
to the real hub, so those actions are refused. Without any simulation everything is refused.
"""

import threading
import time
from collections import deque
from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings
from ..home_config import load_home_config
from .home import SimHome, _room_matches
from .scenarios import SCENARIOS, Stopped, run_scenario

ROLE_PT = {"contactSensor": "contacto", "temperatureSensor": "temperatura", "plug": "tomada",
           "smokeSensor": "fumo", "camera": "câmara", "bulb": "luz", "gateway": "central"}
GLOBAL_ACTIONS = {"fumo", "cancelar_alarme", "armar"}
NEEDS = {"abrir": "contactSensor", "fechar": "contactSensor", "temperatura": "temperatureSensor",
         "humidade": "temperatureSensor", "ligar_tomada": "plug", "desligar_tomada": "plug",
         "movimento": "camera", "fumo": "smokeSensor"}


class SimActionIn(BaseModel):
    acao: Literal["abrir", "fechar", "temperatura", "humidade", "ligar_tomada", "desligar_tomada", "movimento",
                  "fumo", "cancelar_alarme", "armar", "offline", "online", "repor"]
    divisao: str | None = Field(None, max_length=80)
    valor: str | float | None = None


class SimScenarioIn(BaseModel):
    cenario: str = Field(max_length=20)
    divisao: str | None = Field(None, max_length=80)
    passo_s: float = Field(4.0, ge=0.5, le=30)


class SimRefused(ValueError):
    pass


def _cap(dev: dict, cap: str):
    return next((c.get("value") for c in dev.get("capabilities") or [] if c["type"] == cap), None)


class SimControl:
    def __init__(self, settings: Settings):
        config = load_home_config(settings.home_config_file)
        self.whole_house = settings.simulate
        self.sim_ids = set(config.sim_room_ids)
        self.available = settings.simulate or (settings.sim_rooms and bool(self.sim_ids))
        self.home = SimHome(settings.sim_state_file, config.simulated_rooms)
        self.log: deque[dict] = deque(maxlen=60)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running: dict | None = None

    # -- what may be changed ------------------------------------------------------------------
    def _rooms(self, data: dict | None = None) -> list[dict]:
        rooms = (data or self.home.load())["home"].get("rooms") or []
        return rooms if self.whole_house else [r for r in rooms if r["id"] in self.sim_ids]

    def _check(self) -> None:
        if not self.available:
            raise SimRefused("Não há simulação nesta execução: arranca com --sim (tudo simulado) ou com "
                             "SIM_ROOMS=true para as divisões simuladas do config/home.json.")

    def _room(self, types: tuple[str, ...], key: str | None) -> dict:
        rooms = self._rooms()
        if key:
            found = [r for r in rooms if _room_matches(r, key)]
            if not found:
                extra = "" if self.whole_house else " (com a casa real só as divisões simuladas mudam aqui)"
                raise SimRefused(f"Divisão desconhecida ou não simulada: {key}{extra}.")
            room = found[0]
            have = {d["type"] for d in room.get("devices") or []}
            missing = [ROLE_PT.get(t, t) for t in types if t not in have]
            if missing:
                raise SimRefused(f"A divisão {room['name']} não tem: {', '.join(missing)}.")
            return room
        for room in rooms:
            if set(types) <= {d["type"] for d in room.get("devices") or []}:
                return room
        raise SimRefused("Nenhuma divisão simulada tem: " + ", ".join(ROLE_PT.get(t, t) for t in types) + ".")

    def _where(self, types: tuple[str, ...], key: str | None = None) -> dict[str, dict]:
        """For each device type, the room its sensor is taken from: the chosen room if it has it, else
        the first allowed room that has it."""
        rooms = self._rooms()
        if key:
            rooms = [r for r in rooms if _room_matches(r, key)] + rooms
        out = {}
        for t in types:
            room = next((r for r in rooms if t in {d["type"] for d in r.get("devices") or []}), None)
            if room is None:
                raise SimRefused(f"Nenhuma divisão simulada tem: {ROLE_PT.get(t, t)}.")
            out[t] = room
        return out

    def _global_ok(self, what: str) -> None:
        if not self.whole_house:
            raise SimRefused(f"{what}: com a casa real o alarme e a segurança são da central real.")

    def say(self, text: str) -> None:
        self.log.appendleft({"ts": time.time(), "texto": text})

    # -- view ----------------------------------------------------------------------------------
    def view(self) -> dict:
        if not self.available:
            return {"disponivel": False, "motivo": str(self._safe_check()), "divisoes": [], "cenarios": [],
                    "a_correr": None, "registo": []}
        data = self.home.load()
        sec = data["home"].get("security") or {}
        rooms = []
        for r in self._rooms(data):
            devs = []
            for d in r.get("devices") or []:
                if d["type"] == "gateway":
                    continue
                v = {"id": d["id"], "nome": d.get("name", ""), "tipo": d["type"],
                     "online": (d.get("lastCommunicationTime") or 0) > 1}
                if d["type"] == "contactSensor":
                    v["aberto"] = _cap(d, "sensorState") == "alarm"
                elif d["type"] == "temperatureSensor":
                    v["temperatura_c"] = _cap(d, "temperatureValue")
                    v["humidade_pct"] = _cap(d, "humidityValue")
                elif d["type"] == "plug":
                    v["ligada"] = _cap(d, "power") == "on"
                    v["potencia_w"] = _cap(d, "instantPower")
                elif d["type"] == "bulb":
                    v["ligada"] = _cap(d, "power") == "on"
                devs.append(v)
            rooms.append({"id": r["id"], "nome": r.get("name", ""), "simulada": r["id"] in self.sim_ids,
                          "dispositivos": devs})
        scen = []
        for s in SCENARIOS.values():
            try:
                where = self._where(s.dispositivos)
                onde, missing = [{"sensor": ROLE_PT.get(t, t), "divisao": r["name"]} for t, r in where.items()], ""
            except SimRefused as e:
                onde, missing = [], str(e)
            scen.append({"id": s.id, "titulo": s.titulo, "descricao": s.descricao, "onde": onde,
                         "disponivel": (self.whole_house or not s.global_) and not missing,
                         "motivo": missing or ("" if self.whole_house or not s.global_
                                               else "Precisa da casa toda simulada (--sim): o alarme é da central real.")})
        return {"disponivel": True, "casa_toda": self.whole_house,
                "seguranca": {"modo": sec.get("status"), "alarme": bool(sec.get("isAlarmed"))} if self.whole_house else None,
                "divisoes": rooms, "cenarios": scen, "a_correr": self.running, "registo": list(self.log)}

    def _safe_check(self) -> str:
        try:
            self._check()
        except SimRefused as e:
            return str(e)
        return ""

    # -- one-off actions ----------------------------------------------------------------------------
    def action(self, a: SimActionIn) -> dict:
        self._check()
        h, acao = self.home, a.acao
        if acao in GLOBAL_ACTIONS:
            self._global_ok({"fumo": "Fumo", "cancelar_alarme": "Cancelar o alarme", "armar": "Armar"}[acao])
        if acao == "repor":
            self.stop()
            h.reset()
            self.say("Simulador reposto no estado inicial")
            return self.view()
        if acao == "cancelar_alarme":
            h.cancel_alarm()
            self.say("Alarme cancelado")
            return self.view()
        if acao == "armar":
            mode = str(a.valor or "away").lower()
            if mode not in ("away", "stay", "disarm"):
                raise SimRefused("Modo de segurança: away, stay ou disarm.")
            h.set_security(mode)
            self.say({"away": "Casa armada (ausente)", "stay": "Casa armada (em casa)", "disarm": "Casa desarmada"}[mode])
            return self.view()
        if acao in ("offline", "online"):
            dtype = str(a.valor or "")
            if dtype not in ROLE_PT or dtype == "gateway":
                raise SimRefused("Indica o tipo de dispositivo em valor (ex.: plug, contactSensor).")
            room = self._room((dtype,), a.divisao)
            h.set_device_online(dtype, acao == "online", room["id"])
            self.say(f"{ROLE_PT[dtype].capitalize()} {'online' if acao == 'online' else 'offline'} · {room['name']}")
            return self.view()
        room = self._room((NEEDS[acao],), a.divisao)
        rid, name = room["id"], room["name"]
        if acao in ("abrir", "fechar"):
            h.set_contact(acao == "abrir", rid)
            self.say(f"Janela/porta {'ABERTA' if acao == 'abrir' else 'fechada'} · {name}")
        elif acao in ("temperatura", "humidade"):
            try:
                v = float(str(a.valor).replace(",", "."))
            except (TypeError, ValueError):
                raise SimRefused("Indica o valor (número).") from None
            lo, hi = (-10, 45) if acao == "temperatura" else (0, 100)
            if not lo <= v <= hi:
                raise SimRefused(f"Valor fora do intervalo ({lo} a {hi}).")
            if acao == "temperatura":
                h.set_temperature(v, rid)
                self.say(f"Temperatura {v:.1f} °C · {name}")
            else:
                h.set_humidity(v, rid)
                self.say(f"Humidade {v:.0f} % · {name}")
        elif acao in ("ligar_tomada", "desligar_tomada"):
            on = acao == "ligar_tomada"
            w = None
            if on and a.valor not in (None, ""):
                try:
                    w = float(str(a.valor).replace(",", "."))
                except ValueError:
                    raise SimRefused("Potência em W (número).") from None
            dev = h.set_plug(on, w, rid)
            self.say(f"Tomada {'ligada' if on else 'desligada'} ({_cap(dev, 'instantPower')} W) · {name}")
        elif acao == "movimento":
            h.camera(motion=True, room=rid)
            self.say(f"Movimento na câmara · {name}")
        elif acao == "fumo":
            h.smoke(rid)
            self.say(f"Fumo detetado · {name}")
        return self.view()

    # -- scenarios ------------------------------------------------------------------------------------
    def start(self, s: SimScenarioIn) -> dict:
        self._check()
        info = SCENARIOS.get(s.cenario)
        if info is None:
            raise SimRefused(f"Cenário desconhecido: {s.cenario}.")
        if info.global_:
            self._global_ok(info.titulo)
        where = self._where(info.dispositivos, s.divisao)
        rooms = list(dict.fromkeys(r["name"] for r in where.values()))
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise SimRefused("Já está um cenário a correr; para-o primeiro.")
            self._stop = threading.Event()
            self.running = {"cenario": info.id, "titulo": info.titulo, "divisao": " e ".join(rooms), "desde": time.time()}
            self._thread = threading.Thread(target=self._run, args=(info, where, s.passo_s, self._stop),
                                            name=f"sim-{info.id}", daemon=True)
            self._thread.start()
        return self.view()

    def _run(self, info, where: dict[str, dict], step: float, stop: threading.Event) -> None:
        self.say(f"Cenário «{info.titulo}» · " + ", ".join(f"{ROLE_PT.get(t, t)}: {r['name']}" for t, r in where.items()))
        try:
            run_scenario(self.home, info.id, step, say=self.say, stop=stop,
                         where={t: r["id"] for t, r in where.items()})
            self.say("Cenário terminado")
        except Stopped:
            self.say("Cenário parado")
        except Exception as e:  # noqa: BLE001 - shown on the page, never kills the server
            self.say(f"Erro no cenário: {e}")
        finally:
            with self._lock:
                self.running = None

    def stop(self) -> dict:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2)
        return self.view()


def add_sim_routes(app, settings: Settings) -> SimControl:
    from fastapi import HTTPException

    control = SimControl(settings)

    def guarded(fn, *args):
        try:
            return fn(*args)
        except (SimRefused, KeyError) as e:
            raise HTTPException(400, str(e.args[0] if e.args else e)) from None

    @app.get("/sim")
    def sim_view():
        return control.view()

    @app.post("/sim/acao")
    def sim_action(body: SimActionIn):
        return guarded(control.action, body)

    @app.post("/sim/cenario")
    def sim_scenario(body: SimScenarioIn):
        return guarded(control.start, body)

    @app.post("/sim/parar")
    def sim_stop():
        return control.stop()

    return control
