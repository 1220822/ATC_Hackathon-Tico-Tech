"""Camera frame capture (File / RTSP / Webcam) and safe snapshot storage under var/frames/."""

import os
import re
import time
import uuid
from pathlib import Path
from typing import Protocol

from ..config import Settings

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
    return RtspFrameSource(raw)

