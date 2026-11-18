import json
import os
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from src.config import VAR_DIR, get_settings
from src.memory import SharedMemory
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
        finally:
            real.close()


if __name__ == "__main__":
    unittest.main()

