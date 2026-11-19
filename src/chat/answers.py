"""Answers to state questions, from real data only (current state + history). Deterministic PT-PT
templates: no LLM, so the numbers in the answer are exactly the numbers in the data."""

import time

from ..context import Memories, energy_summary
from ..perception.state import HomeState, _num, de, em, fmt


def _kwh(v: float | None) -> str:
    if v is None:
        return "sem dados"
    return f"{fmt(v, 3)} kWh" if v < 1 else f"{fmt(v, 2)} kWh"


def _since(mem: Memories | None, device_id: str, types: tuple[str, ...]) -> str:
    if mem is None:
        return ""
    found = mem.events.recent(1, device_id=device_id, types=types)
    if not found:
        return ""
    return f" desde as {time.strftime('%H:%M', time.localtime(found[0]['ts']))}"


def _rooms(state: HomeState, room: str | None, role: str) -> list:
    devs = state.devices_by_role(role, room) if room else state.devices_by_role(role)
    return devs or state.devices_by_role(role)


def _minmax(mem: Memories | None, room_id: str, metric: str, start: float, end: float) -> tuple | None:
    if mem is None:
        return None
    rows = mem.history.query(room_id, metric, start, end)
    if not rows:
        return None
    vals = [r["value"] for r in rows]
    return min(vals), max(vals)


