"""Chat with the Coordinator: classification, confirmations, rules, pending decisions, learning."""

import json
import tempfile
import unittest
from pathlib import Path

from src.agents.specs import AgentReply, CoordinatorReply, Proposal, Verdict
from src.chat.intents import ChatIntent, classify, classify_rules
from src.chat.rules import RuleWatcher, rule_holds
from src.chat.service import ChatService
from src.config import get_settings
from src.context import Memories
from src.coordinator import Coordinator
from src.llm import LLM
from src.memory import SharedMemory
from src.perception.state import Event, normalize
from src.sim.home import SimHome

QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"
ROOMS = ["Sala", "Quarto"]

# 10 PT-PT sample phrases and what they must become
SAMPLES = [
    ("A janela da sala está fechada?", "pergunta_estado", {"topico": "janela"}),
    ("Quanto gastei ontem?", "pergunta_estado", {"topico": "energia", "periodo": "ontem"}),
    ("Quero o quarto a 21 °C de manhã", "alterar_preferencia", {"tipo": "setpoint_c", "inicio": "07:00", "fim": "09:00"}),
    ("Se abrir a janela com o aquecedor ligado, avisa-me", "criar_regra", {"avisar": True}),
    ("O quarto fica à direita da sala", "configurar_layout", {"relacao": "direita", "alvo": "Quarto"}),
    ("O sensor de temperatura está junto à janela", "configurar_layout", {"relacao": "junto_janela"}),
    ("Tens alguma sugestão?", "pergunta_estado", {"topico": "sugestoes"}),
    ("Não voltes a sugerir isto", "responder_sugestao", {"resposta": "silenciar"}),
    ("Podes desligar o aquecedor sozinho, não precisas de perguntar", "alterar_preferencia",
     {"tipo": "autonomy", "nivel": "auto", "categoria": "energia"}),
    ("Não me incomodes entre as 23h e as 8h", "alterar_preferencia", {"tipo": "quiet_hours", "inicio": "23:00", "fim": "08:00"}),
]


def _flat(i: ChatIntent) -> dict:
    out = i.model_dump(exclude_none=True)
    for k in ("preferencia", "layout", "regra", "comando"):
        out.update(out.pop(k, {}) or {})
    return out


class TestClassification(unittest.TestCase):
    def test_rules_classify_the_ten_samples(self):
        for text, intent, fields in SAMPLES:
            with self.subTest(text=text):
                got = classify_rules(text, ROOMS)
                self.assertEqual(got.intent, intent)
                flat = _flat(got)
                for k, v in fields.items():
                    self.assertEqual(flat.get(k), v, k)

    def test_mocked_llm_structured_output(self):
        def fake(messages, schema):
            self.assertIn("schema", messages[0]["content"])
            return '<think>x</think>```json\n{"intent": "pergunta_estado", "topico": "energia", "periodo": "ontem"}\n```'
        got = classify("Quanto gastei ontem?", LLM(complete=fake), ROOMS)
        self.assertEqual((got.intent, got.topico, got.periodo, got.origem), ("pergunta_estado", "energia", "ontem", "llm"))

    def test_invalid_llm_answer_falls_back_to_rules(self):
        got = classify("Quero o quarto a 21 °C de manhã", LLM(complete=lambda m, s: '{"intent": "dançar"}'), ROOMS)
        self.assertEqual((got.intent, got.origem), ("alterar_preferencia", "regras"))

    def test_llm_down_falls_back_to_rules(self):
        def down(m, s):
            raise ConnectionError("no route")
        self.assertEqual(classify("A janela da sala está fechada?", LLM(complete=down), ROOMS).intent, "pergunta_estado")


class ChatCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "none.json",
            "history_db": tmp / "home.db", "dry_run": True, "simulate": True})
        self.home = SimHome(self.s.sim_state_file)
        self.home.reset()
        self.memory = SharedMemory()
        self.mem = Memories.open(self.s.history_db)
        self.coord = Coordinator(self.memory, self.s, sim=self.home, memories=self.mem,
                                 arbiter=lambda prompt: CoordinatorReply(decisoes=[Verdict(n=1, aprovar=True, motivo="janela aberta")]))
        self.chat = ChatService(self.memory, self.s, self.coord, self.mem, llm=None)
        self.coord.on_pending = self.chat.notify_pending
        self.refresh()

    def refresh(self):
        self.memory.update_state(normalize(self.home.snapshot(), self.s))

    def tearDown(self):
        self.tmp.cleanup()

    def last(self, msgs):
        return msgs[-1]["text"]


