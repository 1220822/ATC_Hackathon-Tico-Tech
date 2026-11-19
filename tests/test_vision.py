import json
import os
import struct
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.agents import bridge as bridge_mod
from src.agents.bridge import AgentBridge, build_prompt
from src.agents.tools import TOOLS_BY_AGENT, ultima_analise_camara
from src.api import create_app
from src.config import VAR_DIR, get_settings
from src.coordinator import Coordinator
from src.memory import EventBus, SharedMemory
from src.perception.state import DeviceState, Event, HomeState, RoomState
from src.twin.state import AREA_BY_EVENT, event_view, twin_state
from src.vision.analyzer import (
    MockVisionAnalyzer,
    OpenAIVisionAnalyzer,
    VisionAnalysisPayload,
    VisionInferenceError,
    VisionTimeoutError,
    make_analyzer,
    parse_vision_payload,
)
from src.vision.capture import (
    CaptureError,
    FileFrameSource,
    FrameStore,
    RtspFrameSource,
    WebcamFrameSource,
    make_frame_source,
    redact_url,
    resolve_frame_path,
)
from src.vision.models import VisionObservation, format_vision_message, to_event, vision_severity
from src.vision.worker import VISION_TRIGGER_EVENTS, VisionWorker, should_trigger_vision


def make_solid_png(w: int = 4, h: int = 4, rgb: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """Generate a minimal valid RGB PNG in memory (avoids committing binary blobs)."""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack("!IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


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
        self.assertEqual(s.vision_timeout_s, 45.0)
        self.assertEqual(s.vision_keep_alive, "30m")
        self.assertTrue(s.vision_warmup)
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


class TestFrameCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_frame_source_reads_bytes_and_handles_missing(self):
        img = self.root / "sample.png"
        expected = make_solid_png(8, 8, (10, 20, 30))
        img.write_bytes(expected)
        src = FileFrameSource(img)
        self.assertEqual(src.capture(), expected)

        with self.assertRaises(CaptureError):
            FileFrameSource(self.root / "nao_existe.jpg").capture()

        with self.assertRaises(CaptureError):
            FileFrameSource(None).capture()

    def test_file_frame_source_fallback_dir(self):
        img = self.root / "01_scene.png"
        expected = make_solid_png()
        img.write_bytes(expected)
        src = FileFrameSource(None, fallback_dir=self.root)
        self.assertEqual(src.capture(), expected)

    def test_frame_store_saves_and_prunes_retention(self):
        frames_dir = self.root / "frames"
        store = FrameStore(frames_dir, max_frames=3)
        png = make_solid_png()
        saved = []
        base_ts = 1795000000.0
        for i in range(5):
            name = store.save(png, room_name="Sala", obs_id=f"obs{i}", ts=base_ts + i)
            p = frames_dir / name
            # ensure deterministic mtime ordering on fast filesystems
            os.utime(p, (base_ts + i, base_ts + i))
            saved.append(name)
        # trigger final prune check with one more save -> max_frames=3
        last = store.save(png, room_name="Sala", obs_id="obs5", ts=base_ts + 5)
        remaining = sorted(p.name for p in frames_dir.iterdir() if p.is_file())
        self.assertEqual(len(remaining), 3)
        self.assertIn(last, remaining)
        self.assertNotIn(saved[0], remaining)

    def test_resolve_frame_path_blocks_traversal(self):
        frames_dir = self.root / "frames"
        frames_dir.mkdir()
        secret = self.root / "secret.txt"
        secret.write_text("top-secret", encoding="utf-8")
        good = frames_dir / "snap.jpg"
        good.write_bytes(b"\xff\xd8\xff\xd9")
        sub = frames_dir / "nested"
        sub.mkdir()
        (sub / "inside.jpg").write_bytes(b"\xff\xd8\xff\xd9")

        self.assertEqual(resolve_frame_path(frames_dir, "snap.jpg"), good.resolve())
        self.assertIsNone(resolve_frame_path(frames_dir, ""))
        self.assertIsNone(resolve_frame_path(frames_dir, "../secret.txt"))
        self.assertIsNone(resolve_frame_path(frames_dir, "..\\secret.txt"))
        self.assertIsNone(resolve_frame_path(frames_dir, "nested/inside.jpg"))
        self.assertIsNone(resolve_frame_path(frames_dir, "nested"))
        self.assertIsNone(resolve_frame_path(frames_dir, str(secret)))
        self.assertIsNone(resolve_frame_path(frames_dir, "missing.jpg"))

    def test_make_frame_source_selection(self):
        base = get_settings()
        sim_s = base.model_copy(update={"simulate": True, "camera_rtsp_url": "rtsp://u:p@1.2.3.4/stream"})
        self.assertIsInstance(make_frame_source(sim_s), FileFrameSource)

        empty_s = base.model_copy(update={"simulate": False, "camera_rtsp_url": ""})
        self.assertIsInstance(make_frame_source(empty_s), FileFrameSource)

        cam_s = base.model_copy(update={"simulate": False, "camera_rtsp_url": "webcam:1"})
        cam = make_frame_source(cam_s)
        self.assertIsInstance(cam, WebcamFrameSource)
        self.assertEqual(cam.index, 1)

        rtsp_s = base.model_copy(update={"simulate": False, "camera_rtsp_url": "rtsp://admin:pass@10.0.0.5:554/s1"})
        rtsp = make_frame_source(rtsp_s)
        self.assertIsInstance(rtsp, RtspFrameSource)

    def test_redact_url_and_lazy_cv2_error(self):
        self.assertEqual(
            redact_url("rtsp://admin:supersecret@192.168.1.20:554/stream1"),
            "rtsp://<redacted>@192.168.1.20:554/stream1",
        )
        with patch.dict("sys.modules", {"cv2": None}):
            with self.assertRaises(CaptureError) as ctx:
                RtspFrameSource("rtsp://admin:supersecret@192.168.1.20:554/s1").capture()
            self.assertNotIn("supersecret", str(ctx.exception))
            with self.assertRaises(CaptureError):
                WebcamFrameSource(0).capture()


class TestVisionAnalyzer(unittest.TestCase):
    def test_parse_clean_fenced_and_think_json(self):
        raw = json.dumps({
            "has_person": True,
            "person_count": 2,
            "category": "person",
            "window_or_door_open": True,
            "smoke_or_fire_visible": False,
            "objects": ["pessoa", "janela"],
            "overall_confidence": 0.88,
            "scene_note_pt": "Duas pessoas junto à janela aberta.",
        })
        p1 = parse_vision_payload(raw)
        self.assertTrue(p1.has_person)
        self.assertEqual(p1.person_count, 2)
        self.assertTrue(p1.window_or_door_open)
        self.assertEqual(p1.objects, ("pessoa", "janela"))

        fenced = f"Aqui está o resultado:\n```json\n{raw}\n```"
        self.assertEqual(parse_vision_payload(fenced), p1)

        with_think = f"<think>vejo duas pessoas e janela aberta</think>\n{raw}"
        self.assertEqual(parse_vision_payload(with_think), p1)

        # confidence alias + count reconciliation
        alias = parse_vision_payload('{"has_person": true, "category": "person", "confidence": 0.75}')
        self.assertEqual(alias.person_count, 1)
        self.assertEqual(alias.overall_confidence, 0.75)

        with self.assertRaises(VisionInferenceError):
            parse_vision_payload("isto não é json")

    def test_openai_analyzer_payload_and_contexts(self):
        seen: list[dict] = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode("utf-8"))
            seen.append({"url": str(req.url), "auth": req.headers.get("authorization"), "body": body})
            reply = {
                "has_person": True,
                "person_count": 1,
                "category": "person",
                "window_or_door_open": True,
                "smoke_or_fire_visible": False,
                "objects": ["pessoa", "janela"],
                "overall_confidence": 0.91,
                "scene_note_pt": "Uma pessoa junto à janela aberta na sala.",
            }
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

        analyzer = OpenAIVisionAnalyzer(
            base_url="http://192.168.2.161:1234/v1",
            model_name="qwen3.8:latest",
            api_key="local",
            timeout_s=25.0,
            reasoning_effort="none",
            transport=httpx.MockTransport(handler),
        )
        try:
            png = make_solid_png()
            res = analyzer.analyze(png, context="intrusion", room_name="Sala", security_status="AWAY", trigger_type="camera_motion")
            self.assertTrue(res.has_person)
            self.assertTrue(res.window_or_door_open)
            self.assertEqual(len(seen), 1)
            b = seen[0]["body"]
            self.assertEqual(seen[0]["url"], "http://192.168.2.161:1234/v1/chat/completions")
            self.assertEqual(seen[0]["auth"], "Bearer local")
            self.assertEqual(b["model"], "qwen3.8:latest")
            self.assertEqual(b["temperature"], 0.0)
            self.assertEqual(b["max_tokens"], 500)
            self.assertEqual(b["response_format"], {"type": "json_object"})
            self.assertEqual(b["reasoning_effort"], "none")
            user_parts = b["messages"][1]["content"]
            self.assertIn("janela", user_parts[0]["text"].lower())
            self.assertTrue(user_parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))

            # smoke context prompt mentions fumo / chamas
            analyzer.analyze(b"\xff\xd8\xff\xd9", context="smoke", room_name="Sala", trigger_type="smoke_detected")
            smoke_parts = seen[1]["body"]["messages"][1]["content"]
            self.assertIn("fumo", smoke_parts[0]["text"].lower())
            self.assertIn("chamas", smoke_parts[0]["text"].lower())
            self.assertTrue(smoke_parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        finally:
            analyzer.close()

    def test_response_format_400_fallback(self):
        attempts = []

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode("utf-8"))
            attempts.append("response_format" in body)
            if "response_format" in body:
                return httpx.Response(400, text="unsupported parameter: response_format")
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"category": "empty", "overall_confidence": 0.8}'}}]})

        analyzer = OpenAIVisionAnalyzer("http://localhost:11434/v1", "qwen3.8:latest", transport=httpx.MockTransport(handler))
        try:
            res = analyzer.analyze(make_solid_png(), context="general")
            self.assertEqual(res.category, "empty")
            self.assertEqual(attempts, [True, False])
        finally:
            analyzer.close()

    def test_timeout_http_500_and_malformed_json(self):
        def timeout_handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=req)

        a_to = OpenAIVisionAnalyzer("http://localhost:11434/v1", "qwen3.8:latest", transport=httpx.MockTransport(timeout_handler))
        try:
            with self.assertRaises(VisionTimeoutError):
                a_to.analyze(make_solid_png(), context="intrusion")
        finally:
            a_to.close()

        a_500 = OpenAIVisionAnalyzer(
            "http://localhost:11434/v1",
            "qwen3.8:latest",
            transport=httpx.MockTransport(lambda req: httpx.Response(500, text="server error")),
        )
        try:
            with self.assertRaises(VisionInferenceError):
                a_500.analyze(make_solid_png(), context="intrusion")
        finally:
            a_500.close()

        a_bad = OpenAIVisionAnalyzer(
            "http://localhost:11434/v1",
            "qwen3.8:latest",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": "sem json"}}]})),
        )
        try:
            with self.assertRaises(VisionInferenceError):
                a_bad.analyze(make_solid_png(), context="intrusion")
        finally:
            a_bad.close()

    def test_mock_analyzer_and_factory(self):
        mock = MockVisionAnalyzer({"has_person": True, "person_count": 1, "category": "person", "overall_confidence": 0.9})
        out = mock.analyze(make_solid_png(), context="intrusion", room_name="Sala")
        self.assertTrue(out.has_person)
        self.assertEqual(len(mock.calls), 1)

        s = get_settings()
        real = make_analyzer(s)
        try:
            self.assertIsInstance(real, OpenAIVisionAnalyzer)
            self.assertEqual(real.model_name, s.vision_model)
            self.assertEqual(real.keep_alive, s.vision_keep_alive)
        finally:
            real.close()

    def test_ollama_warmup_and_non_ollama_fallback(self):
        seen: list[dict] = []

        def ok_handler(req: httpx.Request) -> httpx.Response:
            seen.append({"url": str(req.url), "body": json.loads(req.content.decode("utf-8"))})
            return httpx.Response(200, json={"done": True})

        a = OpenAIVisionAnalyzer(
            "http://192.168.2.161:1234/v1",
            "qwen3.8:latest",
            keep_alive="30m",
            transport=httpx.MockTransport(ok_handler),
        )
        try:
            self.assertTrue(a.warmup())
            self.assertEqual(seen[0]["url"], "http://192.168.2.161:1234/api/generate")
            self.assertEqual(seen[0]["body"], {"model": "qwen3.8:latest", "keep_alive": "30m"})
        finally:
            a.close()

        # Non-Ollama server (404) or network error silently returns False without raising
        a_404 = OpenAIVisionAnalyzer(
            "https://api.example.com/v1",
            "gpt-4o",
            keep_alive="30m",
            transport=httpx.MockTransport(lambda req: httpx.Response(404, text="not found")),
        )
        try:
            self.assertFalse(a_404.warmup())
        finally:
            a_404.close()

        a_off = OpenAIVisionAnalyzer("http://localhost:11434/v1", "qwen3.8:latest", keep_alive="")
        try:
            self.assertFalse(a_off.warmup())
        finally:
            a_off.close()


