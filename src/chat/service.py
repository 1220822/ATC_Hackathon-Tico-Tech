"""The user's only conversation: the Coordinator's chat.

    chat = ChatService(memory, settings, coordinator, memories, llm)
    chat.handle("quero o quarto a 21 °C de manhã")  -> new messages (user + Coordenador)
    chat.action("confirm", id=...)                    -> buttons (Confirmar, Aprovar, Rejeitar, ...)

Rules kept here:
  - nothing is written before the user confirms: preferences, rules, learned autonomy and arm/disarm
    are first restated in concrete terms ("Vou definir 21 °C no Quarto entre as 7h e as 9h. Confirmas?");
  - every write to the house goes through the Coordinator (guard/execute, DRY_RUN respected);
  - pending decisions and suggestions show up as messages with Aprovar / Rejeitar / Alterar;
  - after 3 approvals in a row of the same kind of decision the house asks whether it may do it
    alone; only a "sim" stores a learned_confirmed preference.

Messages are stored in var/home.db (chat_messages) so the UI survives a reload.
"""

import logging
import re
import threading
import time
import uuid

from pydantic import ValidationError

from ..context import Memories, describe_pref, scope_label
from ..coordinator import NOUN, RequestRefused, UserRequest
from ..perception.state import HomeState, de, em, fmt
from ..prefs.models import CATEGORY_PT, validate_value
from ..storage.db import SqliteStore, dumps, loads
from .answers import answer_state
from .intents import SEC_AWAY, ChatIntent, PrefRequest, classify, norm, plan, split_requests, understood
from .rules import RuleProblem, build_rule, scenario_payload

log = logging.getLogger(__name__)

YES = re.compile(r"^(sim|confirmo|confirma|ok|okay|pode ser|podes|claro|isso|avanca|esta bem|certo|exato|faz)\b")
NO = re.compile(r"^(nao|cancela|cancelar|esquece|deixa|para|nem pensar)\b")
HELP = ("Posso responder sobre a casa («a janela da sala está fechada?», «quanto gastei ontem?»), mudar "
        "preferências («quero o quarto a 21 °C de manhã»), criar regras («se abrir a janela com o aquecedor "
        "ligado, avisa-me»), ajustar o 3D («o quarto fica à direita da sala») e dar sugestões («tens alguma "
        "sugestão?»).")


