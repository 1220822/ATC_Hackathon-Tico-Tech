"""3D layout by natural language, by hand and undo (stored as layout preferences with history)."""

import tempfile
import unittest
from pathlib import Path

from src.chat.intents import LayoutRequest
from src.chat.service import ChatService
from src.config import ROOT, get_settings
from src.context import Memories
from src.coordinator import Coordinator
from src.memory import SharedMemory
from src.perception.state import normalize
from src.sim.home import SimHome
from src.twin.edit import LayoutEditor, LayoutProblem, active_layout_file
from src.twin.state import twin_state

SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
TEMP = "54e1ea8e-9ca3-4933-a4dc-a1eb37e6990c"


class LayoutCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "none.json", "history_db": tmp / "home.db",
            "layout_file": ROOT / "config" / "layout.json", "dry_run": True, "simulate": True})
        home = SimHome(self.s.sim_state_file)
        home.reset()
        self.memory = SharedMemory()
        self.memory.update_state(normalize(home.snapshot(), self.s))
        self.mem = Memories.open(self.s.history_db)
        self.ed = LayoutEditor(self.memory, self.s, self.mem.prefs)
        self.chat = ChatService(self.memory, self.s, Coordinator(self.memory, self.s, memories=self.mem), self.mem)
        self.chat.layout = self.ed

    def tearDown(self):
        self.tmp.cleanup()

    def room(self, rid):
        return self.ed.current().rooms[rid]


class TestNaturalLanguage(LayoutCase):
    def test_room_to_the_right_then_ok(self):
        before = self.ed.current()
        msgs = self.chat.handle("O quarto fica à frente da sala")
        self.assertIn("Ficou bem?", msgs[-1]["text"])
        sala, quarto = self.room(SALA), self.room(QUARTO)
        self.assertEqual((quarto.x, quarto.z), (sala.x, sala.z + sala.depth))
        self.assertNotEqual(before.rooms[QUARTO].z, quarto.z)
        self.assertEqual(active_layout_file(self.s).name, "layout_user.json")  # config/layout.json untouched
        [pref] = self.mem.prefs.get_active(key="layout")
        self.assertEqual(pref["original_text"], "O quarto fica à frente da sala")
        self.assertEqual(self.chat.handle("sim")[-1]["text"], "Ótimo, fica assim. Está guardado no histórico do 3D.")

    def test_not_ok_offers_manual_or_rephrase(self):
        self.chat.handle("O quarto fica à frente da sala")
        msgs = self.chat.handle("não")
        labels = [b["label"] for b in msgs[-1]["buttons"]]
        self.assertEqual(labels[:2], ["Ajustar à mão", "Descrever de outra forma"])

    def test_sensor_next_to_the_window_adds_one_if_missing(self):
        before, after, what = self.ed.apply(LayoutRequest(alvo="sensor de temperatura", relacao="junto_janela"),
                                            self.memory.state)
        quarto = after.rooms[QUARTO]
        windows = [o for w in quarto.walls for o in w.openings if o.type == "window"]
        self.assertEqual(len(windows), 1)
        self.assertIn("Acrescentei uma janela", what)
        x, y, z = after.devices[TEMP].position
        self.assertLess(z, 0.2)  # on the back wall, next to the new window
        self.assertLess(abs(x - windows[0].offset), 1.5)

    def test_overlap_is_refused(self):
        with self.assertRaises(LayoutProblem):
            self.ed.apply(LayoutRequest(alvo="Sala", relacao="direita", referencia="Sala"), self.memory.state)

    def test_device_is_never_taken_from_another_room(self):
        # the smoke detector is in the Sala: «põe-no no quarto» must not move the one in the Sala instead
        before = self.ed.current()
        with self.assertRaises(LayoutProblem) as e:
            self.ed.apply(LayoutRequest(alvo="detetor de fumo", relacao="centro", referencia="Quarto"),
                          self.memory.state)
        self.assertIn("na Sala, não no Quarto", str(e.exception))
        self.assertEqual(self.ed.current(), before)

    def test_layout_requests_always_go_through_the_llm_thinking(self):
        from src.chat.intents import ChatIntent, ChatPlan
        calls = []

        class LLM:
            def structured(self, system, user, schema, reasoning_effort=None):
                calls.append(reasoning_effort)
                lay = LayoutRequest(alvo="detetor de fumo", relacao="centro", referencia="Quarto")
                intent = ChatIntent(intent="configurar_layout", divisao="Quarto", layout=lay)
                return ChatPlan(pedidos=[intent]) if schema is ChatPlan else intent

        self.chat.llm = LLM()
        self.chat.s = self.s.model_copy(update={"chat_reasoning_effort": "medium"})
        msgs = self.chat.handle("move o detetor de fumo para dentro do quarto")
        self.assertIn("medium", calls)  # understood on the fast path, and still thought over
        self.assertIn("está na Sala, não no Quarto", msgs[-1]["text"])
        self.assertNotIn("Ficou bem?", msgs[-1]["text"])

    def test_twin_uses_the_user_layout(self):
        self.chat.handle("O quarto fica à frente da sala")
        st = twin_state(self.memory, self.s)
        quarto = next(r for r in st["rooms"] if r["id"] == QUARTO)
        self.assertEqual(quarto["z"], self.room(SALA).depth)


