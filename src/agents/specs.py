"""The LLM agents: three specialists (Segurança, Energia, Conforto) and the Coordinator, who they
are, what they answer, and the reply formats shared by the A2A servers (src.agents.server) and
the Coordinator's bridge.

Specialists only PROPOSE. They see the house through read-only tools and reply with JSON:

    {"resumo": "<one PT-PT sentence for the timeline>",
     "propostas": [{"device_id": "<id from the list>", "acao": "on" | "off", "motivo": "<short>"}]}

The Coordinator LLM decides on each proposal:

    {"resumo": "<one sentence>", "decisoes": [{"n": 1, "aprovar": true | false, "motivo": "<short>"}]}

and src.coordinator applies hard guardrails (device/action allowed, smoke emergency, recent
higher-priority actions) before it executes, being the only component that writes.
Smoke never reaches the LLMs: the emergency path is deterministic (src.coordinator).
"""

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..json_utils import json_candidates as _json_candidates

COMMON = """
Regras:
- Escreve sempre em português de Portugal (PT-PT), frases curtas, sem emojis nem markdown.
- Só podes PROPOR ligar ou desligar dispositivos da lista "Dispositivos acionáveis" que vem na mensagem
  (luzes e tomadas). Nunca proponhas armar ou desarmar a segurança, nem outros dispositivos.
- Não inventes valores: usa os dados da mensagem e, se precisares de mais, as ferramentas (só leitura).
- Os sensores podem estar em divisões diferentes (ex.: janela na Sala, aquecedor no Quarto); diz sempre
  a divisão de cada coisa.
- Se não houver nada a fazer, devolve "propostas": [].
- O "resumo" descreve a situação numa frase; não repitas nele as propostas (vão no campo "propostas").
- A secção "Memória" traz o histórico da casa (tendência das últimas 2 h, consumos, eventos), as
  preferências ativas do utilizador (cada uma com um id) e as últimas decisões semelhantes com a resposta
  do utilizador. Respeita as preferências; se uma proposta for contra uma, diz qual. Se o utilizador
  rejeitou decisões iguais, não insistas sem um facto novo.
- Em cada proposta, "respects_preferences" e "conflicts_with_preferences" são listas de ids de
  preferências da Memória (vazias se nenhuma se aplica). Nunca inventes ids.

Responde APENAS com um objeto JSON, sem texto antes nem depois:
{"resumo": "<uma frase para a linha do tempo>", "propostas": [{"device_id": "<id>", "acao": "on" ou "off", "motivo": "<frase curta>", "respects_preferences": [], "conflicts_with_preferences": []}]}
"""

COORDINATOR_RULES = """
Como decidir:
- Prioridades: segurança das pessoas; depois pedidos do utilizador; depois Segurança, Energia e Conforto,
  por esta ordem.
- Aprova só se o motivo da proposta for confirmado por um facto concreto dos eventos ou do estado da
  casa (ex.: janela aberta e aquecedor ligado). Motivos vagos ("poupar", "conforto") sem esse facto:
  recusa. Na dúvida, recusa: as decisões aprovadas são executadas na casa real.
- A casa é um só espaço: uma janela aberta numa divisão (ex.: Sala) arrefece as outras (ex.: Quarto).
  Aquecer com uma janela aberta em qualquer divisão é desperdício, mesmo que o aquecedor esteja a 0 W
  nesse instante (pode voltar a aquecer a qualquer momento).
- Nunca inventes factos para justificar uma decisão (ex.: presença ou ausência de pessoas, horários).
- Uma luz acesa não é desperdício: não sabes se há alguém na divisão, por isso recusa desligar luzes
  por poupança ou conforto.
- Não contraries uma ação recente (secção "Decisões recentes") de quem tem mais prioridade, nem
  ligues e desligues o mesmo aparelho em pouco tempo.
- Se duas propostas se contradizem, aprova no máximo uma.
- As preferências do utilizador (secção "Preferências") desempatam entre Conforto e Energia: aprova a
  proposta que as respeita e cita a preferência no motivo.
- Não sabes se há pessoas em casa além do que os dados dizem.

Regras:
- Escreve sempre em português de Portugal (PT-PT), frases curtas, sem emojis nem markdown.
- Nunca aproves armar ou desarmar a segurança (essas propostas nem te chegam).
- Decide TODAS as propostas, pelo número.

Responde APENAS com um objeto JSON, sem texto antes nem depois:
{"resumo": "<uma frase>", "decisoes": [{"n": <número da proposta>, "aprovar": true ou false, "motivo": "<frase curta>"}]}
"""