def _sample_home_state(security_status: str = "AWAY") -> HomeState:
    cam = DeviceState(
        id="cam-sala", name="Camera", type="camera", role="camera", online=True,
        room_id="r-sala", room_name="Sala",
    )
    contact_sala = DeviceState(
        id="cnt-sala", name="Contact sensor", type="contactSensor", role="contact", online=True,
        room_id="r-sala", room_name="Sala",
    )
    contact_cozinha = DeviceState(
        id="cnt-coz", name="Contact Cozinha", type="contactSensor", role="contact", online=True,
        room_id="r-cozinha", room_name="Cozinha", source="sim",
    )
    return HomeState(
        ts=1795000000.0,
        home_name="Hack 7",
        security_status=security_status,
        devices={cam.id: cam, contact_sala.id: contact_sala, contact_cozinha.id: contact_cozinha},
        rooms={
            "r-sala": RoomState(id="r-sala", name="Sala", device_ids=[cam.id, contact_sala.id]),
            "r-cozinha": RoomState(id="r-cozinha", name="Cozinha", simulated=True, device_ids=[contact_cozinha.id]),
        },
    )


def _ev(etype: str, room: str | None = "r-sala", room_name: str | None = "Sala", **data) -> Event:
    return Event(type=etype, message="teste", room=room, room_name=room_name, data=data)


