"""'O que a casa sabe de ti': everything the house knows about the user, editable and erasable (RGPD).

    view(state)               active preferences, rules (with the MCP scenario), muted suggestions, history
    edit(pref_id, text)       a new version of the preference (the old one stays in the history)
    revoke(pref_id)           deactivates it; a rule also deactivates its MCP scenario
    erase_all()               preferences, rules (scenarios deactivated), suggestions, chat, decisions

Sensor readings and events (the house's data) are not personal preferences and stay; the UI says so.
"""

import re
import time

from .context import describe_pref, scope_label
from .prefs.models import KEY_LABEL_PT, LEVELS


class KnowledgeError(ValueError):
    pass


def _row(p: dict, state) -> dict:
    return {"id": p["id"], "tipo": p["key"], "tipo_pt": KEY_LABEL_PT.get(p["key"], p["key"]),
            "descricao": describe_pref(p), "ambito": scope_label(p["scope"], state), "frase": p.get("original_text"),
            "origem": "aprendida com a tua confirmação" if p["source"] == "learned_confirmed" else "dita por ti",
            "data": time.strftime("%d/%m/%Y %H:%M", time.localtime(p["ts"])), "ativa": p["active"],
            "substituida_por": p.get("superseded_by"), "cenario_mcp": p.get("mcp_scenario_id"),
            "editavel": p["key"] in EDITABLE, "valor": p["value"]}


EDITABLE = {"setpoint_c": "temperatura em °C (ex.: 21)", "comfort_range_c": "intervalo em °C (ex.: 19-22)",
            "humidity_max_pct": "humidade máxima em % (ex.: 65)", "autonomy": "auto, perguntar ou nunca",
            "quiet_hours": "horas (ex.: 23:00-07:30)", "note": "texto da nota"}


class Knowledge:
    def __init__(self, memories, coordinator, chat=None, insights=None):
        self.mem, self.coordinator, self.chat, self.insights = memories, coordinator, chat, insights

    def view(self, state) -> dict:
        active = self.mem.prefs.get_active()
        prefs = [_row(p, state) for p in active if p["key"] not in ("rule", "layout", "suggestion_muted")]
        rules = []
        for p in active:
            if p["key"] != "rule":
                continue
            r = _row(p, state)
            local = any(t.get("type") == "time" for t in p["value"].get("triggers") or [])
            r["onde"] = "horário local (executado pelo Coordenador)" if local else (
                f"cenário {p['value']['name']} no MCP" if p.get("mcp_scenario_id") else
                f"cenário {p['value']['name']} (não enviado: modo de ensaio)")
            rules.append(r)
        muted = [_row(p, state) for p in active if p["key"] == "suggestion_muted"]
        history = [_row(p, state) for p in self.mem.prefs.history(n=100)]
        decisions = self.mem.decisions.recent(200)
        answered = [d for d in decisions if d.get("user_response")]
        return {"preferencias": prefs, "regras": rules, "silenciadas": muted, "historico": history,
                "layout": [_row(p, state) for p in self.mem.prefs.history("layout", n=20)],
                "decisoes_respondidas": len(answered),
                "nota": "Tudo fica guardado só neste computador (var/home.db). Os dados dos sensores (histórico "
                        "da casa) não são apagados por «apagar tudo»."}

    def edit(self, pref_id: str, text: str) -> dict:
        p = self.mem.prefs.get(pref_id)
        if p is None or not p["active"]:
            raise KnowledgeError("preferência inexistente ou já inativa")
        key, v, t = p["key"], dict(p["value"]), (text or "").strip()
        nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", t)]
        if key == "setpoint_c":
            if not nums:
                raise KnowledgeError("indica a temperatura, ex.: 21")
            v["c"] = nums[0]
        elif key == "comfort_range_c":
            if len(nums) < 2:
                raise KnowledgeError("indica o intervalo, ex.: 19-22")
            v = {"min_c": nums[0], "max_c": nums[1]}
        elif key == "humidity_max_pct":
            if not nums:
                raise KnowledgeError("indica a humidade, ex.: 65")
            v = {"pct": nums[0]}
        elif key == "autonomy":
            level = t.casefold()
            if level not in LEVELS:
                raise KnowledgeError("usa auto, perguntar ou nunca")
            v["level"] = level
        elif key == "quiet_hours":
            m = re.findall(r"(\d{1,2})(?::(\d{2}))?", t)
            if len(m) < 2:
                raise KnowledgeError("indica as horas, ex.: 23:00-07:30")
            v = {"start": f"{int(m[0][0]):02d}:{int(m[0][1] or 0):02d}", "end": f"{int(m[1][0]):02d}:{int(m[1][1] or 0):02d}"}
        elif key == "note":
            if not t:
                raise KnowledgeError("a nota não pode ficar vazia")
            v = {"text": t}
        else:
            raise KnowledgeError("este tipo só pode ser revogado")
        return self.mem.prefs.set(p["scope"], key, v, "explicit", f"editado em «O que a casa sabe de ti»: {t}",
                                  mcp_scenario_id=p.get("mcp_scenario_id"), replaces=p["id"])

    def revoke(self, pref_id: str) -> dict:
        p = self.mem.prefs.get(pref_id)
        if p is None:
            raise KnowledgeError("preferência inexistente")
        note = ""
        if p["key"] == "rule" and p.get("mcp_scenario_id"):
            out = self.coordinator.set_scenario_active(p["mcp_scenario_id"], False, p["value"].get("name", ""))
            note = out["note"]
        self.mem.prefs.revoke(pref_id)
        if self.chat is not None:
            self.chat.say(f"Revoguei: {describe_pref(p)}." + (f" {note}" if note else ""))
        return {"ok": True, "nota": note}

    def erase_all(self) -> dict:
        notes = []
        for p in self.mem.prefs.get_active(None, "rule"):
            if p.get("mcp_scenario_id"):
                try:
                    notes.append(self.coordinator.set_scenario_active(p["mcp_scenario_id"], False,
                                                                       p["value"].get("name", ""))["note"])
                except Exception as e:  # noqa: BLE001 - erase anyway; report it
                    notes.append(f"não consegui desativar {p['value'].get('name')}: {e}")
        n = self.mem.prefs.erase_all()
        n_dec = self.mem.decisions.clear()
        n_sug = self.insights.store.clear() if self.insights else 0
        n_chat = self.chat.store.clear() if self.chat else 0
        if self.chat is not None:
            self.chat.confirm = None
            self.chat.say("Apaguei tudo o que sabia de ti: preferências, regras, sugestões, decisões e esta conversa.")
        return {"preferencias": n, "decisoes": n_dec, "sugestoes": n_sug, "mensagens": n_chat, "notas": notes}
