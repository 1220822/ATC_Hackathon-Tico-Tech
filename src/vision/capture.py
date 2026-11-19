"""Camera frame capture (File / RTSP / Webcam) and safe snapshot storage under var/frames/."""

import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Protocol

from ..config import Settings

log = logging.getLogger(__name__)
URL_CREDS = re.compile(r"(\w+://)[^/@\s:]+:[^/@\s]+@")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
IMAGE_EXTS = (".jpg", ".jpeg", ".png")


class CaptureError(RuntimeError):
    """Raised when a camera frame cannot be captured or read."""


class FrameSource(Protocol):
    name: str

    def capture(self, room_id: str | None = None) -> bytes: ...


def redact_url(url: str) -> str:
    """Strip credentials from RTSP/HTTP URLs before logging or error reporting."""
    return URL_CREDS.sub(r"\1<redacted>@", url)


def _import_cv2():
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as e:
        raise CaptureError(
            "opencv-python não instalado (necessário apenas para captura RTSP/webcam)"
        ) from e
    return cv2


def _encode_frame(cv2, frame, max_width: int, jpeg_quality: int) -> bytes:
    h, w = frame.shape[:2]
    if max_width > 0 and w > max_width:
        scale = max_width / float(w)
        frame = cv2.resize(frame, (max_width, max(1, int(round(h * scale)))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
    if not ok:
        raise CaptureError("falha ao codificar frame para JPEG")
    return buf.tobytes()


class FileFrameSource:
    """Deterministic frame source reading a local image file (for SIMULATE=true and tests)."""

    name = "file"

    def __init__(self, image_path: Path | None = None, fallback_dir: Path | None = None):
        self.image_path = Path(image_path) if image_path else None
        self.fallback_dir = Path(fallback_dir) if fallback_dir else None

    def _resolve_target(self) -> Path:
        if self.image_path is not None:
            if not self.image_path.is_file():
                raise CaptureError(f"imagem de simulação não encontrada: {self.image_path}")
            return self.image_path
        if self.fallback_dir is not None and self.fallback_dir.is_dir():
            for p in sorted(self.fallback_dir.iterdir()):
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                    return p
        raise CaptureError("nenhuma imagem configurada para FileFrameSource (define VISION_SIM_IMAGE)")

    def capture(self, room_id: str | None = None) -> bytes:
        path = self._resolve_target()
        try:
            data = path.read_bytes()
        except OSError as e:
            raise CaptureError(f"falha ao ler imagem {path}: {e}") from None
        if not data:
            raise CaptureError(f"imagem vazia: {path}")
        return data


class RtspFrameSource:
    """Captures a fresh frame from an RTSP stream via lazy-imported OpenCV."""

    name = "rtsp"

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = 5.0,
        max_width: int = 1024,
        jpeg_quality: int = 80,
        flush_frames: int = 4,
    ):
        self.url = url
        self.timeout_s = timeout_s
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self.flush_frames = max(1, flush_frames)

    def capture(self, room_id: str | None = None) -> bytes:
        cv2 = _import_cv2()
        _rtsp_over_tcp()
        safe_url = redact_url(self.url)
        timeout_ms = int(self.timeout_s * 1000)
        cap = None
        try:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(self.url)
            if not cap.isOpened():
                raise CaptureError(f"não foi possível abrir o stream RTSP ({safe_url})")
            for prop, val in (
                (getattr(cv2, "CAP_PROP_BUFFERSIZE", None), 1),
                (getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None), timeout_ms),
                (getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None), timeout_ms),
            ):
                if prop is not None:
                    cap.set(prop, val)
            deadline = time.monotonic() + self.timeout_s
            frame = None
            for _ in range(self.flush_frames):
                if time.monotonic() > deadline:
                    break
                ok, f = cap.read()
                if ok and f is not None:
                    frame = f
            if frame is None:
                raise CaptureError(f"timeout ou stream sem frames ({safe_url})")
            return _encode_frame(cv2, frame, self.max_width, self.jpeg_quality)
        except CaptureError:
            raise
        except Exception as e:  # noqa: BLE001
            raise CaptureError(f"erro na captura RTSP ({safe_url}): {e}") from None
        finally:
            if cap is not None:
                cap.release()