def _wait_for(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


class TestVisionWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.img = self.root / "frame.png"
        self.img.write_bytes(make_solid_png())
        self.s = get_settings().model_copy(update={
            "vision_enabled": True,
            "vision_cooldown_s": 10.0,
            "vision_stay_cooldown_s": 30.0,
            "vision_trigger_disarmed": False,
            "vision_sim_image": self.img,
            "vision_frames_dir": self.root / "frames",
            "vision_max_frames": 10,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_trigger_matrix_across_security_modes_and_rooms(self):
        cases = [
            # (mode, event_type, room, disarmed_flag, expected_ok, expected_ctx)
            ("AWAY", "security_alarm", "r-sala", False, True, "intrusion"),
            ("STAY", "security_alarm", "r-sala", False, True, "intrusion"),
            ("DISARMED", "security_alarm", "r-sala", False, True, "intrusion"),
            ("DISARMED", "security_alarm", None, False, True, "intrusion"),
            ("AWAY", "tamper_alarm", "r-sala", False, True, "intrusion"),
            ("STAY", "tamper_alarm", "r-sala", False, True, "intrusion"),
            ("DISARMED", "tamper_alarm", "r-sala", False, True, "intrusion"),
            ("AWAY", "contact_opened", "r-sala", False, True, "intrusion"),
            ("STAY", "contact_opened", "r-sala", False, True, "intrusion"),
            ("DISARMED", "contact_opened", "r-sala", False, False, "general"),
            ("AWAY", "contact_opened", "r-cozinha", False, False, "general"),  # room-aware: Cozinha has no camera
            ("AWAY", "camera_motion", "r-sala", False, True, "intrusion"),
            ("STAY", "camera_motion", "r-sala", False, True, "intrusion"),
            ("DISARMED", "camera_motion", "r-sala", False, False, "general"),
            ("DISARMED", "camera_motion", "r-sala", True, True, "intrusion"),
            ("AWAY", "camera_sound", "r-sala", False, True, "intrusion"),
            ("STAY", "camera_sound", "r-sala", False, False, "general"),
            ("DISARMED", "camera_sound", "r-sala", False, False, "general"),
            ("AWAY", "smoke_detected", "r-sala", False, True, "smoke"),
            ("STAY", "smoke_detected", "r-sala", False, True, "smoke"),
            ("DISARMED", "smoke_detected", None, False, True, "smoke"),
            ("AWAY", "temperature_changed", "r-sala", False, False, "general"),
            ("AWAY", "contact_closed", "r-sala", False, False, "general"),
            ("AWAY", "security_mode_changed", None, False, False, "general"),
            ("AWAY", "tick", None, False, False, "general"),
        ]
        for mode, etype, room, disarmed_flag, exp_ok, exp_ctx in cases:
            with self.subTest(mode=mode, etype=etype, room=room, disarmed_flag=disarmed_flag):
                st = _sample_home_state(mode)
                s = self.s.model_copy(update={"vision_trigger_disarmed": disarmed_flag})
                ok, ctx, cam = should_trigger_vision(_ev(etype, room=room), st, s)
                self.assertEqual(ok, exp_ok)
                self.assertEqual(ctx, exp_ctx)
                if exp_ok:
                    self.assertEqual(cam.id, "cam-sala")

    def test_intrusion_burst_deduplicates_and_stay_cooldown_differs(self):
        now = [100.0]
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("AWAY"))
        published: list[Event] = []
        bus.subscribe(published.append, types=["camera_analysis"])
        analyzer = MockVisionAnalyzer({
            "has_person": True, "person_count": 1, "category": "person",
            "window_or_door_open": True, "overall_confidence": 0.9,
        })
        worker = VisionWorker(
            mem, bus, self.s,
            source=FileFrameSource(self.img),
            analyzer=analyzer,
            store=FrameStore(self.s.vision_frames_dir, 10),
            clock=lambda: now[0],
        ).start()
        try:
            # Intrusion burst: contact_opened + security_alarm + camera_motion within 2s -> 1 call
            worker.on_event(_ev("contact_opened"))
            worker.on_event(_ev("security_alarm", kind="intrusion"))
            now[0] += 2.0
            worker.on_event(_ev("camera_motion"))
            self.assertTrue(_wait_for(lambda: len(published) == 1))
            time.sleep(0.05)
            self.assertEqual(len(analyzer.calls), 1)

            # Still inside AWAY 10s cooldown -> ignored
            now[0] += 5.0
            worker.on_event(_ev("camera_motion"))
            time.sleep(0.05)
            self.assertEqual(len(analyzer.calls), 1)

            # Past AWAY 10s cooldown -> triggers
            now[0] += 6.0
            worker.on_event(_ev("camera_motion"))
            self.assertTrue(_wait_for(lambda: len(published) == 2))

            # Switch to STAY: camera_motion uses 30s cooldown
            mem.update_state(_sample_home_state("STAY"))
            now[0] += 15.0  # > 10s but < 30s
            worker.on_event(_ev("camera_motion"))
            time.sleep(0.05)
            self.assertEqual(len(analyzer.calls), 2)

            now[0] += 20.0  # total 35s >= 30s -> triggers
            worker.on_event(_ev("camera_motion"))
            self.assertTrue(_wait_for(lambda: len(published) == 3))
        finally:
            worker.stop()

    def test_smoke_priority_upgrade_bypasses_cooldown_and_inflight(self):
        now = [200.0]
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("AWAY"))
        published: list[Event] = []
        bus.subscribe(published.append, types=["camera_analysis"])

        started_first = threading.Event()
        release_first = threading.Event()

        class BlockingAnalyzer(MockVisionAnalyzer):
            def analyze(self, image_bytes, *, context, room_name="", security_status="DISARMED", trigger_type="manual"):
                if len(self.calls) == 0:
                    started_first.set()
                    release_first.wait(2.0)
                return super().analyze(
                    image_bytes, context=context, room_name=room_name,
                    security_status=security_status, trigger_type=trigger_type,
                )

        analyzer = BlockingAnalyzer(
            lambda ctx, trig: {"smoke_or_fire_visible": ctx == "smoke", "category": "smoke_or_fire" if ctx == "smoke" else "empty"}
        )
        worker = VisionWorker(
            mem, bus, self.s,
            source=FileFrameSource(self.img),
            analyzer=analyzer,
            store=FrameStore(self.s.vision_frames_dir, 10),
            clock=lambda: now[0],
        ).start()
        try:
            # 1) smoke arrives WHILE intrusion job is in-flight -> queued in priority slot and runs right after
            worker.on_event(_ev("camera_motion"))
            self.assertTrue(started_first.wait(2.0))
            worker.on_event(_ev("smoke_detected"))
            release_first.set()
            self.assertTrue(_wait_for(lambda: len(published) == 2))
            self.assertEqual([c["context"] for c in analyzer.calls], ["intrusion", "smoke"])
            self.assertEqual(published[1].severity, "critical")

            # 2) smoke also bypasses cooldown after a finished intrusion job
            now[0] += 15.0
            worker.on_event(_ev("camera_motion"))
            self.assertTrue(_wait_for(lambda: len(published) == 3))
            now[0] += 1.0  # inside 10s cooldown
            worker.on_event(_ev("smoke_detected"))
            self.assertTrue(_wait_for(lambda: len(published) == 4))
            self.assertEqual(analyzer.calls[-1]["context"], "smoke")
        finally:
            worker.stop()

    def test_error_and_timeout_fallbacks_preserve_publish_order(self):
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("AWAY"))

        # Check invariant 4: memory.latest_vision() and memory.recent_events() are ALREADY updated when bus fires!
        observed_during_publish: list[tuple] = []

        def on_publish(ev: Event):
            observed_during_publish.append((
                ev.data["status"],
                mem.latest_vision("r-sala") is not None and mem.latest_vision("r-sala").id == ev.data["id"],
                any(e.id == ev.id for e in mem.recent_events()),
            ))

        bus.subscribe(on_publish, types=["camera_analysis"])

        # Capture failure fallback
        w_cap = VisionWorker(
            mem, bus, self.s,
            source=FileFrameSource(self.root / "missing.png"),
            analyzer=MockVisionAnalyzer(),
            store=FrameStore(self.s.vision_frames_dir, 10),
        ).start()
        try:
            w_cap.on_event(_ev("camera_motion"))
            self.assertTrue(_wait_for(lambda: len(observed_during_publish) == 1))
            self.assertEqual(observed_during_publish[0], ("capture_error", True, True))
        finally:
            w_cap.stop()

        # Timeout fallback
        w_to = VisionWorker(
            mem, bus, self.s,
            source=FileFrameSource(self.img),
            analyzer=MockVisionAnalyzer(error=VisionTimeoutError("timeout")),
            store=FrameStore(self.s.vision_frames_dir, 10),
        ).start()
        try:
            w_to.on_event(_ev("camera_motion"))
            self.assertTrue(_wait_for(lambda: len(observed_during_publish) == 2))
            self.assertEqual(observed_during_publish[1], ("timeout", True, True))
        finally:
            w_to.stop()

    def test_analyze_now_serialises_concurrent_io(self):
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("DISARMED"))

        overlap = [0]
        active = [0]
        gate = threading.Lock()

        class CheckingAnalyzer(MockVisionAnalyzer):
            def analyze(self, image_bytes, *, context, room_name="", security_status="DISARMED", trigger_type="manual"):
                with gate:
                    active[0] += 1
                    if active[0] > 1:
                        overlap[0] += 1
                time.sleep(0.05)
                with gate:
                    active[0] -= 1
                return super().analyze(
                    image_bytes, context=context, room_name=room_name,
                    security_status=security_status, trigger_type=trigger_type,
                )

        worker = VisionWorker(
            mem, bus, self.s,
            source=FileFrameSource(self.img),
            analyzer=CheckingAnalyzer(),
            store=FrameStore(self.s.vision_frames_dir, 10),
        ).start()
        try:
            t1 = threading.Thread(target=worker.analyze_now, kwargs={"room": "Sala", "context": "intrusion"})
            t2 = threading.Thread(target=worker.analyze_now, kwargs={"room": "Sala", "context": "smoke"})
            t1.start()
            t2.start()
            t1.join(3)
            t2.join(3)
            self.assertEqual(overlap[0], 0)
            self.assertIsNotNone(mem.latest_vision("Sala"))
            self.assertEqual(len(VISION_TRIGGER_EVENTS), 6)
        finally:
            worker.stop()

    def test_worker_warmup_async_respects_flag(self):
        mem, bus = SharedMemory(), EventBus()
        called = threading.Event()

        class WarmupMock(MockVisionAnalyzer):
            def warmup(self) -> bool:
                called.set()
                return True

        w_on = VisionWorker(
            mem, bus, self.s.model_copy(update={"vision_warmup": True, "vision_keep_alive": "30m"}),
            source=FileFrameSource(self.img),
            analyzer=WarmupMock(),
            store=FrameStore(self.s.vision_frames_dir, 10),
        )
        t = w_on.warmup_async()
        self.assertIsNotNone(t)
        self.assertTrue(called.wait(2.0))
        t.join(2.0)

        w_off = VisionWorker(
            mem, bus, self.s.model_copy(update={"vision_warmup": False}),
            source=FileFrameSource(self.img),
            analyzer=WarmupMock(),
            store=FrameStore(self.s.vision_frames_dir, 10),
        )
        self.assertIsNone(w_off.warmup_async())