class TestChatFlows(ChatCase):
    def test_state_question_uses_real_values(self):
        self.home.set_contact(True)
        self.refresh()
        self.assertIn("aberta", self.last(self.chat.handle("A janela da sala está fechada?")))

    def test_preference_needs_confirmation(self):
        msgs = self.chat.handle("Quero o quarto a 21 °C de manhã")
        self.assertEqual(self.last(msgs), "Vou definir 21 °C no Quarto entre as 7h e as 9h. Confirmas?")
        self.assertEqual(self.mem.prefs.get_active(), [])  # nothing stored yet
        self.chat.handle("sim")
        [p] = self.mem.prefs.get_active()
        self.assertEqual((p["key"], p["scope"], p["source"]), ("setpoint_c", f"room:{QUARTO}", "explicit"))
        self.assertEqual(p["original_text"], "Quero o quarto a 21 °C de manhã")

    def test_cancel_stores_nothing(self):
        self.chat.handle("Quero o quarto a 21 °C de manhã")
        self.chat.handle("não")
        self.assertEqual(self.mem.prefs.get_active(), [])

    def test_rule_creates_tt_scenario_through_the_coordinator(self):
        msgs = self.chat.handle("Se abrir a janela com o aquecedor ligado, avisa-me")
        self.assertIn("TT_janela_aberta_aquecedor_ligado", self.last(msgs))
        self.assertIn("Confirmas?", self.last(msgs))
        self.chat.action("confirm", id=msgs[-1]["confirm_id"])
        [rule] = self.mem.prefs.get_active(key="rule")
        self.assertTrue(rule["value"]["name"].startswith("TT_"))
        [dec] = self.mem.decisions.recent()
        self.assertEqual(dec["status"], "dry_run")
        self.assertEqual(dec["decision"]["tool"], "post_new_scenario")
        # local watcher: notify when the rule becomes true
        posted = []
        w = RuleWatcher(self.mem.prefs, self.memory, posted.append)
        w.on_event()
        self.home.set_contact(True)
        self.home.set_plug(True)
        self.refresh()
        self.assertTrue(rule_holds(rule["value"], self.memory.state))
        w.on_event()
        self.assertEqual(len(posted), 1)

    def test_rule_created_while_running_notifies_on_first_match(self):
        posted = []
        w = RuleWatcher(self.mem.prefs, self.memory, posted.append)  # watcher already running
        self.chat.handle("Se abrir a janela com o aquecedor ligado, avisa-me")
        self.chat.handle("sim")
        self.home.set_contact(True)
        self.home.set_plug(True)
        self.refresh()
        w.on_event()  # the window event itself: no event in between
        self.assertEqual(len(posted), 1)

    def test_security_always_asks(self):
        msgs = self.chat.handle("arma a casa")
        self.assertIn("Confirmas?", self.last(msgs))
        self.chat.handle("sim")
        self.assertEqual(self.mem.decisions.recent()[0]["decision"]["status"], "STAY")


class TestPendingAndLearning(ChatCase):
    def propose(self):
        self.home.set_contact(True)
        self.home.set_plug(True)
        self.refresh()
        trig = Event(type="contact_opened", message="Janela da Sala aberta.", room="sala")
        self.coord.consider("Energia", AgentReply(resumo="Janela aberta com o aquecedor ligado.",
                                                  propostas=[Proposal(device_id=PLUG, acao="off", motivo="desperdício")]), [trig])
        return self.mem.decisions.pending()[-1]

    def test_pending_decision_in_chat_then_three_approvals_ask_to_learn(self):
        for i in range(3):
            rec = self.propose()
            note = self.chat.store.after(0)[-1]
            self.assertEqual(note["decision_id"], rec["id"])
            self.assertEqual([b["label"] for b in note["buttons"]], ["Aprovar", "Rejeitar", "Alterar"])
            msgs = self.chat.action("decision", id=rec["id"], response="approved")
            self.assertEqual(self.mem.decisions.get(rec["id"])["user_response"], "approved")
            self.home.set_plug(True)  # the heater comes back on for the next round
            self.refresh()
        self.assertIn("Queres que passe a fazer isto sozinha?", self.last(msgs))
        self.assertEqual(self.mem.prefs.get_active(key="autonomy"), [])  # nothing without a "sim"
        self.chat.handle("sim")
        [p] = self.mem.prefs.get_active(key="autonomy")
        self.assertEqual((p["source"], p["value"]["level"]), ("learned_confirmed", "auto"))
        # next time the same decision runs on its own
        before = len(self.mem.decisions.pending())
        self.propose_auto = self.propose
        self.home.set_plug(True)
        self.refresh()
        self.coord.consider("Energia", AgentReply(resumo="Outra vez.", propostas=[Proposal(device_id=PLUG, acao="off")]),
                            [Event(type="contact_opened", message="Janela da Sala aberta.")])
        self.assertEqual(len(self.mem.decisions.pending()), before)
        self.assertEqual(self.mem.decisions.recent()[0]["status"], "dry_run")

    def test_declining_learning_is_remembered(self):
        for _ in range(3):
            rec = self.propose()
            msgs = self.chat.action("decision", id=rec["id"], response="approved")
            self.home.set_plug(True)
            self.refresh()
        self.chat.handle("não")
        self.assertEqual(self.mem.prefs.get_active(key="autonomy"), [])
        self.assertTrue(self.mem.prefs.muted(f"learn:{rec['kind']}"))

    def test_faz_sempre_assim(self):
        rec = self.propose()
        msgs = self.chat.handle("Faz sempre assim")
        self.assertEqual(self.mem.decisions.get(rec["id"])["user_response"], "approved")
        self.assertIn("Vou passar a fazer isto sozinha", self.last(msgs))
        self.assertEqual(self.mem.prefs.get_active(key="autonomy"), [])
        self.chat.handle("sim")
        [p] = self.mem.prefs.get_active(key="autonomy")
        self.assertEqual((p["value"]["kind"], p["value"]["level"]), (rec["kind"], "auto"))

    def test_reject_from_chat_text(self):
        rec = self.propose()
        self.chat.handle("não")
        got = self.mem.decisions.get(rec["id"])
        self.assertEqual((got["status"], got["user_response"]), ("rejected", "rejected"))


if __name__ == "__main__":
    unittest.main()
