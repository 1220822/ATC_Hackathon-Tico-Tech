"""Insight detectors on SYNTHETIC series (tests only) and the anti-micromanagement filter."""

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.chat.service import ChatService
from src.config import get_settings
from src.context import Memories
from src.coordinator import Coordinator
from src.insights.detectors import daily_patterns, run_all, window_heater
from src.insights.service import InsightService, faithful
from src.llm import LLM
from src.memory import SharedMemory
from src.perception.state import normalize
from src.sim.home import SimHome

QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"
TEMP = "54e1ea8e-9ca3-4933-a4dc-a1eb37e6990c"
PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"
CONTACT = "20d98aec-c266-4707-bd27-d06bb51733b7"


class InsightCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "none.json",
            "history_db": tmp / "home.db", "dry_run": True, "simulate": True, "store_interval_s": 300})
        home = SimHome(self.s.sim_state_file)
        home.reset()
        self.state = normalize(home.snapshot(), self.s)
        self.memory = SharedMemory()
        self.memory.update_state(self.state)
        self.mem = Memories.open(self.s.history_db)
        # "now" = today at 12:00, so last night (22:00 -> 07:00) is complete
        self.now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()

    def tearDown(self):
        self.tmp.cleanup()

    def night(self, days_ago: int, humid: float, step_s: int = 300):
        """Synthetic night in the Quarto: humidity `humid` from 22:00 to 07:00, 60 % outside it."""
        d0 = datetime.fromtimestamp(self.now).replace(hour=0) - timedelta(days=days_ago)
        rows = []
        t = (d0 - timedelta(hours=4)).timestamp()
        end = (d0 + timedelta(hours=11)).timestamp()
        while t < end:
            h = datetime.fromtimestamp(t).hour
            v = humid if (h >= 22 or h < 7) else 60.0
            rows += [(t, QUARTO, TEMP, "humidity", v), (t, QUARTO, TEMP, "temperature", 21.0)]
            t += step_s
        self.mem.history.insert(rows)


class TestDetectors(InsightCase):
    def test_humid_nights_on_two_days(self):
        self.night(1, 78)  # night that started two days ago... (22:00 of day-2 -> 07:00 of day-1)
        self.night(0, 76)
        found = daily_patterns(self.mem.history, self.state, self.mem.prefs, self.now, min_days=2, store_interval_s=300)
        hum = [i for i in found if i.key.endswith("humidity_high:noite")]
        self.assertEqual(len(hum), 1)
        ins = hum[0]
        self.assertEqual(len(ins.evidence.days), 2)
        self.assertIn("2 noites", ins.text)
        self.assertNotIn("todas as noites", ins.text)
        self.assertEqual(ins.proposed_action.missing_device, "desumidificador")  # honest product note
        self.assertIn("Não tens desumidificador", ins.text)

    def test_one_night_is_not_a_pattern(self):
        self.night(0, 80)
        found = daily_patterns(self.mem.history, self.state, self.mem.prefs, self.now, min_days=2, store_interval_s=300)
        self.assertEqual([i for i in found if "humidity" in i.key], [])

    def test_threshold_follows_the_preference(self):
        self.night(1, 68)
        self.night(0, 68)
        self.assertEqual(daily_patterns(self.mem.history, self.state, self.mem.prefs, self.now, 2, store_interval_s=300), [])
        self.mem.prefs.set(f"room:{QUARTO}", "humidity_max_pct", {"pct": 65})
        found = daily_patterns(self.mem.history, self.state, self.mem.prefs, self.now, 2, store_interval_s=300)
        self.assertTrue(any("humidity_high" in i.key for i in found))

    def test_window_open_with_heater_on(self):
        rows = []
        for days_ago in (2, 1):
            t0 = self.now - days_ago * 86400
            for k in range(0, 3600, 30):
                rows += [(t0 + k, SALA, CONTACT, "open", 1.0), (t0 + k, QUARTO, PLUG, "on", 1.0),
                         (t0 + k, QUARTO, PLUG, "power_w", 1000.0)]
        self.mem.history.insert(rows)
        [ins] = window_heater(self.mem.history, self.state, self.now, min_days=2)
        self.assertEqual(ins.agent, "energia")
        self.assertIn("Em 2 dias", ins.text)
        self.assertEqual(ins.proposed_action.rule["acoes"][0]["acao"], "desligar")
        self.assertIn("1,97 kWh", ins.text)  # 1000 W x (1 h - 30 s) x 2 days, trapezoidal


