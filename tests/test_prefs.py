import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from src.prefs import PrefStore

QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"


class PrefCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = PrefStore(Path(self.tmp.name) / "home.db")

    def tearDown(self):
        self.tmp.cleanup()


class TestVersioning(PrefCase):
    def test_change_creates_new_row_and_deactivates_old(self):
        a = self.p.set(f"room:{QUARTO}", "setpoint_c", {"c": 20}, original_text="quarto a 20")
        b = self.p.set(f"room:{QUARTO}", "setpoint_c", {"c": 21}, original_text="quarto a 21")
        active = self.p.get_active(key="setpoint_c")
        self.assertEqual([r["id"] for r in active], [b["id"]])
        old = self.p.get(a["id"])
        self.assertFalse(old["active"])
        self.assertEqual(old["superseded_by"], b["id"])
        self.assertEqual(len(self.p.history("setpoint_c")), 2)  # nothing deleted

    def test_scopes_do_not_replace_each_other(self):
        self.p.set("global", "setpoint_c", {"c": 20})
        self.p.set(f"room:{QUARTO}", "setpoint_c", {"c": 22})
        self.assertEqual(len(self.p.get_active(key="setpoint_c")), 2)

    def test_autonomy_is_per_category(self):
        self.assertEqual(self.p.autonomy("energia"), "perguntar")  # default
        self.p.set("global", "autonomy", {"category": "energia", "level": "auto"})
        self.p.set("global", "autonomy", {"category": "luz", "level": "nunca"})
        self.p.set("global", "autonomy", {"category": "energia", "level": "perguntar"})
        self.assertEqual(self.p.autonomy("energia"), "perguntar")
        self.assertEqual(self.p.autonomy("luz"), "nunca")
        self.assertEqual(self.p.autonomy("conforto"), "perguntar")
        self.assertEqual(len(self.p.get_active(key="autonomy")), 2)

    def test_rules_and_notes_coexist(self):
        self.p.set("global", "note", {"text": "gosto de dormir fresco"})
        self.p.set("global", "note", {"text": "o gato fica sozinho às terças"})
        self.assertEqual(len(self.p.get_active(key="note")), 2)

    def test_revoke_keeps_the_row(self):
        r = self.p.set("global", "humidity_max_pct", {"pct": 65})
        self.p.revoke(r["id"])
        self.assertEqual(self.p.get_active(), [])
        self.assertFalse(self.p.get(r["id"])["active"])
        self.assertEqual(self.p.humidity_max(QUARTO), (None, None))


class TestValidation(PrefCase):
    def test_bad_values_are_refused(self):
        with self.assertRaises(ValidationError):
            self.p.set("global", "setpoint_c", {"c": 45})
        with self.assertRaises(ValidationError):
            self.p.set("global", "autonomy", {"category": "cozinha", "level": "auto"})
        with self.assertRaises(ValidationError):
            self.p.set("global", "comfort_range_c", {"min_c": 23, "max_c": 19})
        with self.assertRaises(ValidationError):
            self.p.set("global", "rule", {"name": "sem_prefixo", "description": "x", "triggers": [{}]})
        with self.assertRaises(ValueError):
            self.p.set("cozinha", "note", {"text": "x"})
        with self.assertRaises(ValueError):
            self.p.set("global", "inventado", {})

    def test_setpoint_periods(self):
        self.p.set(f"room:{QUARTO}", "setpoint_c", {"c": 19, "periods": [{"start": "7", "end": "09:00", "c": 21}]})
        self.assertEqual(self.p.setpoint(QUARTO, datetime(2026, 11, 20, 8, 0))[0], 21)
        self.assertEqual(self.p.setpoint(QUARTO, datetime(2026, 11, 20, 12, 0))[0], 19)

    def test_quiet_hours_cross_midnight(self):
        self.p.set("global", "quiet_hours", {"start": "23:00", "end": "07:30"})
        self.assertTrue(self.p.quiet_now(datetime(2026, 11, 20, 2, 0)))
        self.assertFalse(self.p.quiet_now(datetime(2026, 11, 20, 12, 0)))

    def test_applicable_filters_scopes(self):
        self.p.set("global", "note", {"text": "global"})
        self.p.set(f"room:{QUARTO}", "humidity_max_pct", {"pct": 65})
        self.p.set("room:sala", "humidity_max_pct", {"pct": 70})
        self.p.set("device:plug1", "note", {"text": "aquecedor velho"})
        got = {r["scope"] for r in self.p.applicable(QUARTO, ["plug1"])}
        self.assertEqual(got, {"global", f"room:{QUARTO}", "device:plug1"})


if __name__ == "__main__":
    unittest.main()