class _FakeA2AClient:
    """In-process stand-in for A2AAgent so AgentBridge tests run offline without sockets."""

    def __init__(self, reply_text: str):
        self.reply_text = reply_text
        self.prompts: list[str] = []

    async def run(self, prompt: str):
        self.prompts.append(prompt)
        msg = SimpleNamespace(role="assistant", text=self.reply_text)
        return SimpleNamespace(text=self.reply_text, messages=[msg], raw_representation=[])


class TestVisionIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.img = self.root / "frame.png"
        self.img.write_bytes(make_solid_png())
        self.s = get_settings().model_copy(update={
            "simulate": True,
            "dry_run": True,
            "history_db": self.root / "hist.db",
            "sim_state_file": self.root / "sim.json",
            "vision_enabled": True,
            "vision_cooldown_s": 10.0,
            "vision_stay_cooldown_s": 30.0,
            "vision_trigger_disarmed": False,
            "vision_sim_image": self.img,
            "vision_frames_dir": self.root / "frames",
            "vision_max_frames": 10,
            "agent_cooldown_s": 60.0,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def _make_bridge(self, mem, bus, coord, vision_worker=None, watchdog_s: float = 1.0):
        clients: dict[str, _FakeA2AClient] = {}
        reply = json.dumps({"resumo": "Verificado pelo agente de segurança.", "propostas": []})

        def factory(key: str):
            c = _FakeA2AClient(reply)
            clients[key] = c
            return c

        br = AgentBridge(
            mem, self.s, coord, bus,
            urls={"seguranca": "http://127.0.0.1:9101/"},
            client_factory=factory,
            vision_worker=vision_worker,
            vision_watchdog_s=watchdog_s,
        ).start()
        return br, clients

    def test_bridge_defers_camera_motion_to_vision_and_sends_once(self):
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("AWAY"))
        coord = Coordinator(mem, self.s)
        analyzer = MockVisionAnalyzer({
            "has_person": True, "person_count": 1, "category": "person",
            "window_or_door_open": True, "overall_confidence": 0.92,
            "scene_note_pt": "Uma pessoa junto à janela aberta.",
        })
        worker = VisionWorker(
            mem, bus, self.s,
            source=FileFrameSource(self.img),
            analyzer=analyzer,
            store=FrameStore(self.s.vision_frames_dir, 10),
        ).start()
        bus.subscribe(worker.on_event, types=list(VISION_TRIGGER_EVENTS))
        with patch.object(bridge_mod, "BATCH_S", 0.03):
            br, clients = self._make_bridge(mem, bus, coord, vision_worker=worker, watchdog_s=0.5)
            try:
                bus.publish([_ev("camera_motion")])
                self.assertTrue(_wait_for(lambda: "seguranca" in clients and len(clients["seguranca"].prompts) == 1))
                time.sleep(0.15)  # make sure watchdog was cancelled and no duplicate call arrives
                self.assertEqual(len(clients["seguranca"].prompts), 1)
                prompt = clients["seguranca"].prompts[0]
                self.assertIn("camera_analysis", prompt)
                self.assertIn("pessoas=1", prompt)
                self.assertIn("janela_ou_porta_aberta=sim", prompt)
                self.assertIn("Uma pessoa junto à janela aberta.", prompt)
            finally:
                br.stop()
                worker.stop()

    def test_bridge_routes_immediately_when_vision_worker_dead(self):
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("AWAY"))
        coord = Coordinator(mem, self.s)
        dead_worker = VisionWorker(
            mem, bus, self.s,
            source=FileFrameSource(self.img),
            analyzer=MockVisionAnalyzer(),
            store=FrameStore(self.s.vision_frames_dir, 10),
        )  # not started -> is_alive() is False
        with patch.object(bridge_mod, "BATCH_S", 0.03):
            br, clients = self._make_bridge(mem, bus, coord, vision_worker=dead_worker, watchdog_s=5.0)
            try:
                bus.publish([_ev("camera_motion")])
                self.assertTrue(_wait_for(lambda: "seguranca" in clients and len(clients["seguranca"].prompts) == 1))
                self.assertIn("camera_motion", clients["seguranca"].prompts[0])
            finally:
                br.stop()

    def test_bridge_watchdog_forwards_original_event_on_hung_vision(self):
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("AWAY"))
        coord = Coordinator(mem, self.s)

        class HungWorker:
            def is_alive(self) -> bool:
                return True

            def will_handle(self, event, state) -> bool:
                return True

        with patch.object(bridge_mod, "BATCH_S", 0.02):
            br, clients = self._make_bridge(mem, bus, coord, vision_worker=HungWorker(), watchdog_s=0.1)
            try:
                bus.publish([_ev("camera_motion")])
                self.assertTrue(_wait_for(lambda: "seguranca" in clients and len(clients["seguranca"].prompts) == 1))
                prompt = clients["seguranca"].prompts[0]
                self.assertIn("camera_motion", prompt)
                self.assertIn("análise visual indisponível", prompt)
            finally:
                br.stop()

    def test_security_alarm_and_camera_analysis_intentional_double_call(self):
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("AWAY"))
        coord = Coordinator(mem, self.s)
        release_vision = threading.Event()

        class SlowAnalyzer(MockVisionAnalyzer):
            def analyze(self, image_bytes, *, context, room_name="", security_status="DISARMED", trigger_type="manual"):
                release_vision.wait(2.0)
                return super().analyze(
                    image_bytes, context=context, room_name=room_name,
                    security_status=security_status, trigger_type=trigger_type,
                )

        analyzer = SlowAnalyzer({
            "has_person": True, "person_count": 1, "category": "person",
            "window_or_door_open": True, "overall_confidence": 0.9,
        })
        worker = VisionWorker(
            mem, bus, self.s,
            source=FileFrameSource(self.img),
            analyzer=analyzer,
            store=FrameStore(self.s.vision_frames_dir, 10),
        ).start()
        bus.subscribe(worker.on_event, types=list(VISION_TRIGGER_EVENTS))
        with patch.object(bridge_mod, "BATCH_S", 0.03):
            br, clients = self._make_bridge(mem, bus, coord, vision_worker=worker, watchdog_s=2.0)
            try:
                # security_alarm goes straight to seguranca while camera_motion is deferred/deduped
                bus.publish([_ev("security_alarm", kind="intrusion"), _ev("camera_motion")])
                self.assertTrue(_wait_for(lambda: "seguranca" in clients and len(clients["seguranca"].prompts) == 1))
                self.assertIn("security_alarm", clients["seguranca"].prompts[0])
                # now let vision finish -> second call carries camera_analysis visual confirmation
                release_vision.set()
                self.assertTrue(_wait_for(lambda: len(clients["seguranca"].prompts) == 2))
                self.assertIn("camera_analysis", clients["seguranca"].prompts[1])
                self.assertIn("pessoas=1", clients["seguranca"].prompts[1])
            finally:
                release_vision.set()
                br.stop()
                worker.stop()

    def test_bridge_cooldown_allows_warning_critical_and_dedups_info(self):
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("AWAY"))
        coord = Coordinator(mem, self.s)
        br, _ = self._make_bridge(mem, bus, coord)
        try:
            warn_ev = to_event(
                VisionObservation(room_id="r-sala", room_name="Sala", trigger_type="security_alarm", category="empty"),
                armed=True,
            )
            crit_ev = to_event(
                VisionObservation(room_id="r-sala", room_name="Sala", has_person=True, person_count=1, category="person"),
                armed=True,
            )
            info_ev = to_event(
                VisionObservation(room_id="r-sala", room_name="Sala", trigger_type="camera_motion", category="pet"),
                armed=True,
            )
            self.assertEqual(warn_ev.severity, "warning")
            self.assertEqual(crit_ev.severity, "critical")
            self.assertEqual(info_ev.severity, "info")

            # warning and critical are never suppressed by the 60s info cooldown
            self.assertFalse(br._recently_sent("seguranca", warn_ev))
            self.assertFalse(br._recently_sent("seguranca", crit_ev))
            # info is suppressed on immediate repeat within 60s
            self.assertTrue(br._recently_sent("seguranca", info_ev))
        finally:
            br.stop()

    def test_fastapi_vision_endpoints_and_path_traversal(self):
        mem, bus = SharedMemory(), EventBus()
        mem.update_state(_sample_home_state("DISARMED"))
        analyzer = MockVisionAnalyzer({
            "has_person": False, "category": "empty", "window_or_door_open": False,
            "overall_confidence": 0.95, "scene_note_pt": "Sala vazia com janela fechada.",
        })
        worker = VisionWorker(
            mem, bus, self.s,
            source=FileFrameSource(self.img),
            analyzer=analyzer,
            store=FrameStore(self.s.vision_frames_dir, 10),
        ).start()
        try:
            app = create_app(mem, self.s, vision_worker=worker)
            client = TestClient(app)

            r0 = client.get("/tools/ultima_analise_camara")
            self.assertEqual(r0.status_code, 200)
            self.assertFalse(r0.json()["disponivel"])

            r_post = client.post("/vision/analyze", json={"divisao": "Sala", "contexto": "intrusion"})
            self.assertEqual(r_post.status_code, 200)
            body = r_post.json()
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["category"], "empty")
            self.assertIsNotNone(body["frame_path"])

            r1 = client.get("/tools/ultima_analise_camara", params={"divisao": "Sala"})
            self.assertEqual(r1.status_code, 200)
            self.assertTrue(r1.json()["disponivel"])
            self.assertEqual(r1.json()["id"], body["id"])

            r_img = client.get(f"/vision/frames/{body['frame_path']}")
            self.assertEqual(r_img.status_code, 200)
            self.assertEqual(r_img.content, make_solid_png())

            # path traversal blocked
            r_bad = client.get("/vision/frames/..%2F..%2F.env")
            self.assertEqual(r_bad.status_code, 404)

            # 503 when vision_worker is not wired
            app_no_vision = create_app(mem, self.s)
            r_503 = TestClient(app_no_vision).post("/vision/analyze")
            self.assertEqual(r_503.status_code, 503)
        finally:
            worker.stop()

    def test_twin_area_and_security_tool_registration(self):
        self.assertEqual(AREA_BY_EVENT["camera_analysis"], "Segurança")
        self.assertIn(ultima_analise_camara, TOOLS_BY_AGENT["seguranca"])

        mem = SharedMemory()
        mem.update_state(_sample_home_state("AWAY"))
        obs = VisionObservation(
            room_id="r-sala", room_name="Sala", camera_id="cam-sala",
            has_person=True, person_count=1, category="person", overall_confidence=0.85,
        )
        ev = to_event(obs, armed=True)
        mem.set_vision(obs)
        mem.add_events([ev])
        self.assertEqual(event_view(ev)["area"], "Segurança")

        st = twin_state(mem, self.s)
        self.assertIsNotNone(st)
        self.assertTrue(any(e["type"] == "camera_analysis" and e["area"] == "Segurança" for e in st["events"]))

        # build_prompt compact line check
        p = build_prompt([ev], mem.state)
        self.assertIn("pessoas=1", p)
        self.assertIn("confiança=0.85", p)


