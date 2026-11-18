import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from src.config import VAR_DIR, get_settings
from src.memory import SharedMemory
from src.vision.models import VisionObservation, format_vision_message, to_event, vision_severity


class TestVisionSettings(unittest.TestCase):
    def tearDown(self):
        get_settings.cache_clear()

    def test_vision_inherits_llm_defaults(self):
        get_settings.cache_clear()
        env = {
            "LLM_BASE_URL": "http://192.168.2.161:1234/v1",
            "LLM_MODEL": "qwen3.8:latest",
            "LLM_API_KEY": "team-key",
            "VISION_BASE_URL": "",
            "VISION_MODEL": "",
            "VISION_API_KEY": "",
            "VISION_SIM_IMAGE": "",
            "VISION_FRAMES_DIR": "",
        }
        with patch.dict(os.environ, env, clear=False):
            s = get_settings()
        self.assertTrue(s.vision_enabled)
        self.assertEqual(s.vision_base_url, "http://192.168.2.161:1234/v1")
        self.assertEqual(s.vision_model, "qwen3.8:latest")
        self.assertEqual(s.vision_api_key, "team-key")
        self.assertEqual(s.vision_timeout_s, 25.0)
        self.assertEqual(s.vision_cooldown_s, 10.0)
        self.assertEqual(s.vision_stay_cooldown_s, 30.0)
        self.assertFalse(s.vision_trigger_disarmed)
        self.assertIsNone(s.vision_sim_image)
        self.assertEqual(s.vision_max_frames, 50)
        self.assertEqual(s.vision_frames_dir, VAR_DIR / "frames")

    def test_vision_overrides_llm(self):
        get_settings.cache_clear()
        env = {
            "LLM_BASE_URL": "http://192.168.2.161:1234/v1",
            "LLM_MODEL": "qwen3.8:latest",
            "VISION_BASE_URL": "http://127.0.0.1:11434/v1",
            "VISION_MODEL": "gemma4:latest",
            "VISION_API_KEY": "vis-key",
            "VISION_TRIGGER_DISARMED": "true",
            "VISION_SIM_IMAGE": "var/sample.jpg",
            "VISION_MAX_FRAMES": "20",
        }
        with patch.dict(os.environ, env, clear=False):
            s = get_settings()
        self.assertEqual(s.vision_base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(s.vision_model, "gemma4:latest")
        self.assertEqual(s.vision_api_key, "vis-key")
        self.assertTrue(s.vision_trigger_disarmed)
        self.assertEqual(s.vision_sim_image, Path("var/sample.jpg"))
        self.assertEqual(s.vision_max_frames, 20)


class TestVisionModels(unittest.TestCase):
    def test_observation_is_frozen_and_coerces_objects(self):
        obs = VisionObservation(
            room_id="r-sala",
            room_name="Sala",
            camera_id="cam-1",
            trigger_type="camera_motion",
            context="intrusion",
            has_person=True,
            person_count=1,
            category="person",
            window_or_door_open=True,
            objects=["pessoa", "janela"],
            overall_confidence=0.9,
            scene_note_pt="Uma pessoa junto à janela aberta.",
        )
        self.assertEqual(obs.objects, ("pessoa", "janela"))
        with self.assertRaises(ValidationError):
            obs.has_person = False  # type: ignore[misc]

    def test_confidence_bounds(self):
        with self.assertRaises(ValidationError):
            VisionObservation(overall_confidence=1.5)
        with self.assertRaises(ValidationError):
            VisionObservation(overall_confidence=-0.1)

    def test_severity_rules(self):
        # Smoke visible is always critical
        self.assertEqual(
            vision_severity(VisionObservation(smoke_or_fire_visible=True, context="smoke"), armed=False),
            "critical",
        )
        # Person while armed -> critical
        self.assertEqual(
            vision_severity(VisionObservation(has_person=True, person_count=1, category="person"), armed=True),
            "critical",
        )
        # Person during smoke_detected even if disarmed -> critical
        self.assertEqual(
            vision_severity(
                VisionObservation(trigger_type="smoke_detected", has_person=True, person_count=1, category="person"),
                armed=False,
            ),
            "critical",
        )
        # Window open while armed -> warning
        self.assertEqual(
            vision_severity(
                VisionObservation(category="empty", window_or_door_open=True, trigger_type="camera_motion"),
                armed=True,
            ),
            "warning",
        )
        # Error while armed -> warning
        self.assertEqual(
            vision_severity(VisionObservation(status="timeout", trigger_type="camera_motion"), armed=True),
            "warning",
        )
        # Security alarm trigger -> at least warning (bypasses AgentBridge info cooldown)
        self.assertEqual(
            vision_severity(VisionObservation(category="empty", trigger_type="security_alarm"), armed=True),
            "warning",
        )
        # False alarm (pet/empty) on camera_motion -> info
        self.assertEqual(
            vision_severity(
                VisionObservation(category="pet", window_or_door_open=False, trigger_type="camera_motion"),
                armed=True,
            ),
            "info",
        )

    def test_to_event_and_message(self):
        obs = VisionObservation(
            room_id="r-sala",
            room_name="Sala",
            camera_id="cam-1",
            trigger_type="camera_motion",
            context="intrusion",
            has_person=True,
            person_count=1,
            category="person",
            window_or_door_open=True,
            overall_confidence=0.9,
        )
        ev = to_event(obs, armed=True)
        self.assertEqual(ev.type, "camera_analysis")
        self.assertEqual(ev.severity, "critical")
        self.assertEqual(ev.device_id, "cam-1")
        self.assertEqual(ev.role, "camera")
        self.assertEqual(ev.room, "r-sala")
        self.assertEqual(ev.room_name, "Sala")
        self.assertIn("da Sala", ev.message)
        self.assertIn("1 pessoa detetada", ev.message)
        self.assertIn("janela/porta aberta", ev.message)
        self.assertEqual(ev.data["overall_confidence"], 0.9)

        err_msg = format_vision_message(VisionObservation(room_name="Sala", status="capture_error"))
        self.assertIn("falha na captura", err_msg)


class TestSharedMemoryVision(unittest.TestCase):
    def test_set_and_lookup_latest_vision(self):
        mem = SharedMemory()
        self.assertIsNone(mem.latest_vision())
        obs_sala = VisionObservation(room_id="id-sala", room_name="Sala", has_person=True, person_count=1)
        obs_quarto = VisionObservation(room_id="id-quarto", room_name="Quarto", category="empty")
        mem.set_vision(obs_sala)
        mem.set_vision(obs_quarto)
        self.assertEqual(mem.latest_vision(), obs_quarto)
        self.assertEqual(mem.latest_vision("id-sala"), obs_sala)
        self.assertEqual(mem.latest_vision("sala"), obs_sala)
        self.assertEqual(mem.latest_vision("QUARTO"), obs_quarto)
        self.assertIsNone(mem.latest_vision("Cozinha"))


if __name__ == "__main__":
    unittest.main()