class TestWindows(LayoutCase):
    CONTACT = "20d98aec-c266-4707-bd27-d06bb51733b7"

    def openings(self, rid, kind="window"):
        return [(w.side, o) for w in self.room(rid).walls for o in w.openings if o.type == kind]

    def test_add_window_on_a_wall_by_chat(self):
        msgs = self.chat.handle("Acrescenta uma janela na parede da direita do quarto")
        self.assertIn("Acrescentei uma janela de 1,2 m na parede da direita do Quarto", msgs[-1]["text"])
        self.assertIn("Ficou bem?", msgs[-1]["text"])
        [(side, o)] = self.openings(QUARTO)
        self.assertEqual(side, "east")
        self.assertEqual((o.width, o.sill), (1.2, 0.9))
        self.assertEqual(len(self.mem.prefs.history("layout")), 1)  # in the layout history

    def test_second_window_does_not_overlap(self):
        self.chat.handle("Acrescenta uma janela na parede de trás do quarto")
        self.chat.handle("Acrescenta outra janela na parede de trás do quarto")
        ws = [o for _, o in self.openings(QUARTO)]
        self.assertEqual(len(ws), 2)
        a, b = sorted(ws, key=lambda o: o.offset)
        self.assertGreaterEqual(b.offset - b.width / 2, a.offset + a.width / 2)
        self.assertNotEqual(a.id, b.id)

    def test_resize_and_move_window_sensor_follows(self):
        self.chat.handle("A janela da sala tem 1,6 m de largura")
        [(_, o)] = self.openings(SALA)
        self.assertEqual(o.width, 1.6)
        msgs = self.chat.handle("Passa a janela da sala para a parede da esquerda")
        self.assertIn("passou para a parede da esquerda", msgs[-1]["text"])
        self.assertIn("O sensor da janela foi com ela", msgs[-1]["text"])
        [(side, o)] = self.openings(SALA)
        self.assertEqual(side, "west")
        sensor = self.ed.current().devices[self.CONTACT]
        self.assertEqual(sensor.opening, o.id)
        self.assertLess(sensor.position[0], 0.2)  # now on the west wall

    def test_window_with_sensor_cannot_be_removed(self):
        msgs = self.chat.handle("Remove a janela da sala")
        self.assertIn("Não consegui mudar a janela", msgs[-1]["text"])
        self.assertEqual(len(self.openings(SALA)), 1)

    def test_add_then_remove_and_undo(self):
        self.chat.handle("Acrescenta uma janela no quarto")
        self.assertEqual(len(self.openings(QUARTO)), 1)
        self.chat.handle("Remove a janela do quarto")
        self.assertEqual(self.openings(QUARTO), [])
        self.ed.undo()
        self.assertEqual(len(self.openings(QUARTO)), 1)

    def test_too_wide_is_refused(self):
        msgs = self.chat.handle("A janela da sala tem 6 m de largura")
        self.assertIn("não cabe", msgs[-1]["text"])

    def test_doors_too(self):
        before = len(self.openings(QUARTO, "door"))  # the Quarto already has its front door
        self.chat.handle("Quero uma porta nova na parede de trás do quarto")
        doors = self.openings(QUARTO, "door")
        self.assertEqual(len(doors), before + 1)
        [(side, o)] = [d for d in doors if d[0] == "north"]
        self.assertEqual((o.sill, o.height), (0.0, 2.1))


class TestManualAndUndo(LayoutCase):
    def test_manual_then_undo(self):
        orig = self.room(QUARTO)
        self.ed.manual({QUARTO: {"x": orig.x + 1, "z": orig.z}}, {TEMP: {"position": [orig.x + 1 + 2.0, 1.3, 1.0]}})
        moved = self.room(QUARTO)
        self.assertEqual(moved.x, orig.x + 1)
        self.assertEqual(self.ed.current().devices[TEMP].position, (2.0, 1.3, 1.0))  # relative to the new corner
        self.assertEqual(len(self.mem.prefs.history("layout")), 1)
        self.assertIn("Ajuste à mão", self.ed.undo())
        self.assertEqual(self.room(QUARTO).x, orig.x)
        self.assertEqual(len(self.mem.prefs.history("layout")), 2)  # the undo is in the history too

    def test_manual_overlap_refused(self):
        sala = self.room(SALA)
        with self.assertRaises(LayoutProblem):
            self.ed.manual({QUARTO: {"x": sala.x, "z": sala.z}}, {})


if __name__ == "__main__":
    unittest.main()