@dataclass(frozen=True)
class AgentSpec:
    key: str  # seguranca | energia | conforto
    name: str  # shown in the UI
    port_offset: int
    description: str
    instructions: str
    priority: int  # lower wins in conflicts (Emergência 0, Utilizador 1)
    rules: str = COMMON  # reply format and rules appended to the instructions


AGENTS: dict[str, AgentSpec] = {
    "seguranca": AgentSpec(
        key="seguranca", name="Segurança", port_offset=1, priority=2,
        description="Avalia intrusões, contactos abertos com a casa armada, movimento na câmara e manipulação.",
        instructions="És o agente de Segurança de uma casa inteligente. Avalias se um evento é uma ameaça "
                     "(janela/porta aberta com a casa armada, movimento ou som na câmara, manipulação, alarme). "
                     "Se receberes um evento camera_analysis, usa a confirmação visual (pessoas, janela/porta, fumo) "
                     "para avaliar se é alarme real ou falso alarme; se não houver pessoas e parecer falso alarme, "
                     "devolve propostas vazias. Podes propor acender uma luz para dissuadir, se fizer sentido. "
                     "Explica o risco numa frase.",
    ),
    "energia": AgentSpec(
        key="energia", name="Energia", port_offset=2, priority=3,
        description="Evita desperdício: aquecedor ligado com janela aberta, consumos altos, tomadas esquecidas.",
        instructions="És o agente de Energia de uma casa inteligente. O teu objetivo é evitar desperdício: "
                     "aquecimento ligado com uma janela aberta, consumos altos sem necessidade. Podes usar o "
                     "consumo por hora. Propõe desligar o que estiver a desperdiçar energia. Não proponhas desligar uma "
                     "luz só por estar acesa: não sabes se há alguém na divisão.",
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


COORDINATOR = AgentSpec(
    key="coordenador", name="Coordenador", port_offset=0, priority=1, rules=COORDINATOR_RULES,
    description="Decide, proposta a proposta, o que os agentes Segurança, Energia e Conforto pedem.",
    instructions="És o Coordenador de uma casa inteligente. Os agentes especialistas (Segurança, Energia, "
                 "Conforto) propõem ligar ou desligar luzes e tomadas; tu decides se cada proposta se executa, "
                 "olhando para a casa toda e para o que foi decidido há pouco.",
)
ALL_AGENTS: dict[str, AgentSpec] = {"coordenador": COORDINATOR, **AGENTS}  # everything served over A2A


def instructions_for(spec: AgentSpec) -> str:
    return spec.instructions + "\n" + spec.rules


class Proposal(BaseModel):
    device_id: str = Field(min_length=1)
    acao: Literal["on", "off"]
    motivo: str = Field("", max_length=300)
    respects_preferences: list[str] = Field(default_factory=list, max_length=10)  # preference ids
    conflicts_with_preferences: list[str] = Field(default_factory=list, max_length=10)


class AgentReply(BaseModel):
    resumo: str = Field(min_length=1, max_length=400)
    propostas: list[Proposal] = Field(default_factory=list, max_length=4)


class Verdict(BaseModel):
    n: int = Field(ge=1)
    aprovar: bool
    motivo: str = Field("", max_length=300)


class CoordinatorReply(BaseModel):
    resumo: str = Field("", max_length=400)
    decisoes: list[Verdict] = Field(default_factory=list, max_length=12)


class BadReply(ValueError):
    pass


def parse_reply(text: str, model: type[BaseModel] = AgentReply):
    """Agent text -> model (AgentReply or CoordinatorReply). Tolerates code fences, <think> blocks
    and text around the JSON."""
    for cand in _json_candidates(text or ""):
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            try:
                return model.model_validate(data)
            except ValidationError as e:
                raise BadReply(f"JSON do agente inválido: {e.errors()[0]['msg']}") from None
    raise BadReply("o agente não devolveu JSON")
