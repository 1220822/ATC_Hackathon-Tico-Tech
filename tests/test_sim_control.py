"""The /simulador controls (src/sim/control.py): what scripts/simulate.py does, over HTTP."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import create_app
from src.config import get_settings
from src.memory import SharedMemory
from src.perception.sources import SimSource

HOUSE = {"simulated_rooms": []}  # the house as it is: Sala and Quarto, no simulated rooms
WITH_SIM_ROOM = {"simulated_rooms": [{"name": "Arrumos", "type": "storage", "devices": ["temperature", "contact", "plug"]}]}


class SimControlCase(unittest.TestCase):
    simulate = True
    sim_rooms = False
    config = HOUSE

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "home.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.s = get_settings().model_copy(update={
            "simulate": self.simulate, "sim_rooms": self.sim_rooms, "sim_state_file": tmp / "sim.json",
            "home_config_file": tmp / "home.json", "history_db": tmp / "history.db"})
        self.home = SimSource(self.s).home
        self.home.reset()
        self.client = TestClient(create_app(SharedMemory(), self.s))

    def tearDown(self):
        self.client.post("/sim/parar")
        self.tmp.cleanup()

    def room(self, view, name):
        return next(r for r in view["divisoes"] if r["nome"] == name)

    def dev(self, view, room, tipo):
        return next(d for d in self.room(view, room)["dispositivos"] if d["tipo"] == tipo)


class TestWholeHouse(SimControlCase):
    def test_page_is_served(self):
        r = self.client.get("/simulador")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/static/app/simulador.js", r.text)

    def test_contact_and_plug_in_a_named_room(self):
        v = self.client.post("/sim/acao", json={"acao": "abrir", "divisao": "sala"}).json()
        self.assertTrue(self.dev(v, "Sala", "contactSensor")["aberto"])
        v = self.client.post("/sim/acao", json={"acao": "ligar_tomada", "divisao": "Quarto", "valor": "1200"}).json()
        plug = self.dev(v, "Quarto", "plug")
        self.assertTrue(plug["ligada"])
        self.assertEqual(plug["potencia_w"], 1200)
        self.assertIn("Tomada ligada", v["registo"][0]["texto"])

    def test_only_the_rooms_of_the_house(self):
        self.assertEqual([r["nome"] for r in self.client.get("/sim").json()["divisoes"]], ["Sala", "Quarto"])

    def test_room_without_that_sensor_is_a_400(self):
        r = self.client.post("/sim/acao", json={"acao": "ligar_tomada", "divisao": "Sala"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("tomada", r.json()["detail"])

    def test_temperature_accepts_a_comma_and_checks_the_range(self):
        v = self.client.post("/sim/acao", json={"acao": "temperatura", "divisao": "Quarto", "valor": "18,4"}).json()
        self.assertEqual(self.dev(v, "Quarto", "temperatureSensor")["temperatura_c"], 18.4)
        r = self.client.post("/sim/acao", json={"acao": "temperatura", "divisao": "Quarto", "valor": 99})
        self.assertEqual(r.status_code, 400)

    def test_unknown_room_is_a_400(self):
        r = self.client.post("/sim/acao", json={"acao": "abrir", "divisao": "Garagem"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Garagem", r.json()["detail"])

    def test_security_and_alarm(self):
        v = self.client.post("/sim/acao", json={"acao": "armar", "valor": "away"}).json()
        self.assertEqual(v["seguranca"]["modo"], "AWAY")
        v = self.client.post("/sim/acao", json={"acao": "fumo"}).json()
        self.assertTrue(v["seguranca"]["alarme"])
        v = self.client.post("/sim/acao", json={"acao": "cancelar_alarme"}).json()
        self.assertFalse(v["seguranca"]["alarme"])

    def test_scenario_runs_in_the_background_and_logs_each_step(self):
        r = self.client.post("/sim/cenario", json={"cenario": "objetivo", "passo_s": 0.5})
        self.assertEqual(r.status_code, 200)
        for _ in range(40):
            v = self.client.get("/sim").json()
            if v["a_correr"] is None:
                break
            time.sleep(0.05)
        self.assertIsNone(v["a_correr"])
        textos = [r["texto"] for r in v["registo"]]
        self.assertIn("Cenário terminado", textos)
        self.assertEqual(self.dev(v, "Quarto", "temperatureSensor")["temperatura_c"], 19.0)
        self.assertFalse(self.dev(v, "Quarto", "plug")["ligada"])

    def test_window_scenario_uses_the_sala_window_and_the_quarto_heater(self):
        scen = {c["id"]: c for c in self.client.get("/sim").json()["cenarios"]}
        self.assertEqual({o["sensor"]: o["divisao"] for o in scen["janela"]["onde"]},
                         {"contacto": "Sala", "temperatura": "Quarto", "tomada": "Quarto"})
        v = self.client.post("/sim/cenario", json={"cenario": "janela", "passo_s": 0.5}).json()
        self.assertEqual(v["a_correr"]["divisao"], "Sala e Quarto")
        for _ in range(100):
            v = self.client.get("/sim").json()
            if v["a_correr"] is None:
                break
            time.sleep(0.05)
        self.assertTrue(self.dev(v, "Sala", "contactSensor")["aberto"])
        self.assertTrue(self.dev(v, "Quarto", "plug")["ligada"])
        self.assertEqual(self.dev(v, "Quarto", "temperatureSensor")["temperatura_c"], 20.3)

    def test_scenario_can_be_stopped_and_only_one_runs(self):
        self.client.post("/sim/cenario", json={"cenario": "janela", "passo_s": 5})
        r = self.client.post("/sim/cenario", json={"cenario": "objetivo"})
        self.assertEqual(r.status_code, 400)
        v = self.client.post("/sim/parar").json()
        self.assertIsNone(v["a_correr"])
        self.assertIn("Cenário parado", [x["texto"] for x in v["registo"]])

    def test_reset(self):
        self.client.post("/sim/acao", json={"acao": "abrir", "divisao": "Sala"})
        v = self.client.post("/sim/acao", json={"acao": "repor"}).json()
        self.assertFalse(self.dev(v, "Sala", "contactSensor")["aberto"])


class TestRealHouse(SimControlCase):
    """SIMULATE=false with SIM_ROOMS=true: only the simulated rooms, never the hub."""
    simulate = False
    sim_rooms = True
    config = WITH_SIM_ROOM

    def test_only_simulated_rooms_are_listed_and_changed(self):
        v = self.client.get("/sim").json()
        self.assertEqual([r["nome"] for r in v["divisoes"]], ["Arrumos"])
        self.assertIsNone(v["seguranca"])
        r = self.client.post("/sim/acao", json={"acao": "abrir", "divisao": "Sala"})
        self.assertEqual(r.status_code, 400)
        v = self.client.post("/sim/acao", json={"acao": "abrir"}).json()  # default: the simulated room
        self.assertTrue(self.dev(v, "Arrumos", "contactSensor")["aberto"])

    def test_alarm_actions_and_global_scenarios_are_refused(self):
        self.assertEqual(self.client.post("/sim/acao", json={"acao": "fumo"}).status_code, 400)
        self.assertEqual(self.client.post("/sim/acao", json={"acao": "armar", "valor": "away"}).status_code, 400)
        self.assertEqual(self.client.post("/sim/cenario", json={"cenario": "intrusao"}).status_code, 400)
        scen = {c["id"]: c for c in self.client.get("/sim").json()["cenarios"]}
        self.assertFalse(scen["fumo"]["disponivel"])
        self.assertEqual({o["divisao"] for o in scen["janela"]["onde"]}, {"Arrumos"})


class TestNoSimulation(SimControlCase):
    simulate = False
    sim_rooms = False

    def test_everything_is_refused(self):
        v = self.client.get("/sim").json()
        self.assertFalse(v["disponivel"])
        self.assertIn("--sim", v["motivo"])
        self.assertEqual(self.client.post("/sim/acao", json={"acao": "abrir"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
