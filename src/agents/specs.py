"""The three LLM agents (Segurança, Energia, Conforto): who they are, what they answer, and the
reply format shared by the A2A servers (src.agents.server) and the Coordinator's bridge.

Agents only PROPOSE. They see the house through read-only tools and reply with JSON:

    {"resumo": "<one PT-PT sentence for the timeline>",
     "propostas": [{"device_id": "<id from the list>", "acao": "on" | "off", "motivo": "<short>"}]}

The Coordinator decides (guard, priorities, dry-run) and is the only one that writes.
Smoke never reaches them: the emergency path is deterministic (src.coordinator).
"""

import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

COMMON = """
Regras:
- Escreve sempre em português de Portugal (PT-PT), frases curtas, sem emojis nem markdown.
- Só podes PROPOR ligar ou desligar dispositivos da lista "Dispositivos acionáveis" que vem na mensagem
  (luzes e tomadas). Nunca proponhas armar ou desarmar a segurança, nem outros dispositivos.
- Não inventes valores: usa os dados da mensagem e, se precisares de mais, as ferramentas (só leitura).
- Os sensores podem estar em divisões diferentes (ex.: janela na Sala, aquecedor no Quarto); diz sempre
  a divisão de cada coisa.
- Se não houver nada a fazer, devolve "propostas": [].

Responde APENAS com um objeto JSON, sem texto antes nem depois:
{"resumo": "<uma frase para a linha do tempo>", "propostas": [{"device_id": "<id>", "acao": "on" ou "off", "motivo": "<frase curta>"}]}
"""


@dataclass(frozen=True)
class AgentSpec:
    key: str  # seguranca | energia | conforto
    name: str  # shown in the UI
    port_offset: int
    description: str
    instructions: str
    priority: int  # lower wins in conflicts (Emergência 0, Utilizador 1)


AGENTS: dict[str, AgentSpec] = {
    "seguranca": AgentSpec(
        key="seguranca", name="Segurança", port_offset=1, priority=2,
        description="Avalia intrusões, contactos abertos com a casa armada, movimento na câmara e manipulação.",
        instructions="És o agente de Segurança de uma casa inteligente. Avalias se um evento é uma ameaça "
                     "(janela/porta aberta com a casa armada, movimento ou som na câmara, manipulação, alarme). "
                     "Podes propor acender uma luz para dissuadir, se fizer sentido. Explica o risco numa frase.",
    ),
    "energia": AgentSpec(
        key="energia", name="Energia", port_offset=2, priority=3,
        description="Evita desperdício: aquecedor ligado com janela aberta, consumos altos, tomadas esquecidas.",
        instructions="És o agente de Energia de uma casa inteligente. O teu objetivo é evitar desperdício: "
                     "aquecimento ligado com uma janela aberta, consumos altos sem necessidade. Podes usar o "
                     "consumo por hora. Propõe desligar o que estiver a desperdiçar energia.",
    ),
    "conforto": AgentSpec(
        key="conforto", name="Conforto", port_offset=3, priority=4,
        description="Mantém a temperatura perto do setpoint de cada divisão e a humidade razoável.",
        instructions="És o agente de Conforto de uma casa inteligente. Manténs cada divisão perto da sua "
                     "temperatura alvo (setpoint) e com humidade razoável. Usa a tendência de temperatura. "
                     "Se estiver frio e o aquecedor desligado, podes propor ligá-lo; se uma janela aberta "
                     "estiver a arrefecer a casa, diz isso.",
    ),
}
PRIORITY = {"Emergência": 0, "Utilizador": 1, **{a.name: a.priority for a in AGENTS.values()}}


def instructions_for(spec: AgentSpec) -> str:
    return spec.instructions + "\n" + COMMON


class Proposal(BaseModel):
    device_id: str = Field(min_length=1)
    acao: Literal["on", "off"]
    motivo: str = Field("", max_length=300)


class AgentReply(BaseModel):
    resumo: str = Field(min_length=1, max_length=400)
    propostas: list[Proposal] = Field(default_factory=list, max_length=4)


class BadReply(ValueError):
    pass


def _json_candidates(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    yield text
    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.S):
        yield m.group(1)
    # last balanced {...} block
    depth, start = 0, None
    blocks = []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(text[start:i + 1])
    yield from reversed(blocks)


def parse_reply(text: str) -> AgentReply:
    """Agent text -> AgentReply. Tolerates code fences, <think> blocks and text around the JSON."""
    for cand in _json_candidates(text or ""):
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            try:
                return AgentReply.model_validate(data)
            except ValidationError as e:
                raise BadReply(f"JSON do agente inválido: {e.errors()[0]['msg']}") from None
    raise BadReply("o agente não devolveu JSON")
