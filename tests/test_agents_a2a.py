import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from agent_framework.a2a import A2AAgent
from fastapi.testclient import TestClient

from src.agents.specs import AGENTS, BadReply, instructions_for, parse_reply
from src.api import create_app
from src.config import get_settings
from src.memory import SharedMemory
from src.perception.sources import SimSource
from src.perception.state import normalize
from src.storage.history import History
from tests.agents_support import A2AServer, FakeChat

PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
REPLY = json.dumps({"resumo": "Janela aberta na Sala com o aquecedor do Quarto ligado.",
                    "propostas": [{"device_id": PLUG, "acao": "off", "motivo": "evitar desperdício"}]},
                   ensure_ascii=False)


class TestParseReply(unittest.TestCase):
    def test_plain_json(self):
        r = parse_reply(REPLY)
        self.assertEqual(r.propostas[0].device_id, PLUG)
        self.assertEqual(r.propostas[0].acao, "off")

    def test_tolerates_fences_think_and_text(self):
        text = f"<think>a pensar {{nada}}</think>Aqui vai:\n```json\n{REPLY}\n```\nObrigado."
        self.assertEqual(parse_reply(text).resumo, "Janela aberta na Sala com o aquecedor do Quarto ligado.")
        self.assertEqual(parse_reply(f"Resposta: {REPLY} fim").propostas[0].acao, "off")

    def test_empty_proposals(self):
        self.assertEqual(parse_reply('{"resumo": "Tudo normal."}').propostas, [])

    def test_rejects_bad_replies(self):
        for bad in ("sem json nenhum", '{"resumo": ""}', '{"resumo": "x", "propostas": [{"device_id": "a", "acao": "arm"}]}',
                    '{"propostas": []}'):
            with self.subTest(bad=bad), self.assertRaises(BadReply):
                parse_reply(bad)

    def test_instructions_are_pt_and_forbid_security(self):
        for spec in AGENTS.values():
            text = instructions_for(spec)
            self.assertIn("PT-PT", text)
            self.assertIn("Nunca proponhas armar ou desarmar", text)


class TestA2AServer(unittest.TestCase):
    """Real A2A over HTTP with a fake chat client: no LLM involved."""

    def test_agent_card(self):
        with A2AServer("energia", FakeChat()) as srv:
            card = httpx.get(srv.url + ".well-known/agent-card.json", timeout=5).json()
        self.assertEqual(card["name"], "Energia")
        iface = card["supportedInterfaces"][0]
        self.assertEqual((iface["url"], iface["protocolBinding"]), (srv.url, "JSONRPC"))
        self.assertEqual(iface["protocolVersion"], "1.0")

    def test_round_trip(self):
        chat = FakeChat(REPLY)
        with A2AServer("energia", chat) as srv:
            r = asyncio.run(A2AAgent(name="Energia", url=srv.url, timeout=20).run("Evento de teste"))
        self.assertEqual(parse_reply(r.text).propostas[0].device_id, PLUG)
        self.assertIn("Evento de teste", chat.prompts[0])
        self.assertIn("agente de Energia", chat.prompts[0])  # instructions reached the model


    def test_reasoning_effort_reaches_the_model(self):
        from src.agents.server import chat_options
        s = get_settings().model_copy(update={"llm_reasoning_effort": "none"})
        chat = FakeChat()
        with A2AServer("conforto", chat, options=chat_options(s)) as srv:
            asyncio.run(A2AAgent(name="Conforto", url=srv.url, timeout=20).run("x"))
        self.assertEqual(chat.options[0]["reasoning_effort"], "none")
        self.assertEqual(chat.options[0]["temperature"], 0.2)
        self.assertNotIn("reasoning_effort", chat_options(s.model_copy(update={"llm_reasoning_effort": ""})))


class TestToolEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "simulate": True, "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "none.json",
            "history_db": tmp / "h.db"})
        src = SimSource(self.s)
        src.home.reset()
        self.memory = SharedMemory()
        self.memory.update_state(normalize(src.fetch(), self.s))
        self.client = TestClient(create_app(self.memory, self.s))
        History(self.s.history_db).record(self.memory.state)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_tendencia(self):
        r = self.client.get("/tools/tendencia_temperatura", params={"divisao": "Quarto"}).json()
        self.assertEqual(r["divisao_id"], QUARTO)
        self.assertEqual(r["atual_c"], 21.0)

    def test_consumo(self):
        r = self.client.get("/tools/consumo_por_hora", params={"horas": 2}).json()
        self.assertIn("Quarto", r["divisoes"])

    def test_unknown_room_is_400(self):
        self.assertEqual(self.client.get("/tools/tendencia_temperatura", params={"divisao": "Garagem"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()


class TestAgentsProcess(unittest.TestCase):
    def test_exits_when_parent_dies(self):
        import os
        import subprocess
        import sys
        import time

        from src.agents.__main__ import port_in_use
        from tests.agents_support import free_port
        base = free_port() + 10  # three ports above a free one
        env = {**os.environ, "AGENTS_BASE_PORT": str(base), "PYTHONIOENCODING": "utf-8"}
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(4)"])
        agents = subprocess.Popen([sys.executable, "-m", "src.agents", "--parent-pid", str(parent.pid)],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            t0 = time.time()
            while not port_in_use("127.0.0.1", base + 2) and time.time() - t0 < 20:
                time.sleep(0.2)
            self.assertTrue(port_in_use("127.0.0.1", base + 2))  # Energia is up
            parent.wait(10)
            self.assertIsNotNone(agents.wait(15))  # and goes away with its parent
        finally:
            for p in (parent, agents):
                if p.poll() is None:
                    p.kill()
