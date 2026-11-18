"""Test helpers: a fake chat client (no LLM) and a real A2A server on a free local port."""

import socket
import threading
import time

import uvicorn
from agent_framework import BaseChatClient, ChatResponse, Message

from src.agents.server import build_app
from src.agents.specs import ALL_AGENTS


class FakeChat(BaseChatClient):
    """Answers every prompt with `reply` (or raises `error`); records the prompts."""

    def __init__(self, reply: str = '{"resumo": "Nada a fazer.", "propostas": []}', error: str | None = None):
        super().__init__()
        self.reply, self.error, self.prompts, self.options = reply, error, [], []

    def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.options.append(dict(options or {}))
        system = str((options or {}).get("instructions") or "")  # the real client sends it as the system message
        self.prompts.append(system + "\n" + "\n".join(m.text or "" for m in messages))

        async def respond():
            if self.error:
                raise RuntimeError(self.error)
            return ChatResponse(messages=[Message("assistant", [self.reply])])
        return respond()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class A2AServer:
    """Serves one agent over real A2A (HTTP JSON-RPC) in a background thread."""

    def __init__(self, key: str, chat: FakeChat, port: int | None = None, options: dict | None = None):
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        self.chat = chat
        app = build_app(ALL_AGENTS[key], self.url, chat, options)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        t0 = time.time()
        while not self.server.started and time.time() - t0 < 10:
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)
