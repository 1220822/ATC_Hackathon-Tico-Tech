"""Rules in natural language -> MCP scenario (post_new_scenario) + local watcher for "avisa-me".

build_rule(RuleRequest, state, existing_names) -> (RuleValue dict, description PT-PT) or RuleProblem.
The scenario uses the trigger/action format confirmed on the real account
(docs/capabilities/probes/test2_scenario_create_response.json):
    {"type": "device", "device": {"name": "", "id": ...},
     "capabilities": [{"name": "", "type": "sensorState", "operator": "=", "value": "alarm"}]}

The MCP cannot write in our chat, so "avisa-me" is done here: RuleWatcher checks the active rules on
every perception event and posts a chat message when a rule's conditions become true. Device actions
are left to the MCP scenario (so nothing is done twice). A rule that only notifies still needs a
device action in the MCP: the light turns amber, and the confirmation message says so.
"""

import re
import time
from typing import Callable

from ..perception.state import HomeState, _num, de, em
from .intents import COLOURS, RuleRequest, norm

AMBER = (255, 140, 0)
COLOUR_PT = {"vermelh": "vermelho", "verde": "verde", "azul": "azul", "ambar": "âmbar", "laranja": "laranja",
             "amarel": "amarelo", "branc": "branco", "roxo": "roxo"}


class RuleProblem(ValueError):
    """The rule cannot be built with the devices that exist (message in PT-PT)."""


def _device(state: HomeState, role: str, room: str | None):
    if room:
        d = state.by_role(role, room)
        if d is not None:
            return d
    found = state.devices_by_role(role)
    return found[0] if found else None


def _colour(name: str | None) -> tuple[tuple[int, int, int], str]:
    key = norm(name or "")
    for k, rgb in COLOURS.items():
        if key.startswith(k) or k in key:
            return rgb, COLOUR_PT[k]
    return AMBER, "âmbar"


def _cap(type_: str, op: str, value) -> dict:
    return {"name": "", "type": type_, "operator": op, "value": value}


def build_rule(req: RuleRequest, state: HomeState, existing_names: set[str]) -> tuple[dict, str]:
    triggers, conds_pt, slug = [], [], []
    for c in req.condicoes:
        val = norm(str(c.valor)) if c.valor is not None else ""
        if c.sensor == "janela":
            d = _device(state, "contact", c.divisao)
            if d is None:
                raise RuleProblem("Não há nenhum sensor de janela na casa.")
            opened = not val.startswith("fech")
            triggers.append({"type": "device", "device": {"name": "", "id": d.id},
                             "capabilities": [_cap("sensorState", "=", "alarm" if opened else "normal")]})
            conds_pt.append(f"a janela {de(d.room_name)} {'abrir' if opened else 'fechar'}")
            slug.append("janela_" + ("aberta" if opened else "fechada"))
        elif c.sensor in ("temperatura", "humidade"):
            d = _device(state, "temperature", c.divisao)
            if d is None:
                raise RuleProblem("Não há nenhum sensor de temperatura na casa.")
            num = _num(c.valor)
            if num is None:
                raise RuleProblem(f"Falta o valor de {c.sensor} para a regra.")
            op = c.operador if c.operador in (">", "<") else ">"
            cap = "temperatureValue" if c.sensor == "temperatura" else "humidityValue"
            unit = "°C" if c.sensor == "temperatura" else "%"
            triggers.append({"type": "device", "device": {"name": "", "id": d.id}, "capabilities": [_cap(cap, op, num)]})
            conds_pt.append(f"a {c.sensor} {em(d.room_name)} {'passar' if op == '>' else 'descer abaixo'} de "
                            f"{num:g} {unit}".replace("descer abaixo de", "descer abaixo de"))
            slug.append(f"{c.sensor[:4]}_{'acima' if op == '>' else 'abaixo'}_{num:g}".replace(".", "_"))
        elif c.sensor in ("aquecedor", "luz"):
            d = _device(state, "plug" if c.sensor == "aquecedor" else "light", c.divisao)
            if d is None:
                raise RuleProblem(f"Não há {'tomada (aquecedor)' if c.sensor == 'aquecedor' else 'luz'} na casa.")
            on = not val.startswith("deslig")
            triggers.append({"type": "device", "device": {"name": "", "id": d.id},
                             "capabilities": [_cap("power", "=", "on" if on else "off")]})
            noun = "o aquecedor" if c.sensor == "aquecedor" else "a luz"
            conds_pt.append(f"{noun} {de(d.room_name)} estiver {'ligad' if on else 'desligad'}{'o' if c.sensor == 'aquecedor' else 'a'}")
            slug.append(f"{c.sensor}_{'ligado' if on else 'desligado'}")
    if not triggers:
        raise RuleProblem("Não percebi a condição da regra. Exemplo: «se a janela da sala abrir com o aquecedor ligado, avisa-me».")

    actions, acts_pt = [], []
    for a in req.acoes:
        d = _device(state, "light" if a.dispositivo == "luz" else "plug", a.divisao)
        if d is None:
            raise RuleProblem(f"Não há {a.dispositivo} na casa para esta ação.")
        if a.acao == "cor":
            rgb, cname = _colour(a.cor)
            actions.append({"type": "device", "device": {"name": "", "id": d.id},
                            "capabilities": [{"name": "", "type": "color_rgb", "redValue": rgb[0],
                                              "greenValue": rgb[1], "blueValue": rgb[2]}]})
            acts_pt.append(f"a luz {de(d.room_name)} fica {cname}")
        else:
            on = a.acao == "ligar"
            actions.append({"type": "device", "device": {"name": "", "id": d.id},
                            "capabilities": [{"name": "", "type": "power", "value": "on" if on else "off"}]})
            noun = "a luz" if a.dispositivo == "luz" else "o aquecedor"
            acts_pt.append(f"{'ligo' if on else 'desligo'} {noun} {de(d.room_name)}")
    notify = req.avisar or not actions
    if not actions:  # the MCP scenario needs an action: amber light as a visible warning
        light = _device(state, "light", None)
        if light is None:
            raise RuleProblem("Para avisar na casa preciso de uma luz, e não há nenhuma.")
        actions.append({"type": "device", "device": {"name": "", "id": light.id},
                        "capabilities": [{"name": "", "type": "color_rgb", "redValue": AMBER[0],
                                          "greenValue": AMBER[1], "blueValue": AMBER[2]}]})
        acts_pt.append(f"a luz {de(light.room_name)} fica âmbar como aviso")
    if notify:
        acts_pt.append("aviso-te aqui no chat")

    name = "TT_" + "_".join(slug)[:50]
    base, i = name, 2
    while name in existing_names:
        name = f"{base}_{i}"
        i += 1
    description = f"quando {' e '.join(conds_pt)}, {', '.join(acts_pt[:-1]) + ' e ' + acts_pt[-1] if len(acts_pt) > 1 else acts_pt[0]}"
    rule = {"name": name, "description": description[:300], "triggers": triggers, "actions": actions,
            "logic": "AND", "notify": notify}
    return rule, description