def _rtsp_over_tcp() -> None:
    """FFmpeg options for the RTSP camera: TCP transport, and only fatal FFmpeg messages (the go2rtc
    stream loses an h264 packet now and then; FFmpeg would print "error while decoding MB…" each time,
    the live reader logs its own state instead). An explicit value in the environment wins.

    On Windows the FFmpeg plugin of OpenCV (opencv_videoio_ffmpeg*.dll) reads the environment through
    msvcrt, whose copy is taken when the process starts: os.environ alone is not seen, so the values
    are also set there."""
    for key, value in (("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp"), ("OPENCV_FFMPEG_LOGLEVEL", "8")):
        os.environ.setdefault(key, value)
        if os.name == "nt":
            try:
                import ctypes
                ctypes.cdll.msvcrt._putenv(f"{key}={os.environ[key]}".encode())
            except Exception:  # noqa: BLE001 - only cosmetics / defaults depend on it
                log.debug("não consegui passar %s ao FFmpeg", key, exc_info=True)


class LiveRtspFrameSource(RtspFrameSource):
    """RTSP kept open in a background thread: the newest frame is always at hand, so a capture for the
    vision model is instant (opening the stream costs ~5 s) and the 3D can show the live feed.
    Reconnects on its own; if the stream is down, capture() raises CaptureError like the base class."""

    name = "rtsp-live"

    # A single failed read is a lost h264 packet, not a dead stream (measured on go2rtc: ~1 per minute,
    # frames keep coming right after). Reconnect only after STALL_S without a good frame; frames decoded
    # in the GRACE_S after a failure may be smeared (missing reference picture), so they are not used.
    STALL_S = 8.0
    GRACE_S = 1.5

    def __init__(self, url: str, *, max_age_s: float = 6.0, reconnect_s: float = 3.0, **kw):
        super().__init__(url, **kw)
        self.max_age_s = max_age_s
        self.reconnect_s = reconnect_s
        self.read_failures = 0  # since start (shown in the logs on reconnect)
        self._frame = None  # newest decoded frame (BGR ndarray)
        self._frame_ts = 0.0  # monotonic
        self._seq = 0
        self._jpeg: tuple[int, bytes] | None = None  # (seq, bytes) cache for the newest frame
        self._error = ""
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------------------------
    def start(self) -> "LiveRtspFrameSource":
        with self._cond:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="rtsp-live", daemon=True)
                self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        _rtsp_over_tcp()
        safe_url = redact_url(self.url)
        while not self._stop.is_set():
            try:  # retried: installing opencv-python while the API runs is enough to recover
                cv2 = _import_cv2()
            except CaptureError as e:
                self._error = str(e)
                log.warning("câmara em direto: %s; a tentar de novo em 30 s", e)
                self._stop.wait(30.0)
                continue
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            try:
                if not cap.isOpened():
                    self._error = f"não foi possível abrir o stream RTSP ({safe_url})"
                else:
                    log.info("câmara em direto: ligada a %s", safe_url)
                    self._error = ""
                    last_good = time.monotonic()
                    suspect_until = last_good + self.GRACE_S  # the first frames may predate a keyframe
                    while not self._stop.is_set():
                        ok, frame = cap.read()
                        now = time.monotonic()
                        if not ok or frame is None:
                            self.read_failures += 1
                            if now - last_good > self.STALL_S:
                                self._error = (f"o stream RTSP não envia imagens há {self.STALL_S:.0f} s "
                                               f"({safe_url})")
                                break
                            suspect_until = now + self.GRACE_S
                            self._stop.wait(0.05)  # do not spin if the stream returns errors at once
                            continue
                        last_good = now
                        if now < suspect_until:
                            continue
                        with self._cond:
                            self._frame, self._frame_ts = frame, time.monotonic()
                            self._seq += 1
                            self._cond.notify_all()
            except Exception as e:  # noqa: BLE001 - keep the reader alive
                self._error = f"erro na captura RTSP ({safe_url}): {e}"
            finally:
                cap.release()
            if not self._stop.is_set():
                log.warning("câmara em direto: %s; a tentar de novo em %.0f s (%d leituras falhadas desde o "
                            "arranque)", self._error, self.reconnect_s, self.read_failures)
                self._stop.wait(self.reconnect_s)

    # -- frames ------------------------------------------------------------------------------------
    @property
    def error(self) -> str:
        return self._error

    def latest_jpeg(self, wait_s: float = 0.0) -> bytes | None:
        """Newest frame as JPEG if it is fresh (waits up to wait_s for one), else None."""
        self.start()
        deadline = time.monotonic() + wait_s
        with self._cond:
            while self._frame is None or time.monotonic() - self._frame_ts > self.max_age_s:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(min(left, 0.5))
            if self._jpeg is not None and self._jpeg[0] == self._seq:
                return self._jpeg[1]
            frame, seq = self._frame, self._seq
        data = _encode_frame(_import_cv2(), frame, self.max_width, self.jpeg_quality)
        with self._cond:
            self._jpeg = (seq, data)
        return data

    def capture(self, room_id: str | None = None) -> bytes:
        data = self.latest_jpeg(wait_s=self.timeout_s + 5.0)  # first open of the stream takes ~5 s
        if data is None:
            raise CaptureError(self._error or f"timeout ou stream sem frames ({redact_url(self.url)})")
        return data