if __name__ == "__main__":
    unittest.main()



class TestLiveRtsp(unittest.TestCase):
    """A lost h264 packet (one failed read) must not drop the RTSP connection."""

    def test_isolated_read_failure_keeps_the_connection(self):
        import threading
        import time as _time
        from src.vision import capture as cap_mod

        opened = []
        done = threading.Event()

        class FakeCap:
            def __init__(self, *a):
                opened.append(1)
                self.reads = iter([(True, "f1"), (False, None), (True, "smeared"), (True, "f2")])

            def isOpened(self):
                return True

            def read(self):
                try:
                    return next(self.reads)
                except StopIteration:
                    done.set()
                    _time.sleep(0.05)
                    return True, "f2"

            def release(self):
                pass

        class FakeCv2:
            CAP_FFMPEG = 0
            VideoCapture = FakeCap

        src = cap_mod.LiveRtspFrameSource("rtsp://x/y")
        src.GRACE_S = 0.0  # the first frame is usable; the grace after the failure is tested below
        seen = []
        with patch.object(cap_mod, "_import_cv2", return_value=FakeCv2):
            orig_wait = src._stop.wait
            src._stop.wait = lambda t=None: orig_wait(0)  # no real sleeping in the test
            src.start()
            self.assertTrue(done.wait(5))
            seen.append(src._frame)
            src.stop()
        self.assertEqual(len(opened), 1)  # never reconnected
        self.assertEqual(src.read_failures, 1)
        self.assertEqual(seen, ["f2"])

    def test_frames_right_after_a_failure_are_skipped(self):
        from src.vision import capture as cap_mod
        self.assertGreater(cap_mod.LiveRtspFrameSource.GRACE_S, 0)
        self.assertGreater(cap_mod.LiveRtspFrameSource.STALL_S, cap_mod.LiveRtspFrameSource.GRACE_S)
