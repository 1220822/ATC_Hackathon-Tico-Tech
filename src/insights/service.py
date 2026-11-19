"""Proactive suggestions: detectors (code) -> anti-micromanagement filter -> phrase -> chat.

    svc = InsightService(memory, settings, coordinator, memories, llm)
    svc.start_thread()            # at start-up and every INSIGHT_EVERY_S (30 min)
    svc.on_request(chat)          # "tens alguma sugestão?"

Filter (the Coordinator's job, so the house does not micromanage the user):
  - at most INSIGHT_MAX_PER_DAY (2) suggestions per day, none during quiet_hours (on request: allowed);
  - only when there is an action with existing devices;
  - a rejected suggestion is not repeated for 7 days; "não voltes a sugerir isto" -> suggestion_muted.
Phrase: the agent that owns the insight (Conforto / Energia) rewrites the template with the LLM;
the rewrite is kept only if it keeps every number of the evidence and says nothing like "sempre" or
"todas as noites" (otherwise the template is used). Accepting follows the normal flow: restated,
confirmed, then preference / TT_ scenario / schedule through the Coordinator, stored in both memories.
"""

import logging
import re
import threading
import time
from datetime import datetime

from pydantic import BaseModel, Field

from ..chat.intents import RuleRequest
from ..chat.rules import RuleProblem, build_rule, scenario_payload
from ..storage.db import SqliteStore, dumps, loads
from .detectors import Insight, run_all

log = logging.getLogger(__name__)
REJECT_COOLDOWN_S = 7 * 86400
BANNED = re.compile(r"\b(sempre|todas as noites|todos os dias|todas as manh[ãa]s|constantemente|nunca)\b", re.I)
METRIC_PT = {"humidity": ("Humidade", "%"), "temperature": ("Temperatura", "°C"), "power_w": ("Potência", "W"),
             "on": ("Ligado", ""), "open": ("Aberta (janela/porta)", "")}


class Phrase(BaseModel):
    frase: str = Field(min_length=10, max_length=450)