class WebcamFrameSource:
    """Captures a frame from a local webcam via lazy-imported OpenCV (webcam:<index>)."""

    name = "webcam"

    def __init__(
        self,
        index: int = 0,
        *,
        max_width: int = 1024,
        jpeg_quality: int = 80,
        warmup_frames: int = 2,
    ):
        self.index = index
        self.max_width = max_width
        self.jpeg_quality = jpeg_quality
        self.warmup_frames = max(1, warmup_frames)

    def capture(self, room_id: str | None = None) -> bytes:
        cv2 = _import_cv2()
        cap = None
        try:
            if os.name == "nt" and hasattr(cv2, "CAP_DSHOW"):
                cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
                if not cap.isOpened():
                    cap.release()
                    cap = cv2.VideoCapture(self.index)
            else:
                cap = cv2.VideoCapture(self.index)
            if not cap.isOpened():
                raise CaptureError(f"não foi possível abrir a webcam {self.index}")
            frame = None
            for _ in range(self.warmup_frames):
                ok, f = cap.read()
                if ok and f is not None:
                    frame = f
            if frame is None:
                raise CaptureError(f"webcam {self.index} não devolveu frame")
            return _encode_frame(cv2, frame, self.max_width, self.jpeg_quality)
        except CaptureError:
            raise
        except Exception as e:  # noqa: BLE001
            raise CaptureError(f"erro na captura da webcam {self.index}: {e}") from None
        finally:
            if cap is not None:
                cap.release()


def _slug(text: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return clean or "home"


def resolve_frame_path(frames_dir: Path, filename: str) -> Path | None:
    """Resolve a frame filename inside frames_dir safely (blocks path traversal)."""
    if not filename or Path(filename).name != filename:
        return None
    base = Path(frames_dir).resolve()
    candidate = (base / filename).resolve()
    if candidate.parent != base or not candidate.is_file():
        return None
    return candidate


class FrameStore:
    """Persists captured snapshots under var/frames/ with bounded file retention."""

    def __init__(self, frames_dir: Path, max_frames: int = 50):
        self.frames_dir = Path(frames_dir)
        self.max_frames = max(1, int(max_frames))

    def save(
        self,
        image_bytes: bytes,
        *,
        room_name: str = "",
        obs_id: str = "",
        ts: float | None = None,
    ) -> str:
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        ts = time.time() if ts is None else ts
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(ts))
        tag = re.sub(r"[^a-zA-Z0-9_-]+", "", obs_id)[:12] or uuid.uuid4().hex[:10]
        ext = ".png" if image_bytes.startswith(PNG_MAGIC) else ".jpg"
        filename = f"{stamp}_{_slug(room_name)}_{tag}{ext}"
        target = self.frames_dir / filename
        tmp = self.frames_dir / f".{filename}.{os.getpid()}.tmp"
        tmp.write_bytes(image_bytes)
        os.replace(tmp, target)
        self._prune()
        return filename

    def _prune(self) -> None:
        files = [
            p for p in self.frames_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        ]
        if len(files) <= self.max_frames:
            return
        files.sort(key=lambda p: (p.stat().st_mtime_ns, p.name))
        for old in files[: len(files) - self.max_frames]:
            try:
                old.unlink()
            except OSError:
                pass


def make_frame_source(settings: Settings) -> FrameSource:
    """Choose the concrete FrameSource from Settings (file in simulation/unconfigured, webcam, or RTSP)."""
    raw = (settings.camera_rtsp_url or "").strip()
    if settings.simulate or not raw:
        return FileFrameSource(settings.vision_sim_image)
    if raw.lower().startswith("webcam:"):
        idx_str = raw.split(":", 1)[1].strip() or "0"
        try:
            idx = int(idx_str)
        except ValueError as e:
            raise CaptureError(f"índice de webcam inválido em CAMERA_RTSP_URL: {raw}") from e
        return WebcamFrameSource(idx)
    return LiveRtspFrameSource(raw)