class ChatStore(SqliteStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
        meta TEXT
    );
    """

    def add(self, role: str, text: str, **meta) -> dict:
        ts = time.time()
        with self.connect() as db:
            cur = db.execute("INSERT INTO chat_messages (ts, role, text, meta) VALUES (?, ?, ?, ?)",
                             (ts, role, text, dumps(meta or {})))
            mid = cur.lastrowid
        db.close()
        return {"id": mid, "ts": ts, "role": role, "text": text, **(meta or {})}

    def after(self, after_id: int = 0, n: int = 100) -> list[dict]:
        rows = self.rows("SELECT * FROM chat_messages WHERE id > ? ORDER BY id DESC LIMIT ?", (after_id, n))
        out = []
        for r in reversed(rows):
            meta = loads(r.pop("meta"), {})
            out.append({**r, **meta})
        return out

    def clear(self) -> int:
        return self.execute("DELETE FROM chat_messages")


def _h(hhmm: str) -> str:
    h, m = hhmm.split(":")
    return f"{int(h)}h" + (m if m != "00" else "")


BUTTONS_CONFIRM = [{"label": "Confirmar", "action": "confirm"}, {"label": "Cancelar", "action": "cancel"}]
BUTTONS_DECISION = [{"label": "Aprovar", "action": "decision", "response": "approved"},
                    {"label": "Rejeitar", "action": "decision", "response": "rejected"},
                    {"label": "Alterar", "action": "decision", "response": "modified", "ask": "O que queres diferente?"}]


class ChatService:
    def __init__(self, memory, settings, coordinator, memories: Memories, llm=None, insights=None, layout=None):
        self.memory, self.s, self.coordinator, self.mem, self.llm = memory, settings, coordinator, memories, llm
        self.insights = insights  # src.insights.service.InsightService (step 6)
        self.layout = layout  # src.twin.edit.LayoutEditor (step 7)
        self.vision = None  # src.vision.worker.VisionWorker: "está alguém na sala?" looks through the camera
        self.store = ChatStore(memories.prefs.path)
        self.confirm: dict | None = None  # what waits for "sim" / "não"
        self.queue: list[tuple[ChatIntent, str]] = []  # requests of the same message waiting for that answer
        self._lock = threading.RLock()

    # -- helpers -----------------------------------------------------------------------
    def say(self, text: str, **meta) -> dict:
        return self.store.add("coordenador", text, **meta)

    def _state(self) -> HomeState | None:
        return self.memory.state

    def _rooms(self) -> list[str]:
        st = self._state()
        return [r.name for r in st.rooms.values()] if st else []

    def _ask(self, kind: str, text: str, payload: dict, original: str = "") -> dict:
        self.confirm = {"id": uuid.uuid4().hex[:8], "kind": kind, "payload": payload, "text": text,
                        "original": original, "ts": time.time()}
        buttons = [dict(b, id=self.confirm["id"]) for b in BUTTONS_CONFIRM]
        return self.say(text, buttons=buttons, confirm_id=self.confirm["id"])

    # -- entry points ----------------------------------------------------------------------
    def handle(self, text: str) -> list[dict]:
        text = (text or "").strip()
        if not text:
            return []
        with self._lock:
            out = [self.store.add("user", text)]
            t = norm(text)
            if self.confirm is not None:
                if YES.match(t):
                    return out + self._do_confirm() + self._next_in_queue()
                if NO.match(t) and len(t) < 40:
                    return out + [self._cancel()] + self._next_in_queue()
                self.confirm = None  # a new request replaces the open question
                self.queue = []
            state = self._state()
            if state is None:
                return out + [self.say("Ainda estou a ler a casa. Tenta daqui a uns segundos.")]
            return out + self._run(self._read(text), state)

    def _read(self, text: str) -> list[tuple[ChatIntent, str]]:
        """Message -> [(intent, part of the text)]. With an LLM, every message is read by the LLM thinking
        it over (CHAT_REASONING_EFFORT, default "high"), which returns every request of the message: slower,
        but the answers are right. The PT-PT rules read it too, only to fill fields the LLM left out, and
        are the whole reading when there is no LLM (or it fails)."""
        pending_dec = bool(self.mem.decisions.pending())
        pending_sug = bool(self.insights.pending()) if self.insights else False
        rooms = self._rooms()
        think = self.llm is not None and bool(self.s.chat_reasoning_effort)
        parts = split_requests(text)
        items = [(classify(p, None if think else self.llm, rooms, pending_dec, pending_sug), p) for p in parts]
        if len(items) > 1:  # "vou sair": context only, unless nothing else was understood
            useful = [(i, p) for i, p in items if i.intent != "outro"]
            items = useful or items[:1]
        if think:
            planned = plan(text, self.llm, rooms, pending_dec, pending_sug, self.s.chat_reasoning_effort) or []
            # an answer to a proposal or suggestion that is not there is a misreading
            planned = [i for i in planned
                       if not (i.intent == "responder_proposta" and not pending_dec and not i.sempre)
                       and not (i.intent == "responder_sugestao" and not pending_sug)]
            if planned:
                for i in planned:  # fields the rules extracted reliably fill what the LLM left out
                    same = next((r for r, _ in items if r.intent == i.intent), None)
                    for f in ("divisao", "topico", "periodo", "preferencia", "regra", "layout", "abertura", "comando"):
                        if same is not None and getattr(i, f) is None and getattr(same, f) is not None:
                            setattr(i, f, getattr(same, f))
                items = [(i, text) for i in planned if i.intent != "outro" or len(planned) == 1]
                log.info("chat: pensei (%s) -> %d pedido(s)", self.s.chat_reasoning_effort, len(items))
        away = bool(SEC_AWAY.search(norm(text)))
        for i, _ in items:  # "vou sair, arma a casa": the context of the whole message counts
            c = i.comando
            if c is not None and c.dispositivo == "seguranca" and c.acao == "armar_casa" and away:
                c.acao = "armar_ausente"
        # the same request said twice ("foi falso alarme, desliga o alarme" = desarmar + desarmar) is done
        # once: never two confirmations for one thing
        seen, unique = set(), []
        for i, p in items:
            key = repr(i.model_dump(exclude={"origem", "comentario"}, exclude_none=True))
            if key not in seen:
                seen.add(key)
                unique.append((i, p))
        items = unique
        for i, _ in items:
            log.info("chat intent (%s): %s", i.origem, i.model_dump(exclude_none=True))
        # what runs at once first (commands, questions); what needs a confirmation after, one at a time
        immediate = [(i, p) for i, p in items
                     if i.intent in ("pergunta_estado", "responder_proposta", "responder_sugestao")
                     or (i.intent == "comando_dispositivo" and i.comando is not None
                         and i.comando.dispositivo != "seguranca")]
        return immediate + [x for x in items if x not in immediate]

    def _run(self, items: list[tuple[ChatIntent, str]], state: HomeState) -> list[dict]:
        out: list[dict] = []
        for k, (intent, text) in enumerate(items):
            try:
                out += self._dispatch(intent, text, state, self.mem.decisions.pending(),
                                      self.insights.pending() if self.insights else [])
            except RequestRefused as e:
                out.append(self.say(f"Não consegui: {e}."))
            except Exception as e:  # noqa: BLE001 - the chat must answer something
                log.exception("chat handler failed")
                out.append(self.say(f"Algo correu mal ao tratar o pedido ({type(e).__name__})."))
            if self.confirm is not None and k + 1 < len(items):  # wait for the answer, then go on
                self.queue = items[k + 1:]
                break
        return out

    def _next_in_queue(self) -> list[dict]:
        """After a confirmation was answered: the next requests of the same message."""
        if not self.queue or self.confirm is not None:
            return []
        items, self.queue = self.queue, []
        state = self._state()
        return self._run(items, state) if state is not None else []

    def clear(self) -> int:
        """«Limpar conversa»: the messages go, and so does any question still open in them (a confirmation
        and the requests queued behind it). Preferences, rules and the agents' decisions stay."""
        with self._lock:
            self.confirm, self.queue = None, []
            return self.store.clear()

    def action(self, action: str, id: str | None = None, response: str | None = None,
               comment: str | None = None) -> list[dict]:
        """Buttons of the UI."""
        with self._lock:
            if action == "confirm":
                if self.confirm is None or (id and self.confirm["id"] != id):
                    return [self.say("Essa pergunta já não está em aberto.")]
                return [self.store.add("user", "Confirmar")] + self._do_confirm() + self._next_in_queue()
            if action == "cancel":
                if self.confirm is not None and id and self.confirm["id"] != id:
                    return [self.say("Essa pergunta já não está em aberto.")]
                return [self.store.add("user", "Cancelar"), self._cancel()] + self._next_in_queue()
            if action == "decision":
                label = {"approved": "Aprovar", "rejected": "Rejeitar", "modified": "Alterar"}.get(response, response)
                out = [self.store.add("user", label + (f": {comment}" if comment else ""))]
                return out + self._answer_decision(id, response, comment)
            if action == "suggestion" and self.insights is not None:
                out = [self.store.add("user", {"accept": "Aceitar", "reject": "Agora não",
                                               "mute": "Não voltes a sugerir isto"}.get(response, response))]
                return out + self.insights.respond(id, response, chat=self)
            if action.startswith("layout") and self.layout is not None:
                return self.layout.chat_action(action, self)
            return [self.say("Não percebi esse botão.")]

    def notify_pending(self, rec: dict) -> dict:
        """Coordinator.on_pending: a decision waits for the user."""
        why = rec.get("explanation") or ""
        debate = [f"{p.get('agent')}: {'ligar' if p.get('acao') == 'on' else 'desligar'} — {p.get('motivo') or 'sem motivo'}"
                  for p in rec.get("proposals") or []]
        return self.say(f"Proposta para aprovares: {why}", buttons=[dict(b, id=rec["id"]) for b in BUTTONS_DECISION],
                        decision_id=rec["id"], why={"explicacao": why, "debate": debate})

    # -- dispatch ----------------------------------------------------------------------------
    def _dispatch(self, intent: ChatIntent, text: str, state: HomeState, pending_dec: list,
                  pending_sug: list) -> list[dict]:
        if intent.intent == "pergunta_estado":
            return self._question(intent, state)
        if intent.intent == "alterar_preferencia":
            return self._preference(intent, text, state)
        if intent.intent == "criar_regra":
            return self._rule(intent, text, state)
        if intent.intent == "configurar_layout":
            if self.layout is None:
                return [self.say("O ajuste do 3D ainda não está disponível.")]
            if intent.abertura is not None:  # a window / door of the floor plan
                if intent.abertura.divisao is None:
                    intent.abertura.divisao = intent.divisao
                return self.layout.from_chat(intent.abertura, text, self)
            return self.layout.from_chat(intent.layout, text, self)
        if intent.intent == "responder_proposta" and intent.sempre:
            return self._always(pending_dec)
        if intent.intent == "responder_proposta":
            if not pending_dec:
                return [self.say("Não há nenhuma proposta à espera de resposta.")]
            resp = {"aprovar": "approved", "rejeitar": "rejected", "alterar": "modified"}.get(intent.resposta or "", "approved")
            return self._answer_decision(pending_dec[-1]["id"], resp, intent.comentario)
        if intent.intent == "responder_sugestao":
            if self.insights is None:
                return [self.say("Ainda não tenho sugestões.")]
            resp = {"aprovar": "accept", "rejeitar": "reject", "silenciar": "mute"}.get(intent.resposta or "", "accept")
            sug = pending_sug[-1]["id"] if pending_sug else None
            if sug is None:
                last = self.insights.last_shown()
                sug = last["id"] if last else None
            if sug is None:
                return [self.say("Não há nenhuma sugestão em aberto.")]
            return self.insights.respond(sug, resp, chat=self)
        if intent.intent == "comando_dispositivo" and intent.comando is not None:
            return self._command(intent, state)
        return [self.say(HELP)]

    # -- intents -------------------------------------------------------------------------------
    def _question(self, intent: ChatIntent, state: HomeState) -> list[dict]:
        topic = intent.topico or "geral"
        if topic == "sugestoes":
            if self.insights is None:
                return [self.say("Ainda não tenho sugestões.")]
            return self.insights.on_request(chat=self)
        if topic in ("regras", "preferencias"):
            return [self.say(self.knowledge_summary(state))]
        if topic == "presenca":
            return [self._look(intent, state)]
        room = state.room(intent.divisao).id if intent.divisao and state.room(intent.divisao) else None
        return [self.say(answer_state(topic, intent.periodo or "agora", room, state, self.mem))]

    def _look(self, intent: ChatIntent, state: HomeState) -> dict:
        """Presence: analyse the camera image now with the vision model (never guessed from sensors)."""
        cams = state.devices_by_role("camera")
        room = state.room(intent.divisao) if intent.divisao else None
        if not cams:
            return self.say("Não há nenhuma câmara na casa, por isso não consigo ver se está alguém.")
        cam = next((c for c in cams if room is None or c.room_id == room.id), None)
        if cam is None:
            where = sorted({c.room_name for c in cams})
            return self.say(f"Não há câmara {em(room.name)}; "
                            + (f"a única câmara está {em(where[0])}." if len(where) == 1 else
                               "as câmaras estão " + ", ".join(em(w) for w in where) + "."))
        if self.vision is None:
            return self.say("A análise da câmara está desligada (VISION_ENABLED=false).")
        obs = self.vision.analyze_now(room=cam.room_id, context="general")
        if obs.status != "ok":
            why = {"capture_error": "não consegui obter a imagem da câmara",
                   "timeout": "o modelo de visão demorou demasiado",
                   "inference_error": "o modelo de visão falhou"}.get(obs.status, obs.status)
            return self.say(f"Não consegui ver {em(cam.room_name)}: {why}.")
        if obs.person_count:
            who = "1 pessoa" if obs.person_count == 1 else f"{obs.person_count} pessoas"
            head = f"Sim, vejo {who} {em(cam.room_name)}"
        else:
            head = f"Não vejo ninguém {em(cam.room_name)}"
        text = f"{head} (câmara, há instantes; confiança {round(obs.overall_confidence * 100)} %)."
        if obs.scene_note_pt:
            text += f" {obs.scene_note_pt}"
        image = f"/vision/frames/{obs.frame_path}" if obs.frame_path else None
        return self.say(text, image=image, vision_id=obs.id)

    def knowledge_summary(self, state: HomeState | None) -> str:
        prefs = [p for p in self.mem.prefs.get_active() if p["key"] != "layout"]
        if not prefs:
            return "Ainda não sei nada de ti: não tenho preferências nem regras guardadas."
        lines = [f"{describe_pref(p)} ({scope_label(p['scope'], state)})" for p in prefs]
        return "O que sei de ti: " + "; ".join(lines) + ". Podes ver, editar e apagar tudo em «O que a casa sabe de ti»."

    def _target_room(self, intent: ChatIntent, state: HomeState, role: str = "temperature"):
        if intent.divisao and state.room(intent.divisao):
            return state.room(intent.divisao)
        devs = state.devices_by_role(role)
        rooms = list(dict.fromkeys(d.room_id for d in devs))
        return state.room(rooms[0]) if len(rooms) == 1 else None

    def _preference(self, intent: ChatIntent, text: str, state: HomeState) -> list[dict]:
        p: PrefRequest | None = intent.preferencia
        if p is None:
            return [self.say("Não percebi que preferência queres mudar. Exemplo: «quero o quarto a 21 °C de manhã».")]
        try:
            scope, key, value, what = self._pref_value(p, intent, state)
            value = validate_value(key, value)
        except (ValidationError, ValueError) as e:
            msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
            return [self.say(f"Não posso guardar isso: {msg}.")]
        return [self._ask("pref", f"{what} Confirmas?", {"scope": scope, "key": key, "value": value}, text)]

    def _pref_value(self, p: PrefRequest, intent: ChatIntent, state: HomeState) -> tuple[str, str, dict, str]:
        if p.tipo in ("setpoint_c", "comfort_range_c", "humidity_max_pct"):
            room = self._target_room(intent, state)
            scope = f"room:{room.id}" if room else "global"
            where = em(room.name) if room else "em casa"
            if p.tipo == "setpoint_c":
                if p.valor_c is None:
                    raise ValueError("falta a temperatura")
                existing = self.mem.prefs.get_active(scope, "setpoint_c")
                value = dict(existing[-1]["value"]) if existing else {}
                if p.inicio and p.fim:
                    periods = [x for x in value.get("periods", []) if (x["start"], x["end"]) != (p.inicio, p.fim)]
                    periods.append({"start": p.inicio, "end": p.fim, "c": p.valor_c})
                    value["periods"] = sorted(periods, key=lambda x: x["start"])
                    what = f"Vou definir {fmt(p.valor_c, 1).replace(',0', '')} °C {where} entre as {_h(p.inicio)} e as {_h(p.fim)}."
                else:
                    value["c"] = p.valor_c
                    what = f"Vou definir {fmt(p.valor_c, 1).replace(',0', '')} °C como temperatura alvo {where}."
                return scope, "setpoint_c", value, what
            if p.tipo == "comfort_range_c":
                return scope, "comfort_range_c", {"min_c": p.min_c, "max_c": p.max_c}, \
                    f"Vou manter {room.name if room else 'a casa'} entre {fmt(p.min_c or 0, 1)} e {fmt(p.max_c or 0, 1)} °C."
            return scope, "humidity_max_pct", {"pct": p.pct}, \
                f"Vou considerar {fmt(p.pct or 0, 0)} % como humidade máxima {where}."
        if p.tipo == "autonomy":
            cat, level = p.categoria or "energia", p.nivel or "perguntar"
            examples = {"energia": "desligar o aquecedor quando há desperdício", "conforto": "ligar o aquecedor quando está frio",
                        "luz": "mudar as luzes", "seguranca": "acender luzes por segurança"}
            what = {"auto": f"Vou passar a agir sozinha em {CATEGORY_PT[cat]} (ex.: {examples[cat]}), sem te perguntar.",
                    "perguntar": f"Vou pedir-te aprovação antes de agir em {CATEGORY_PT[cat]}.",
                    "nunca": f"Nunca vou agir em {CATEGORY_PT[cat]}: só registo e te informo."}[level]
            if cat == "seguranca":
                what += " Armar ou desarmar a casa continua a precisar sempre da tua confirmação."
            return "global", "autonomy", {"category": cat, "level": level}, what
        if p.tipo == "quiet_hours":
            return "global", "quiet_hours", {"start": p.inicio or "23:00", "end": p.fim or "07:30"}, \
                f"Não te faço sugestões entre as {_h(p.inicio or '23:00')} e as {_h(p.fim or '07:30')}."
        return "global", "note", {"text": p.texto or ""}, f"Vou guardar a nota: «{p.texto}»."

    def _rule(self, intent: ChatIntent, text: str, state: HomeState) -> list[dict]:
        if intent.regra is None:
            return [self.say("Não percebi a regra. Exemplo: «se abrir a janela com o aquecedor ligado, avisa-me».")]
        names = {p["value"]["name"] for p in self.mem.prefs.history("rule")}
        try:
            rule, description = build_rule(intent.regra, state, names, setpoint=self._setpoint)
        except RuleProblem as e:
            return [self.say(str(e))]
        note = " (Está em modo de ensaio: o cenário fica registado mas não é enviado ao MCP.)" if self.s.dry_run else ""
        return [self._ask("rule", f"Vou criar a regra {rule['name']} no MCP: {description}.{note} Confirmas?",
                          {"rule": rule}, text)]

    def _setpoint(self, room_id: str) -> float | None:
        """Target temperature of a room now: the user's preference, else config/home.json / SETPOINT_C."""
        c, _ = self.mem.prefs.setpoint(room_id)
        if c is not None:
            return c
        st = self._state()
        room = st.rooms.get(room_id) if st else None
        return room.setpoint_c if room else None

    def _command(self, intent: ChatIntent, state: HomeState) -> list[dict]:
        c = intent.comando
        if c.dispositivo == "seguranca":
            mode = self.coordinator.SECURITY_MODES[c.acao] if c.acao in self.coordinator.SECURITY_MODES else None
            if mode is None:
                return [self.say("Não percebi o que queres fazer à segurança.")]
            label = {"STAY": "armar a casa (modo em casa)", "AWAY": "armar a casa (modo ausente)", "DISARMED": "desarmar a casa"}[mode]
            alarmed = state.is_alarmed or getattr(self.coordinator, "emergency_active", False) or any(
                h.caps.get("alarmState") == "alarm" for h in state.devices_by_role("hub"))
            if mode == "DISARMED" and alarmed:
                label += " e desligar o alarme"
            hint = {"STAY": " (Para sair de casa: «arma em modo ausente».)",
                    "AWAY": " (Se ficares em casa: «arma em modo casa».)"}.get(mode, "")
            return [self._ask("security", f"Vou {label}. Confirmas?{hint}", {"mode": mode})]
        role = "light" if c.dispositivo == "luz" else "plug"
        room = state.room(c.divisao) if c.divisao else None
        dev = state.by_role(role, room.id) if room else state.by_role(role)
        if dev is None:
            return [self.say(f"Não encontrei {NOUN.get(role, c.dispositivo)} {de(c.divisao) if c.divisao else ''}.".strip())]
        d = self.coordinator.request(UserRequest(device_id=dev.id, action="on" if c.acao == "ligar" else "off", origin="chat"))
        return [self.say(d.message)]

    # -- confirmations -------------------------------------------------------------------------
    def _do_confirm(self) -> list[dict]:
        c, self.confirm = self.confirm, None
        if c is None:
            return [self.say("Não havia nada para confirmar.")]
        kind, pl = c["kind"], c["payload"]
        try:
            if kind == "pref":
                row = self.mem.prefs.set(pl["scope"], pl["key"], pl["value"], "explicit", c["original"])
                return [self.say(f"Feito. Guardei: {describe_pref(row)}.", pref_id=row["id"])]
            if kind == "learn":
                row = self.mem.prefs.set("global", "autonomy", pl["value"], "learned_confirmed", c["text"])
                return [self.say("Combinado: a partir de agora faço isto sozinha e aviso-te. Podes mudar em "
                                 "«O que a casa sabe de ti».", pref_id=row["id"])]
            if kind == "rule":
                rule = pl["rule"]
                res = self.coordinator.create_scenario(scenario_payload(rule), f"Regra {rule['name']}: {rule['description']}.")
                row = self.mem.prefs.set("global", "rule", rule, "explicit", c["original"],
                                         mcp_scenario_id=res.get("scenario_id"))
                return [self.say(f"Regra {rule['name']} criada. {res['note']}", pref_id=row["id"])]
            if kind == "security":
                res = self.coordinator.set_security(pl["mode"], confirmed=True)
                return [self.say(f"Segurança: {res['note']}")]
            if kind == "suggestion" and self.insights is not None:
                return self.insights.accept_confirmed(pl, chat=self)
            if kind == "layout_ok":
                return [self.say("Ótimo, fica assim. Está guardado no histórico do 3D.")]
        except RequestRefused as e:
            return [self.say(f"Não consegui: {e}.")]
        except (ValidationError, ValueError) as e:
            return [self.say(f"Não consegui guardar: {e}.")]
        except Exception as e:  # noqa: BLE001 - a confirmation must answer in the chat, never a 500
            log.exception("chat confirmation failed")
            return [self.say(f"Não consegui: algo correu mal ({type(e).__name__}).")]
        return [self.say("Não sei confirmar isso.")]

    def _cancel(self) -> dict:
        c, self.confirm = self.confirm, None
        if c is not None and c["kind"] == "learn":
            self.decline_learning(c["payload"]["value"]["kind"])
            return self.say("Está bem, continuo a pedir-te aprovação e não volto a perguntar isto.")
        if c is not None and c["kind"] == "layout_ok" and self.layout is not None:
            return self.layout.not_ok(self)
        if c is not None and c["kind"] == "suggestion" and self.insights is not None:
            self.insights.respond(c["payload"].get("suggestion_id"), "reject", chat=None)
        return self.say("Está bem, não faço nada.")

    # -- decisions + learning -------------------------------------------------------------------
    def _answer_decision(self, decision_id: str | None, response: str | None, comment: str | None) -> list[dict]:
        if not decision_id or response not in ("approved", "rejected", "modified"):
            return [self.say("Não percebi a resposta.")]
        try:
            rec = self.coordinator.resolve_pending(decision_id, response, comment)
        except RequestRefused as e:
            return [self.say(str(e))]
        out = []
        if response == "approved":
            status = {"executed": "Feito.", "dry_run": "Aprovado (modo de ensaio: nada foi enviado à casa).",
                      "vetoed": "Já não dá para executar: " + (rec.get("explanation") or "").split("Não executada:")[-1].strip(),
                      "rejected": "Não foi possível executar."}.get(rec["status"], "Registado.")
            out.append(self.say(status))
            out += self._maybe_learn(rec)
        elif response == "rejected":
            out.append(self.say("Rejeitado. Fica registado: os agentes vão ter isto em conta."))
        else:
            out.append(self.say("Não executo a proposta original." + (" Vou tratar do teu pedido." if comment else
                                                                      " Diz-me o que preferes.")))
            if comment:
                out += self.handle(comment)[1:]
        return out

    def _always(self, pending_dec: list) -> list[dict]:
        """«Faz sempre assim»: approve the pending decision (if any) and offer to do it alone from now on."""
        out: list[dict] = []
        if pending_dec:
            out += self._answer_decision(pending_dec[-1]["id"], "approved", None)
            rec = self.mem.decisions.get(pending_dec[-1]["id"])
        else:
            answered = [d for d in self.mem.decisions.recent(30) if d.get("user_response") == "approved"]
            rec = answered[0] if answered else None
        if rec is None:
            return out + [self.say("Não sei a que decisão te referes: ainda não aprovaste nenhuma.")]
        if self.confirm is not None and self.confirm["kind"] == "learn":
            return out  # the 3-approvals question was just asked
        learn = self._maybe_learn(rec, force=True)
        return out + (learn or [self.say("Isso já é automático.")])

    def _maybe_learn(self, rec: dict, force: bool = False) -> list[dict]:
        kind = rec.get("kind")
        if not kind or (not force and self.mem.decisions.approval_streak(kind) < 3):
            return []
        category = rec.get("category") or "energia"
        if category not in CATEGORY_PT or self.mem.prefs.autonomy(category, kind) == "auto":
            return []
        if self.mem.prefs.muted(f"learn:{kind}") and not force:
            return []
        dec = rec.get("decision") or {}
        state = self._state()
        dev = state.devices.get(dec.get("device_id")) if state else None
        what = (f"{'ligar' if dec.get('value') == 'on' else 'desligar'} {NOUN.get(dev.role, dev.name)} {de(dev.room_name)}"
                if dev else "esta ação")
        when = (rec.get("trigger") or {}).get("message") or "nesta situação"
        value = {"category": category, "level": "auto", "kind": kind,
                 "description": f"{what} quando: {when}"[:300]}
        if force:
            return [self._ask("learn", f"Vou passar a fazer isto sozinha: {what} quando acontecer isto ({when.rstrip('.')}). "
                              "Só isto; o resto continua a pedir-te aprovação. Confirmas?", {"value": value})]
        return [self._ask("learn", f"Aprovaste 3 vezes seguidas: {what} ({when.rstrip('.')}). Queres que passe a "
                          "fazer isto sozinha? Só isto; o resto continua a pedir-te aprovação.", {"value": value})]

    def decline_learning(self, kind: str) -> None:
        self.mem.prefs.set("global", "suggestion_muted", {"insight_key": f"learn:{kind}",
                                                          "description": "não voltar a perguntar se faço sozinha"})
