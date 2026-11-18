"""A2A servers of the LLM agents (Microsoft Agent Framework + a2a-sdk).

    python -m src.agents                    # Segurança :9101, Energia :9102, Conforto :9103
    python -m src.agents --only energia     # one agent per process, if you want them apart

Each agent is an agent_framework Agent backed by the team's OpenAI-compatible LLM server
(LLM_BASE_URL / LLM_MODEL, e.g. qwen3.8:latest), with read-only tools (src.agents.tools), and is
exposed through the A2A protocol (JSON-RPC + agent card) by A2AExecutor. The agents never write
to the house: they answer with proposals that the Coordinator arbitrates.
"""

import asyncio
import logging

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from agent_framework import Agent
from agent_framework.a2a import A2AExecutor
from starlette.applications import Starlette

from ..config import Settings
from . import tools as agent_tools
from .specs import AGENTS, AgentSpec, instructions_for

log = logging.getLogger(__name__)
A2A_PROTOCOL_VERSION = "1.0"


def agent_url(settings: Settings, key: str) -> str:
    return f"http://{settings.agents_host}:{settings.agents_base_port + AGENTS[key].port_offset}/"


def make_chat_client(settings: Settings):
    from agent_framework.openai import OpenAIChatCompletionClient
    return OpenAIChatCompletionClient(model=settings.llm_model, base_url=settings.llm_base_url,
                                      api_key=settings.llm_api_key)


def build_agent(spec: AgentSpec, chat_client) -> Agent:
    return Agent(chat_client, instructions_for(spec), name=spec.name, description=spec.description,
                 tools=agent_tools.TOOLS_BY_AGENT[spec.key],
                 default_options={"temperature": 0.2, "max_tokens": 900})


def agent_card(spec: AgentSpec, url: str) -> AgentCard:
    return AgentCard(
        name=spec.name, description=spec.description, version="1.0.0",
        default_input_modes=["text"], default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(url=url, protocol_binding="JSONRPC",
                                             protocol_version=A2A_PROTOCOL_VERSION)],
        skills=[AgentSkill(id=spec.key, name=spec.name, description=spec.description,
                           tags=["casa", spec.key], examples=[])],
    )


def build_app(spec: AgentSpec, url: str, chat_client) -> Starlette:
    """Starlette app serving one agent over A2A (agent card + JSON-RPC)."""
    card = agent_card(spec, url)
    handler = DefaultRequestHandler(agent_executor=A2AExecutor(build_agent(spec, chat_client)),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")])


async def serve(settings: Settings, keys: list[str], chat_client_factory=make_chat_client) -> None:
    """Run the A2A servers of `keys` in this event loop (one chat client per agent)."""
    agent_tools.configure(settings.api_url)
    servers = []
    for key in keys:
        spec = AGENTS[key]
        url = agent_url(settings, key)
        port = settings.agents_base_port + spec.port_offset
        app = build_app(spec, url, chat_client_factory(settings))
        servers.append(uvicorn.Server(uvicorn.Config(app, host=settings.agents_host, port=port,
                                                     log_level="warning")))
        log.info("agente %s em %s (A2A, modelo %s em %s)", spec.name, url, settings.llm_model,
                 settings.llm_base_url)
    await asyncio.gather(*(s.serve() for s in servers))
