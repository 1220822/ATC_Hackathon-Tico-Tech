"""Structured output from any OpenAI-compatible endpoint (LLM_BASE_URL, LLM_MODEL, LLM_API_KEY).

    llm = LLM.from_settings(settings)
    intent = llm.structured(SYSTEM, user_text, Intent)     # -> validated Pydantic object

- asks for JSON with the model's JSON schema (response_format json_schema; servers that refuse it
  get a plain request), then extracts the JSON (tolerates <think> and code fences) and validates
  it with Pydantic; one retry with the validation error;
- raises LLMError when there is no valid answer, so callers can fall back to rules;
- tests replace the transport with a function: LLM(complete=lambda messages, schema: '{"...": ...}').
"""

import json
import logging
import time
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from .json_utils import json_candidates

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
Complete = Callable[[list[dict], dict | None], str]


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_url: str = "", model: str = "", api_key: str = "local", timeout_s: float = 60.0,
                 reasoning_effort: str = "none", complete: Complete | None = None):
        self.base_url, self.model, self.api_key = base_url, model, api_key or "local"
        self.timeout_s, self.reasoning_effort = timeout_s, reasoning_effort
        self._complete = complete
        self._client = None
        self._schema_ok = True  # flips to False if the server refuses response_format json_schema
        self.last_latency_s: float | None = None

    @classmethod
    def from_settings(cls, settings) -> "LLM":
        return cls(settings.llm_base_url, settings.llm_model, settings.llm_api_key,
                   timeout_s=getattr(settings, "chat_timeout_s", 60.0),
                   reasoning_effort=settings.llm_reasoning_effort)

    # -- transport -------------------------------------------------------------------
    def _openai(self, messages: list[dict], schema: dict | None, effort: str | None = None) -> str:
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout_s, max_retries=0)
        kwargs: dict = {"model": self.model, "messages": messages, "temperature": 0.1}
        effort = self.reasoning_effort if effort is None else effort
        if effort:
            kwargs["reasoning_effort"] = effort
        if schema is not None and self._schema_ok:
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": "resposta", "schema": schema}}
        try:
            r = self._client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            if "response_format" in kwargs and _is_bad_request(e):
                self._schema_ok = False
                kwargs.pop("response_format")
                r = self._client.chat.completions.create(**kwargs)
            elif "reasoning_effort" in kwargs and _is_bad_request(e):
                kwargs.pop("reasoning_effort")
                r = self._client.chat.completions.create(**kwargs)
            else:
                raise
        return r.choices[0].message.content or ""

    def complete(self, messages: list[dict], schema: dict | None = None, reasoning_effort: str | None = None) -> str:
        """reasoning_effort overrides LLM_REASONING_EFFORT for this call (e.g. "medium" to think it over)."""
        t = time.time()
        try:
            if self._complete is not None:
                return self._complete(messages, schema)
            return self._openai(messages, schema, reasoning_effort)
        finally:
            self.last_latency_s = round(time.time() - t, 2)

    def warmup_async(self) -> None:
        """Load the model in the background (the first call on a cold Ollama takes ~15 s)."""
        import threading

        def run():
            try:
                self.complete([{"role": "user", "content": "Responde só: ok"}], None)
                log.info("LLM pronto (%s, %.1f s)", self.model, self.last_latency_s or 0)
            except Exception as e:  # noqa: BLE001
                log.warning("LLM warmup failed: %s", e)
        threading.Thread(target=run, name="llm-warmup", daemon=True).start()

    # -- structured ------------------------------------------------------------------
    def structured(self, system: str, user: str, model: type[T], retries: int = 1,
                   reasoning_effort: str | None = None) -> T:
        schema = model.model_json_schema()
        messages = [{"role": "system", "content": system + "\n\nResponde APENAS com um objeto JSON que siga "
                     "este schema:\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))},
                    {"role": "user", "content": user}]
        last_err = "sem resposta"
        for attempt in range(retries + 1):
            try:
                text = self.complete(messages, schema, reasoning_effort)
            except Exception as e:  # noqa: BLE001 - down / timeout
                raise LLMError(f"LLM indisponível ({type(e).__name__}: {str(e)[:120]})") from None
            try:
                return parse_model(text, model)
            except ValueError as e:
                last_err = str(e)
                log.info("LLM structured retry (%d): %s", attempt + 1, last_err[:200])
                messages = messages + [{"role": "assistant", "content": text[:2000]},
                                       {"role": "user", "content": f"O JSON não é válido: {last_err[:300]}. "
                                                                   "Responde só com o JSON corrigido."}]
        raise LLMError(f"resposta inválida do LLM: {last_err[:200]}")


def parse_model(text: str, model: type[T]) -> T:
    err = "o modelo não devolveu JSON"
    for cand in json_candidates(text or ""):
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            try:
                return model.model_validate(data)
            except ValidationError as e:
                first = e.errors()[0]
                err = f"{'.'.join(str(x) for x in first['loc'])}: {first['msg']}"
    raise ValueError(err)


def _is_bad_request(e: Exception) -> bool:
    return getattr(e, "status_code", None) in (400, 422) or "400" in str(e)[:40]
