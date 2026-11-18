"""'O que a casa sabe de ti': view, edit (new version), revoke (rule -> scenario off), erase all."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import create_app
from src.chat.service import ChatService
from src.config import get_settings
from src.context import Memories
from src.coordinator import Coordinator
from src.memory import SharedMemory
from src.perception.state import normalize
from src.sim.home import SimHome

QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"


class TestKnowledgeApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "none.json",
            "history_db": tmp / "home.db", "dry_run": True, "simulate": True, "vision_enabled": False})
        home = SimHome(self.s.sim_state_file)
        home.reset()
        self.memory = SharedMemory()
        self.memory.update_state(normalize(home.snapshot(), self.s))
        self.mem = Memories.open(self.s.history_db)
        self.coord = Coordinator(self.memory, self.s, memories=self.mem)
        self.chat = ChatService(self.memory, self.s, self.coord, self.mem, llm=None)
        self.client = TestClient(create_app(self.memory, self.s, self.coord, chat=self.chat))

    def tearDown(self):
        self.tmp.cleanup()

    def test_view_edit_revoke_erase(self):
        c = self.client
        c.post("/chat", json={"text": "Quero o quarto a 21 °C de manhã"})
        c.post("/chat", json={"text": "sim"})
        c.post("/chat", json={"text": "Se abrir a janela com o aquecedor ligado, avisa-me"})
        c.post("/chat", json={"text": "sim"})
        k = c.get("/knowledge").json()
        [pref] = k["preferencias"]
        self.assertEqual(pref["frase"], "Quero o quarto a 21 °C de manhã")
        self.assertEqual(pref["ambito"], "Quarto")
        [rule] = k["regras"]
        self.assertIn("TT_", rule["descricao"])
        # edit -> new version, the old one stays in the history
        new = c.post(f"/knowledge/prefs/{pref['id']}/edit", json={"text": "20"}).json()
        self.assertEqual(new["value"]["c"], 20)
        self.assertEqual(new["value"]["periods"][0]["c"], 21)  # periods kept
        hist = c.get("/knowledge").json()["historico"]
        self.assertEqual(len([h for h in hist if h["tipo"] == "setpoint_c"]), 2)
        self.assertEqual(c.post(f"/knowledge/prefs/{pref['id']}/edit", json={"text": "x"}).status_code, 400)
        # revoke the rule
        c.post(f"/knowledge/prefs/{rule['id']}/revoke")
        self.assertEqual(c.get("/knowledge").json()["regras"], [])
        # erase everything (needs the word)
        self.assertEqual(c.post("/knowledge/erase", json={"confirm": "sim"}).status_code, 400)
        out = c.post("/knowledge/erase", json={"confirm": "APAGAR"}).json()
        self.assertGreaterEqual(out["preferencias"], 3)
        k = c.get("/knowledge").json()
        self.assertEqual((k["preferencias"], k["regras"], k["historico"]), ([], [], []))


if __name__ == "__main__":
    unittest.main()
