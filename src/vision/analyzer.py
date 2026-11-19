"""Multimodal vision analyzer (OpenAI-compatible / Ollama /v1/chat/completions + mock)."""

import base64
import json
import threading
from typing import Any, Callable, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..config import Settings
from ..json_utils import json_candidates
from .capture import PNG_MAGIC
from .models import VisionCategory, VisionContext


class VisionTimeoutError(TimeoutError):
    """Raised when VLM inference exceeds the configured timeout."""


class VisionInferenceError(RuntimeError):
    """Raised when VLM inference fails (HTTP error, transport error, invalid JSON)."""


class VisionAnalysisPayload(BaseModel):
    """Objective visual facts returned by a VisionAnalyzer before event/metadata wrapping."""

    model_config = ConfigDict(frozen=True)

    has_person: bool = False
    person_count: int = Field(default=0, ge=0)
    category: VisionCategory = "unclear"
    window_or_door_open: bool | None = None
    smoke_or_fire_visible: bool = False
    objects: tuple[str, ...] = ()
    overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    scene_note_pt: str = Field(default="", max_length=300)

    @model_validator(mode="before")
    @classmethod
    def _normalise_raw(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "overall_confidence" not in out and "confidence" in out:
            out["overall_confidence"] = out["confidence"]
        if out.get("objects") is None:
            out["objects"] = ()
        if out.get("scene_note_pt") is None:
            out["scene_note_pt"] = ""
        return out

    @model_validator(mode="after")
    def _reconcile_counts(self) -> "VisionAnalysisPayload":
        if self.person_count > 0 and not self.has_person:
            object.__setattr__(self, "has_person", True)
        elif self.has_person and self.person_count == 0:
            object.__setattr__(self, "person_count", 1)
        return self


class VisionAnalyzer(Protocol):
    model_name: str

    def analyze(
        self,
        image_bytes: bytes,
        *,
        context: VisionContext,
        room_name: str = "",
        security_status: str = "DISARMED",
        trigger_type: str = "manual",
    ) -> VisionAnalysisPayload: ...

    def close(self) -> None: ...


SYSTEM_PROMPT = (
    "És um módulo de perceção visual de uma casa inteligente. "
    "Descreves APENAS factos objetivos visíveis na imagem, sem tomar decisões, sem recomendar ações "
    "e sem inventar o que não é visível. "
    "Escreve sempre em português de Portugal (PT-PT), sem emojis nem markdown. "
    "Responde EXCLUSIVAMENTE com um objeto JSON válido com estas chaves exatas:\n"
    '{"has_person": bool, "person_count": int, '
    '"category": "person"|"pet"|"smoke_or_fire"|"empty"|"other"|"unclear", '
    '"window_or_door_open": true|false|null, '
    '"smoke_or_fire_visible": bool, '
    '"objects": [string], '
    '"overall_confidence": float (0.0 a 1.0), '
    '"scene_note_pt": "<1 frase factual curta em PT-PT>"}'
)

CONTEXT_GUIDANCE: dict[VisionContext, str] = {
    "intrusion": (
        "Contexto: verificação de segurança/intrusão. "
        "Foca-te em: (1) detetar e contar pessoas; "
        "(2) distinguir falsos alarmes comuns (animais de estimação, cortinas, sombras, divisão vazia); "
        "(3) verificar visualmente o estado da janela ou porta (aberta=true, fechada=false, não visível=null) "
        "e eventuais sinais de acesso forçado."
    ),
    "smoke": (
        "Contexto: alerta de detetor de fumo. "
        "Foca-te em: (1) verificar se há fumo visível, neblina densa, chamas ou clarão (smoke_or_fire_visible); "
        "(2) detetar e contar pessoas presentes na divisão em potencial risco; "
        "(3) descrever de forma objetiva o que se vê na imagem."
    ),
    "general": (
        "Contexto: inspeção visual geral da divisão. "
        "Indica se há pessoas, o estado visível de janelas/portas (se visíveis), "
        "eventual fumo/anomalia e uma nota factual curta."
    ),
}


def build_vision_prompt(
    context: VisionContext,
    room_name: str = "",
    security_status: str = "DISARMED",
    trigger_type: str = "manual",
) -> str:
    where = room_name or "divisão monitorizada"
    return (
        f"Divisão: {where}. Modo de segurança: {security_status}. Evento: {trigger_type}.\n"
        f"{CONTEXT_GUIDANCE[context]}\n"
        "Devolve apenas o objeto JSON pedido."
    )


def parse_vision_payload(text: str) -> VisionAnalysisPayload:
    """Parse VLM text into VisionAnalysisPayload, tolerating <think> blocks and code fences."""
    last_err: str | None = None
    for cand in json_candidates(text or ""):
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            try:
                return VisionAnalysisPayload.model_validate(data)
            except ValidationError as e:
                last_err = e.errors()[0]["msg"]
    if last_err:
        raise VisionInferenceError(f"JSON da visão inválido: {last_err}")
    raise VisionInferenceError("o modelo de visão não devolveu JSON válido")


def _mime_for(image_bytes: bytes) -> str:
    return "image/png" if image_bytes.startswith(PNG_MAGIC) else "image/jpeg"


class OpenAIVisionAnalyzer:
    """Vision analyzer talking to Ollama / any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str = "local",
        *,
        timeout_s: float = 45.0,
        reasoning_effort: str = "none",
        max_tokens: int = 500,
        keep_alive: str = "30m",
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.reasoning_effort = reasoning_effort.strip()
        self.max_tokens = max_tokens
        self.keep_alive = keep_alive.strip()
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def _build_body(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        use_response_format: bool,
    ) -> dict[str, Any]:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        mime = _mime_for(image_bytes)
        body: dict[str, Any] = {
            "model": self.model_name,
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                },
            ],
        }
        if use_response_format:
            body["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        return body

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            return self._client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise VisionTimeoutError(f"timeout na inferência visual ({self.timeout_s:.0f}s): {e}") from None
        except httpx.HTTPError as e:
            raise VisionInferenceError(f"erro HTTP/rede na inferência visual: {e}") from None

    def analyze(
        self,
        image_bytes: bytes,
        *,
        context: VisionContext,
        room_name: str = "",
        security_status: str = "DISARMED",
        trigger_type: str = "manual",
    ) -> VisionAnalysisPayload:
        prompt = build_vision_prompt(context, room_name, security_status, trigger_type)
        with self._lock:
            resp = self._post(self._build_body(image_bytes, prompt, use_response_format=True))
            # Automatic fallback if a non-Ollama server rejects response_format with HTTP 400
            if resp.status_code == 400 and (
                "response_format" in resp.text.lower() or "json_object" in resp.text.lower()
            ):
                resp = self._post(self._build_body(image_bytes, prompt, use_response_format=False))

        if resp.status_code >= 400:
            raise VisionInferenceError(f"HTTP {resp.status_code} do servidor de visão: {resp.text[:250]}")
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise VisionInferenceError(f"resposta inesperada do servidor de visão: {e}") from None
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        return parse_vision_payload(str(content or ""))

    def warmup(self) -> bool:
        """Best-effort Ollama pre-load (POST /api/generate with keep_alive, 0 tokens generated).
        Silently returns False on non-Ollama servers (e.g. 404) or transient network errors."""
        if not self.keep_alive:
            return False
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        url = f"{root}/api/generate"
        payload = {"model": self.model_name, "keep_alive": self.keep_alive}
        with self._lock:
            try:
                resp = self._client.post(url, json=payload)
            except httpx.HTTPError:
                return False
        return resp.status_code == 200

    def close(self) -> None:
        with self._lock:
            self._client.close()


class MockVisionAnalyzer:
    """Deterministic analyzer for unit tests and offline runs."""

    def __init__(
        self,
        result: VisionAnalysisPayload | dict[str, Any] | Callable[[VisionContext, str], VisionAnalysisPayload | dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
        model_name: str = "mock-vision",
    ):
        self.result = result
        self.error = error
        self.model_name = model_name
        self.calls: list[dict[str, Any]] = []

    def analyze(
        self,
        image_bytes: bytes,
        *,
        context: VisionContext,
        room_name: str = "",
        security_status: str = "DISARMED",
        trigger_type: str = "manual",
    ) -> VisionAnalysisPayload:
        self.calls.append({
            "size": len(image_bytes),
            "context": context,
            "room_name": room_name,
            "security_status": security_status,
            "trigger_type": trigger_type,
        })
        if self.error is not None:
            raise self.error
        if callable(self.result):
            out = self.result(context, trigger_type)
        elif self.result is not None:
            out = self.result
        else:
            out = VisionAnalysisPayload(category="empty", overall_confidence=0.9, scene_note_pt="Divisão vazia.")
        return out if isinstance(out, VisionAnalysisPayload) else VisionAnalysisPayload.model_validate(out)

    def warmup(self) -> bool:
        return True

    def close(self) -> None:
        pass


def make_analyzer(settings: Settings) -> VisionAnalyzer:
    """Create the default OpenAI-compatible (Ollama) vision analyzer from Settings."""
    return OpenAIVisionAnalyzer(
        base_url=settings.vision_base_url,
        model_name=settings.vision_model,
        api_key=settings.vision_api_key,
        timeout_s=settings.vision_timeout_s,
        reasoning_effort=settings.vision_reasoning_effort,
        keep_alive=settings.vision_keep_alive,
    )