class TestFaithfulPhrase(unittest.TestCase):
    T = "A humidade no Quarto passou de 70 % nas últimas 2 noites (máximos de 78 %, 76 %). Queres que te avise?"

    def test_rules(self):
        self.assertTrue(faithful("Nas últimas 2 noites, a humidade no Quarto passou de 70 % (78 % e 76 %). Aviso-te?", self.T))
        self.assertFalse(faithful("Todas as noites a humidade passa de 70 %. Aviso-te?", self.T))
        self.assertFalse(faithful("Nas últimas 5 noites a humidade passou de 70 %. Aviso-te?", self.T))
        self.assertFalse(faithful("A humidade passou de 70 % nas últimas 2 noites.", self.T))  # lost the question


class TestAntiSpam(InsightCase):
    def setUp(self):
        super().setUp()
        coord = Coordinator(self.memory, self.s, memories=self.mem)
        self.chat = ChatService(self.memory, self.s, coord, self.mem, llm=None)
        self.svc = InsightService(self.memory, self.s, coord, self.mem, llm=None, min_days=2, max_per_day=2)
        self.svc.chat, self.chat.insights = self.chat, self.svc
        self.night(1, 78)
        self.night(0, 76)
        rows = []
        for days_ago in (2, 1):
            t0 = self.now - days_ago * 86400
            for k in range(0, 3600, 30):
                rows += [(t0 + k, SALA, CONTACT, "open", 1.0), (t0 + k, QUARTO, PLUG, "on", 1.0)]
        self.mem.history.insert(rows)

    def test_daily_limit_and_no_repeat(self):
        first = self.svc.run(now=self.now)
        self.assertEqual(len(first), 2)  # humidity + window/heater, at most 2 per day
        self.assertEqual(self.svc.run(now=self.now + 60), [])  # limit reached, and already shown

    def test_quiet_hours(self):
        self.mem.prefs.set("global", "quiet_hours", {"start": "00:00", "end": "23:59"})
        self.assertEqual(self.svc.run(now=self.now), [])
        self.assertEqual(len(self.svc.on_request(chat=self.chat)), 1)  # asked for: allowed

    def test_reject_waits_seven_days_and_mute_is_forever(self):
        [a, b] = self.svc.run(now=self.now)
        self.svc.respond(a["suggestion_id"], "reject", chat=self.chat)
        self.svc.respond(b["suggestion_id"], "mute", chat=self.chat)
        self.assertTrue(self.mem.prefs.get_active(key="suggestion_muted"))
        later = self.now + 3 * 86400
        self.assertEqual(self.svc.run(now=later), [])
        self.assertEqual(len(self.svc.run(now=self.now + 8 * 86400)), 1)  # only the rejected one returns

    def test_accept_needs_confirmation_then_creates_the_rule(self):
        [a, _] = self.svc.run(now=self.now)
        msgs = self.svc.respond(a["suggestion_id"], "accept", chat=self.chat)
        self.assertIn("Confirmas?", msgs[-1]["text"])
        self.assertEqual(self.mem.prefs.get_active(key="rule"), [])
        self.chat.handle("sim")
        [rule] = self.mem.prefs.get_active(key="rule")
        self.assertEqual(rule["source"], "learned_confirmed")
        self.assertEqual(self.mem.decisions.recent()[0]["decision"]["tool"], "post_new_scenario")

    def test_evidence_for_the_chart(self):
        [a, _] = self.svc.run(now=self.now)
        ev = self.svc.evidence(a["suggestion_id"])
        self.assertTrue(ev["series"][0]["points"])
        self.assertTrue(ev["janelas"])

    def test_llm_phrase_is_checked(self):
        self.svc.llm = LLM(complete=lambda m, s: '{"frase": "Todas as noites o quarto fica húmido. Aviso-te?"}')
        [a, _] = self.svc.run(now=self.now)
        self.assertNotIn("Todas as noites", a["text"])  # unfaithful rewrite -> template


if __name__ == "__main__":
    unittest.main()
