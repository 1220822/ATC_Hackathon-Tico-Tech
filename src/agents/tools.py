"""Read-only tools of the LLM agents. They run in the agents' process and only do HTTP GETs
against the API (settings.api_url): no MCP access, no writes."""

from typing import Annotated, Any

import httpx
from agent_framework import tool
from pydantic import Field

_API = {"url": "http://127.0.0.1:8000"}


def configure(api_url: str) -> None:
    _API["url"] = api_url.rstrip("/")


async def _get(path: str, **params: Any) -> Any:
    params = {k: v for k, v in params.items() if v is not None}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(_API["url"] + path, params=params)
        if r.status_code >= 400:
            return {"erro": (r.json().get("detail") if r.headers.get("content-type", "").startswith(
                "application/json") else r.text)[:300]}
        return r.json()
    except httpx.HTTPError as e:
        return {"erro": f"API indisponível: {e}"}


def _compact_room(r: dict) -> dict:
    return {"divisao": r.get("nome"), "id": r.get("id"), "simulado": r.get("simulado"),
            "setpoint_c": r.get("setpoint_c"), "sensores": r.get("sensores"),
            "dispositivos": [{"id": d["id"], "nome": d["nome"], "papel": d["papel"], "online": d["online"]}
                             for d in r.get("dispositivos", [])]}


@tool(description="Estado atual de todas as divisões da casa (sensores, dispositivos, online/offline).")
async def estado_casa() -> Any:
    rooms = await _get("/rooms")
    return [_compact_room(r) for r in rooms] if isinstance(rooms, list) else rooms


@tool(description="Estado atual de uma divisão (nome ou id), com sensores e dispositivos.")
async def estado_divisao(divisao: Annotated[str, Field(description="Nome ou id da divisão, ex.: Quarto")]) -> Any:
    r = await _get(f"/rooms/{divisao}")
    return _compact_room(r) if "erro" not in r else r


@tool(description="Consumo elétrico estimado (kWh) por hora e por divisão nas últimas horas.")
async def consumo_por_hora(
    divisao: Annotated[str | None, Field(description="Nome ou id da divisão; vazio = todas")] = None,
    horas: Annotated[int, Field(ge=1, le=168, description="Quantas horas para trás")] = 24,
) -> Any:
    return await _get("/tools/consumo_por_hora", divisao=divisao, horas=horas)


@tool(description="Temperatura média por intervalo e tendência (°C/h) de uma divisão, e o seu setpoint.")
async def tendencia_temperatura(
    divisao: Annotated[str, Field(description="Nome ou id da divisão")],
    horas: Annotated[float, Field(ge=0.25, le=72, description="Janela em horas")] = 3,
) -> Any:
    return await _get("/tools/tendencia_temperatura", divisao=divisao, horas=horas)


TOOLS_BY_AGENT = {
    "seguranca": [estado_casa, estado_divisao],
    "energia": [estado_casa, estado_divisao, consumo_por_hora],
    "conforto": [estado_casa, estado_divisao, tendencia_temperatura],
}