class SuggestionStore(SqliteStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS suggestions (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, key TEXT NOT NULL, type TEXT, agent TEXT,
        insight TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL,
        shown_at REAL, responded_at REAL, requested INTEGER DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS suggestions_key ON suggestions (key, ts);
    """

    def add(self, ins: Insight, text: str, requested: bool) -> dict:
        import uuid
        row = {"id": uuid.uuid4().hex[:10], "ts": time.time(), "key": ins.key, "type": ins.type, "agent": ins.agent,
               "insight": dumps(ins.model_dump()), "text": text, "status": "shown", "shown_at": time.time(),
               "responded_at": None, "requested": int(requested)}
        cols = list(row)
        self.execute(f"INSERT INTO suggestions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [row[c] for c in cols])
        return self.get(row["id"])

    def get(self, id: str) -> dict | None:
        r = self.row("SELECT * FROM suggestions WHERE id = ?", (id,))
        if r:
            r["insight"] = loads(r["insight"], {})
        return r

    def set_status(self, id: str, status: str) -> None:
        self.execute("UPDATE suggestions SET status = ?, responded_at = ? WHERE id = ?", (status, time.time(), id))

    def recent(self, n: int = 50) -> list[dict]:
        rows = self.rows("SELECT * FROM suggestions ORDER BY ts DESC LIMIT ?", (n,))
        for r in rows:
            r["insight"] = loads(r["insight"], {})
        return rows

    def by_key(self, key: str) -> list[dict]:
        return self.rows("SELECT id, ts, status, responded_at FROM suggestions WHERE key = ? ORDER BY ts DESC", (key,))

    def shown_since(self, since: float, auto_only: bool = True) -> int:
        sql = "SELECT COUNT(*) AS n FROM suggestions WHERE shown_at >= ?" + (" AND requested = 0" if auto_only else "")
        return (self.row(sql, (since,)) or {"n": 0})["n"]

    def clear(self) -> int:
        return self.execute("DELETE FROM suggestions")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".").rstrip("0").rstrip(".") for n in re.findall(r"\d+(?:[.,]\d+)?", text)}


def faithful(candidate: str, template: str) -> bool:
    """The LLM's sentence may not add numbers, drop the evidence's numbers or generalise."""
    if not candidate or BANNED.search(candidate) and not BANNED.search(template):
        return False
    a, b = _numbers(candidate), _numbers(template)
    return a <= b and len(b - a) <= max(0, len(b) // 3) and "?" in candidate


class InsightService:
    def __init__(self, memory, settings, coordinator, memories, llm=None, min_days: int = 2,
                 max_per_day: int = 2, every_s: float = 1800.0):
        self.memory, self.s, self.coordinator, self.mem, self.llm = memory, settings, coordinator, memories, llm
        self.min_days, self.max_per_day, self.every_s = min_days, max_per_day, every_s
        self.store = SuggestionStore(memories.prefs.path)
        self.chat = None  # set by the API (ChatService) so automatic suggestions reach the chat
        self._lock = threading.RLock()

    # -- detection + filter -------------------------------------------------------------
    def candidates(self, now: float | None = None) -> list[Insight]:
        state = self.memory.state
        if state is None:
            return []
        exclude = [d["ts"] for d in self.mem.decisions.recent(200) if d["status"] == "executed"]
        return run_all(self.mem.history, self.mem.events, state, self.mem.prefs, self.min_days, now,
                       store_interval_s=self.s.store_interval_s, exclude_ts=exclude)

    def eligible(self, ins: Insight, now: float, on_request: bool = False) -> tuple[bool, str]:
        if ins.proposed_action is None:
            return False, "sem ação possível"
        if self.mem.prefs.muted(ins.key):
            return False, "silenciada"
        for r in self.store.by_key(ins.key):
            if r["status"] == "rejected" and now - (r["responded_at"] or r["ts"]) < REJECT_COOLDOWN_S:
                return False, "rejeitada há menos de 7 dias"
            if r["status"] in ("shown", "accepted"):
                return False, "já mostrada" if r["status"] == "shown" else "já aceite"
        if not on_request:
            if self.mem.prefs.quiet_now(datetime.fromtimestamp(now)):
                return False, "horas de silêncio"
            midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
            if self.store.shown_since(midnight) >= self.max_per_day:
                return False, "limite diário"
        return True, ""

    def run(self, now: float | None = None) -> list[dict]:
        """Automatic run: post the eligible suggestions (respecting the daily limit) to the chat."""
        now = time.time() if now is None else now
        posted = []
        with self._lock:
            for ins in self.candidates(now):
                ok, why = self.eligible(ins, now)
                if not ok:
                    log.debug("insight %s skipped: %s", ins.key, why)
                    continue
                posted.append(self.post(ins, requested=False))
        return posted

    def on_request(self, chat=None) -> list[dict]:
        now = time.time()
        with self._lock:
            cands = self.candidates(now)
            for ins in cands:
                ok, _ = self.eligible(ins, now, on_request=True)
                if ok:
                    return [self.post(ins, requested=True, chat=chat)]
        say = (chat or self.chat).say
        span = self._data_span()
        if cands:
            return [say("Já te fiz as sugestões que tinha (ou pediste para não as repetir). Quando aparecer um padrão "
                        "novo, digo-te.")]
        return [say("Ainda não encontrei nenhum padrão que valha uma sugestão." + span)]

    def _data_span(self) -> str:
        rows = self.mem.history.summary()
        if not rows:
            return " Ainda não tenho histórico gravado."
        first = min(r["first"] for r in rows)
        days = (time.time() - first) / 86400
        return (f" Tenho {('menos de um dia' if days < 1 else f'{days:.0f} dias'.replace('.', ','))} de dados; "
                f"preciso de ver a mesma coisa em pelo menos {self.min_days} dias.")

    # -- phrase + post ----------------------------------------------------------------------
    def phrase(self, ins: Insight) -> str:
        if self.llm is None:
            return ins.text
        from ..agents.specs import AGENTS
        from ..llm import LLMError
        spec = AGENTS.get(ins.agent)
        system = ((spec.instructions if spec else "") +
                  "\nReescreve a sugestão do utilizador em português de Portugal, numa ou duas frases curtas e "
                  "simpáticas. Mantém exatamente todos os números, datas e períodos. Não generalizes (nada de "
                  "'sempre', 'todas as noites', 'todos os dias'). Mantém a pergunta final.")
        try:
            got = self.llm.structured(system, ins.text, Phrase).frase.strip()
        except LLMError as e:
            log.info("insight phrase: template (%s)", e)
            return ins.text
        return got if faithful(got, ins.text) else ins.text

    def post(self, ins: Insight, requested: bool, chat=None) -> dict:
        chat = chat or self.chat
        text = self.phrase(ins)
        row = self.store.add(ins, text, requested)
        missing = ins.proposed_action.missing_device if ins.proposed_action else None
        buttons = [{"label": "Aceitar", "action": "suggestion", "response": "accept", "id": row["id"]},
                   {"label": "Agora não", "action": "suggestion", "response": "reject", "id": row["id"]},
                   {"label": "Não voltes a sugerir isto", "action": "suggestion", "response": "mute", "id": row["id"]}]
        agent = {"conforto": "Conforto", "energia": "Energia"}.get(ins.agent, ins.agent)
        if chat is None:
            return row
        return chat.say(f"Sugestão ({agent}): {text}", buttons=buttons, suggestion_id=row["id"], kind="suggestion",
                        benefit=ins.benefit, missing_device=missing)

    def pending(self) -> list[dict]:
        return [r for r in self.store.recent(20) if r["status"] == "shown"]

    def last_shown(self) -> dict | None:
        rows = self.store.recent(1)
        return rows[0] if rows else None

    # -- answers ----------------------------------------------------------------------------
    def respond(self, id: str | None, response: str, chat=None) -> list[dict]:
        chat = chat or self.chat
        row = self.store.get(id) if id else None
        if row is None:
            return [chat.say("Não encontrei essa sugestão.")] if chat else []
        if row["status"] != "shown" and response != "mute":
            return [chat.say("Essa sugestão já foi respondida.")] if chat else []
        ins = Insight.model_validate(row["insight"])
        if response == "reject":
            self.store.set_status(id, "rejected")
            return [chat.say("Está bem. Não volto a sugerir isto nos próximos 7 dias.")] if chat else []
        if response == "mute":
            self.store.set_status(id, "muted")
            self.mem.prefs.set("global", "suggestion_muted",
                               {"insight_key": ins.key, "description": ins.proposed_action.description
                                if ins.proposed_action else ins.text[:200]}, "explicit", "Não voltes a sugerir isto")
            return [chat.say("Combinado: não volto a sugerir isto. Podes mudar de ideias em «O que a casa sabe de ti».")]
        # accept: restate in concrete terms and ask for confirmation
        act = ins.proposed_action
        note = " (Modo de ensaio: nada é enviado à casa.)" if self.s.dry_run and act.kind == "scenario" else ""
        return [chat._ask("suggestion", f"Vou {act.description}.{note} Confirmas?", {"suggestion_id": id})]

    def accept_confirmed(self, payload: dict, chat) -> list[dict]:
        row = self.store.get(payload.get("suggestion_id"))
        if row is None:
            return [chat.say("A sugestão já não existe.")]
        ins = Insight.model_validate(row["insight"])
        act = ins.proposed_action
        state = self.memory.state
        if act.kind == "scenario":
            names = {p["value"]["name"] for p in self.mem.prefs.history("rule")}
            try:
                rule, _ = build_rule(RuleRequest.model_validate(act.rule), state, names)
            except RuleProblem as e:
                return [chat.say(str(e))]
            res = self.coordinator.create_scenario(scenario_payload(rule), f"Sugestão aceite: {act.description}.")
            self.mem.prefs.set("global", "rule", rule, "learned_confirmed", row["text"], mcp_scenario_id=res.get("scenario_id"))
            msg = f"Feito: regra {rule['name']}. {res['note']}"
        elif act.kind == "preference" and act.pref:
            self.mem.prefs.set(act.pref["scope"], act.pref["key"], act.pref["value"], "learned_confirmed", row["text"])
            msg = "Feito: guardei a preferência."
        else:  # schedule: local time trigger, executed by the Coordinator (MCP timers return 404)
            sch = act.schedule
            name = f"TT_horario_{sch['at'].replace(':', 'h')}_{sch['value']}"
            rule = {"name": name, "description": act.description, "logic": "AND", "notify": False,
                    "triggers": [{"type": "time", "at": sch["at"]}],
                    "actions": [{"type": "device", "device": {"name": "", "id": sch["device_id"]},
                                 "capabilities": [{"name": "", "type": "power", "value": sch["value"]}]}]}
            self.mem.prefs.set("global", "rule", rule, "learned_confirmed", row["text"])
            msg = f"Feito: {act.description}, todos os dias (horário local, executado por mim)."
        self.store.set_status(row["id"], "accepted")
        return [chat.say(msg)]

    # -- "Porquê?" ---------------------------------------------------------------------------------
    def evidence(self, id: str) -> dict | None:
        row = self.store.get(id)
        if row is None:
            return None
        ins = Insight.model_validate(row["insight"])
        ev = ins.evidence
        spans = [tuple(s) for s in ev.spans] or []
        if spans:
            start, end = min(s[0] for s in spans) - 3 * 3600, max(s[1] for s in spans) + 3600
        else:
            end = time.time()
            start = end - 3 * 86400
        start = max(start, end - 4 * 86400)
        metric = ev.metric if ev.metric in METRIC_PT else "temperature"
        series = []
        metrics = ["open", "on"] if ins.type == "window_heater" else [metric]
        for m in metrics:
            rows = self.mem.history.aggregate(ev.room if m != "open" else None, m, 900, "avg", start, end)
            label, unit = METRIC_PT.get(m, (m, ""))
            series.append({"label": label, "unit": unit, "points": [[r["ts"], round(r["value"], 2)] for r in rows]})
        if ev.threshold is not None and series and series[0]["points"] and ins.type == "daily_pattern":
            pts = series[0]["points"]
            series.append({"label": "limite", "unit": ev.unit, "points": [[pts[0][0], ev.threshold], [pts[-1][0], ev.threshold]]})
        resumo = (f"Dias observados: {', '.join(ev.days)}" + (f" · janela {ev.window}" if ev.window else "")
                  + f" · confiança {round(ins.confidence * 100)} %")
        legenda = ("A sombreado, os períodos que contaram. " + ins.benefit)
        return {"id": id, "resumo": resumo, "legenda": legenda, "series": series,
                "janelas": [list(s) for s in spans], "insight": ins.model_dump()}

    # -- loop -------------------------------------------------------------------------------------
    def start_thread(self, first_delay_s: float = 60.0) -> threading.Thread:
        def loop():
            stop = threading.Event()
            stop.wait(first_delay_s)
            while True:
                try:
                    self.run()
                except Exception:  # noqa: BLE001
                    log.exception("insight run failed")
                stop.wait(self.every_s)
        t = threading.Thread(target=loop, name="insights", daemon=True)
        t.start()
        return t