def scenario_payload(rule: dict, active: bool = True) -> dict:
    return {"name": rule["name"], "triggerLogic": rule.get("logic", "AND"), "isActive": active,
            "triggers": rule["triggers"], "startActions": rule["actions"]}


# ------------------------------------------------------------------ local watcher

def _holds(trigger: dict, state: HomeState) -> bool:
    d = state.devices.get((trigger.get("device") or {}).get("id"))
    if d is None:
        return False
    for cap in trigger.get("capabilities") or []:
        cur, want, op = d.caps.get(cap.get("type")), cap.get("value"), cap.get("operator", "=")
        if op in (">", "<"):
            a, b = _num(cur), _num(want)
            if a is None or b is None or not (a > b if op == ">" else a < b):
                return False
        elif str(cur) != str(want):
            return False
    return True


def rule_holds(rule: dict, state: HomeState) -> bool:
    checks = [_holds(t, state) for t in rule.get("triggers") or []]
    if not checks:
        return False
    return all(checks) if rule.get("logic", "AND") == "AND" else any(checks)


class RuleWatcher:
    """Bus subscriber: posts a chat message when an active notify rule becomes true."""

    def __init__(self, prefs, memory, post: Callable[[str], None]):
        self.prefs, self.memory, self.post = prefs, memory, post
        self._last: dict[str, bool] = {}
        self._started = time.time()

    def on_event(self, _event=None) -> list[str]:
        state = self.memory.state
        if state is None:
            return []
        fired = []
        for p in self.prefs.get_active(None, "rule"):
            v = p["value"]
            now = rule_holds(v, state)
            # a rule created while we run starts as "not holding": its first match must notify
            was = self._last.get(p["id"], False if p["ts"] >= self._started else None)
            self._last[p["id"]] = now
            if now and was is False and v.get("notify"):
                text = f"Regra {v['name']}: {re.sub(r'^quando ', '', v['description']).split(',')[0]}. Aconteceu agora."
                self.post(text)
                fired.append(p["id"])
        return fired
