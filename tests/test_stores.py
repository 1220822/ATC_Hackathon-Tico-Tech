import tempfile
import unittest
from pathlib import Path

from src.config import get_settings
from src.coordinator import Coordinator, UserRequest
from src.memory import SharedMemory
from src.perception.state import Event, normalize
from src.sim.home import SimHome
from src.storage.decisions import DecisionStore, decision_kind
from src.storage.events import EventStore

QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "home.db"

    def tearDown(self):
        self.tmp.cleanup()


class TestEventStore(StoreCase):
    def test_events_are_stored_without_ticks(self):
        store = EventStore(self.db)
        e = Event(type="contact_opened", message="Janela da Sala aberta.", room="sala", room_name="Sala",
                  device_id="c1", data={"open": True})
        store.on_event(e)
        store.on_event(e)  # same id: stored once
        store.on_event(Event(type="tick", message="Verificação periódica."))
        rows = store.recent()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data"], {"open": True})
        self.assertEqual(rows[0]["room_name"], "Sala")
        self.assertEqual(store.recent(room="quarto"), [])

    def test_only_the_writer_stores(self):
        store = EventStore(self.db, is_writer=lambda: False)
        store.on_event(Event(type="contact_opened", message="x"))
        self.assertEqual(store.count(), 0)


class TestDecisionStore(StoreCase):
    def test_add_respond_and_streak(self):
        store = DecisionStore(self.db)
        kind = decision_kind("contact_opened", PLUG, {"type": "power", "value": "off"})
        ids = []
        for i in range(4):
            d = store.add(trigger={"type": "contact_opened"}, proposals=[{"agent": "Energia"}],
                          decision={"device_id": PLUG}, explanation="x", status="pending", kind=kind, ts=100 + i)
            ids.append(d["id"])
        self.assertEqual(len(store.pending()), 4)
        store.respond(ids[0], "rejected", now=200)
        for i, id_ in enumerate(ids[1:]):
            store.respond(id_, "approved", now=201 + i)
        self.assertEqual(store.approval_streak(kind), 3)
        self.assertEqual(store.get(ids[1])["proposals"], [{"agent": "Energia"}])
        self.assertEqual([d["id"] for d in store.similar(kind)], ids[::-1][:3])
        with self.assertRaises(ValueError):
            store.add(trigger=None, proposals=None, decision=None, explanation="", status="talvez")


class TestCoordinatorPersists(StoreCase):
    def test_user_request_is_recorded(self):
        s = get_settings().model_copy(update={"sim_state_file": Path(self.tmp.name) / "sim.json",
                                              "home_config_file": Path(self.tmp.name) / "none.json",
                                              "dry_run": True})
        home = SimHome(s.sim_state_file)
        home.reset()
        mem = SharedMemory()
        mem.update_state(normalize(home.snapshot(), s))
        store = DecisionStore(self.db)
        Coordinator(mem, s, decisions=store).request(UserRequest(device_id=PLUG, action="on"))
        [d] = store.recent()
        self.assertEqual(d["status"], "dry_run")
        self.assertEqual(d["room"], QUARTO)
        self.assertEqual(d["kind"], f"user_request|{PLUG}|power=on")


if __name__ == "__main__":
    unittest.main()