def answer_state(topic: str, period: str, room: str | None, state: HomeState, mem: Memories | None,
                 now: float | None = None) -> str:
    now = time.time() if now is None else now
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    if topic == "janela":
        devs = _rooms(state, room, "contact")
        if not devs:
            return "Não há nenhum sensor de contacto (janela ou porta) na casa."
        parts = []
        for d in devs:
            noun = d.opening_noun
            if not d.online:
                parts.append(f"O sensor da {noun} {de(d.room_name)} não comunica, não sei.")
                continue
            opened = d.caps.get("sensorState") == "alarm"
            since = _since(mem, d.id, ("contact_opened",) if opened else ("contact_closed",))
            parts.append(f"A {noun} {de(d.room_name)} está {'aberta' if opened else 'fechada'}{since}.")
        if any(d.caps.get("sensorState") == "alarm" for d in devs) and state.plugs_on():
            plug = state.plugs_on()[0]
            parts.append(f"Atenção: o aquecedor {de(plug.room_name)} está ligado.")
        return " ".join(parts)

    if topic in ("temperatura", "humidade"):
        devs = _rooms(state, room, "temperature")
        if not devs:
            return "Não há nenhum sensor de temperatura na casa."
        metric, cap, unit = (("temperature", "temperatureValue", "°C") if topic == "temperatura"
                             else ("humidity", "humidityValue", "%"))
        parts = []
        for d in devs:
            v = _num(d.caps.get(cap))
            if v is None:
                continue
            if period in ("ontem", "hoje", "semana"):
                start, end = {"ontem": (midnight - 86400, midnight), "hoje": (midnight, now + 1),
                              "semana": (now - 7 * 86400, now + 1)}[period]
                mm = _minmax(mem, d.room_id, metric, start, end)
                label = {"ontem": "Ontem", "hoje": "Hoje", "semana": "Nos últimos 7 dias"}[period]
                if mm:
                    parts.append(f"{label}, {em(d.room_name)}, a {topic} variou entre {fmt(mm[0])} e {fmt(mm[1])} {unit}.")
                else:
                    parts.append(f"Não tenho histórico {em(d.room_name)} para esse período.")
                continue
            line = f"{'A temperatura' if topic == 'temperatura' else 'A humidade'} {em(d.room_name)} está em {fmt(v)} {unit}"
            mm = _minmax(mem, d.room_id, metric, now - 2 * 3600, now + 1)
            if mm and mm[1] - mm[0] >= (0.3 if topic == "temperatura" else 2):
                line += f" (nas últimas 2 h entre {fmt(mm[0])} e {fmt(mm[1])} {unit})"
            parts.append(line + ".")
            if topic == "temperatura" and mem is not None:
                target, _ = mem.prefs.setpoint(d.room_id)
                if target is not None:
                    parts.append(f"A tua temperatura alvo agora é {fmt(target)} °C.")
        return " ".join(parts) or "Não tenho leituras."

    if topic == "energia":
        e = energy_summary(state, mem.history if mem else None, now)
        plug = state.devices_by_role("plug")
        if period == "ontem":
            mcp = e.get("ontem_kwh_casa")
            txt = f"Ontem a casa gastou {_kwh(mcp)} (valor do MCP)."
            if e.get("ontem_kwh_medido"):
                txt += f" Pelas minhas medições da tomada: {_kwh(e['ontem_kwh_medido'])}."
            if mcp is not None and e.get("tarifa"):
                txt += f" A {fmt(e['tarifa'], 3)} €/kWh, cerca de {fmt(mcp * e['tarifa'], 2)} €."
            return txt
        if period == "semana" and mem is not None:
            wh = mem.history.energy_wh(None, now - 7 * 86400, now + 1)
            return f"Nos últimos 7 dias medi {_kwh(wh / 1000)} na tomada (só conta o tempo em que eu estava a gravar)."
        txt = f"Hoje a tomada já gastou {_kwh(e.get('hoje_kwh_tomadas'))} (contador do MCP)"
        if e.get("hoje_kwh_medido") is not None:
            txt += f"; pelas minhas medições, {_kwh(e['hoje_kwh_medido'])}"
        txt += f". Ontem a casa gastou {_kwh(e.get('ontem_kwh_casa'))}."
        if plug:
            w = _num(plug[0].caps.get("instantPower"))
            if w is not None:
                txt += f" Neste momento o aquecedor está a puxar {fmt(w, 0)} W."
        return txt

    if topic == "aquecedor":
        devs = _rooms(state, room, "plug")
        if not devs:
            return "Não há nenhuma tomada inteligente na casa."
        d = devs[0]
        on = d.caps.get("power") == "on"
        w = _num(d.caps.get("instantPower"))
        return (f"O aquecedor {de(d.room_name)} está {'ligado' if on else 'desligado'}"
                + (f", a {fmt(w, 0)} W" if on and w is not None else "") + f"{_since(mem, d.id, ('plug_switched',))}.")

    if topic == "luz":
        devs = _rooms(state, room, "light")
        if not devs:
            return "Não há nenhuma luz inteligente na casa."
        d = devs[0]
        if d.caps.get("power") != "on":
            return f"A luz {de(d.room_name)} está apagada."
        return f"A luz {de(d.room_name)} está acesa, com brilho {d.caps.get('brightness')} %."

    if topic == "seguranca":
        mode = {"DISARMED": "desarmada", "STAY": "armada (em casa)", "AWAY": "armada (ausente)"}.get(
            state.security_status, state.security_status)
        txt = f"A segurança está {mode}."
        if state.is_alarmed:
            txt += " A casa está em alarme!"
        return txt

    # geral
    parts = []
    for r in state.rooms.values():
        s = state.room_summary(r.id)
        bits = []
        if s.get("temperatura_c") is not None:
            bits.append(f"{fmt(s['temperatura_c'])} °C")
        if s.get("humidade_pct") is not None:
            bits.append(f"{fmt(s['humidade_pct'], 0)} % de humidade")
        for noun in ("janela", "porta"):
            if f"{noun}_aberta" in s:
                bits.append(f"{noun} {'aberta' if s[f'{noun}_aberta'] else 'fechada'}")
        if "aquecedor_ligado" in s:
            bits.append("aquecedor ligado" if s["aquecedor_ligado"] else "aquecedor desligado")
        if bits:
            parts.append(f"{r.name}: {', '.join(bits)}.")
    parts.append(answer_state("seguranca", "agora", None, state, mem, now))
    return " ".join(parts)
