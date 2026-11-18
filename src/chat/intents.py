"""What the user wants from one chat message (structured output, validated with Pydantic).

classify(text, llm, rooms) asks the LLM for a ChatIntent; when the LLM is down or answers nonsense,
classify_rules() (keywords + regex, PT-PT) does the job, and its extracted fields fill whatever the
LLM left empty. Nothing here acts on the house.
"""

import logging
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

IntentName = Literal["pergunta_estado", "alterar_preferencia", "criar_regra", "configurar_layout",
                     "responder_proposta", "responder_sugestao", "comando_dispositivo", "outro"]
Topic = Literal["temperatura", "humidade", "janela", "energia", "luz", "aquecedor", "seguranca", "geral",
                "sugestoes", "regras", "preferencias"]
Relation = Literal["esquerda", "direita", "frente", "tras", "junto_janela", "junto_porta", "centro", "canto",
                   "ao_lado"]


class PrefRequest(BaseModel):
    tipo: Literal["setpoint_c", "comfort_range_c", "humidity_max_pct", "autonomy", "quiet_hours", "note"]
    valor_c: float | None = None
    min_c: float | None = None
    max_c: float | None = None
    pct: float | None = None
    inicio: str | None = Field(None, description="HH:MM")
    fim: str | None = Field(None, description="HH:MM")
    categoria: Literal["energia", "conforto", "seguranca", "luz"] | None = None
    nivel: Literal["auto", "perguntar", "nunca"] | None = None
    texto: str | None = None


class RuleCondition(BaseModel):
    sensor: Literal["janela", "temperatura", "humidade", "aquecedor", "luz"]
    divisao: str | None = None
    operador: Literal["=", ">", "<"] = "="
    valor: str | float | None = Field(None, description="aberta/fechada, ligado/desligado, ou um número")


class RuleAction(BaseModel):
    dispositivo: Literal["luz", "aquecedor"]
    divisao: str | None = None
    acao: Literal["ligar", "desligar", "cor"]
    cor: str | None = None


class RuleRequest(BaseModel):
    condicoes: list[RuleCondition] = Field(default_factory=list, max_length=4)
    acoes: list[RuleAction] = Field(default_factory=list, max_length=3)
    avisar: bool = False


class LayoutRequest(BaseModel):
    alvo: str = Field(description="nome da divisão ou do dispositivo a mover")
    relacao: Relation
    referencia: str | None = Field(None, description="divisão ou dispositivo de referência")


Wall = Literal["tras", "frente", "esquerda", "direita"]


class OpeningRequest(BaseModel):
    """A window (or door) of the floor plan: add, change or remove it (3D only, never the MCP)."""
    acao: Literal["adicionar", "alterar", "remover"]
    tipo: Literal["janela", "porta"] = "janela"
    divisao: str | None = None
    parede: Wall | None = Field(None, description="parede onde fica (ou onde está, ao alterar/remover)")
    para_parede: Wall | None = Field(None, description="ao alterar: parede para onde passa")
    posicao: Literal["inicio", "centro", "fim"] | None = Field(
        None, description="ao longo da parede: inicio = esquerda/fundo, fim = direita/frente")
    largura_m: float | None = Field(None, gt=0.2, le=6)
    altura_m: float | None = Field(None, gt=0.2, le=3)
    peitoril_m: float | None = Field(None, ge=0, le=2)
    delta_largura_m: float | None = Field(None, ge=-3, le=3, description="mais larga (+) / mais estreita (-)")
    delta_altura_m: float | None = Field(None, ge=-2, le=2)


class CommandRequest(BaseModel):
    dispositivo: Literal["luz", "aquecedor", "seguranca"]
    divisao: str | None = None
    acao: Literal["ligar", "desligar", "armar_casa", "armar_ausente", "desarmar"]


class ChatIntent(BaseModel):
    intent: IntentName
    divisao: str | None = None
    topico: Topic | None = None
    periodo: Literal["agora", "hoje", "ontem", "semana"] | None = None
    preferencia: PrefRequest | None = None
    regra: RuleRequest | None = None
    layout: LayoutRequest | None = None
    abertura: OpeningRequest | None = None
    comando: CommandRequest | None = None
    resposta: Literal["aprovar", "rejeitar", "alterar", "silenciar"] | None = None
    sempre: bool = Field(False, description="«faz sempre assim»: passa a fazer esta decisão sozinha")
    comentario: str | None = None
    origem: Literal["llm", "regras"] = "regras"


SYSTEM = """És o classificador do chat de uma casa inteligente (português de Portugal).
Classifica a mensagem do utilizador numa intenção e extrai os campos. Intenções:
- pergunta_estado: perguntas sobre a casa agora ou no passado ("a janela da sala está fechada?",
  "quanto gastei ontem?", "tens alguma sugestão?" -> topico "sugestoes").
- alterar_preferencia: muda um parâmetro ou o modo de agir: temperatura alvo (setpoint_c, com
  inicio/fim se houver horário: "de manhã" = 07:00-09:00, "à noite" = 21:00-23:59), intervalo de conforto,
  humidade máxima, autonomia (categoria energia|conforto|seguranca|luz e nivel auto|perguntar|nunca),
  horas de silêncio (quiet_hours), nota livre.
- criar_regra: "se ... então/avisa-me/liga/desliga ..." (condições com sensores e ações).
- configurar_layout: onde ficam divisões ou dispositivos no 3D ("o quarto fica à direita da sala",
  "o sensor de temperatura está junto à janela"). Também janelas e portas da planta: acrescentar,
  mudar (parede, posição ao longo da parede, largura, altura) ou remover -> preenche "abertura"
  ("acrescenta uma janela na parede da direita do quarto", "a janela da sala tem 1,5 m de largura",
  "passa a janela do quarto para a parede da esquerda", "remove a janela do quarto").
- responder_proposta: aprova, rejeita ou altera uma proposta pendente ("sim, desliga", "não").
- responder_sugestao: responde a uma sugestão ("aceito", "não voltes a sugerir isto" -> resposta silenciar).
- comando_dispositivo: ordem direta ("liga a luz do quarto", "arma a casa").
- outro: qualquer outra coisa.
Divisões da casa: {rooms}. Não inventes valores que a mensagem não tenha."""


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(text or "").casefold()).strip()


# --------------------------------------------------------------------------- extraction

PERIODS = {"manha": ("07:00", "09:00"), "tarde": ("14:00", "19:00"), "noite": ("21:00", "23:59"),
           "madrugada": ("00:00", "07:00")}
COLOURS = {"vermelh": (255, 0, 0), "verde": (0, 255, 0), "azul": (0, 0, 255), "ambar": (255, 140, 0),
           "laranja": (255, 140, 0), "amarel": (255, 200, 0), "branc": (255, 255, 255), "roxo": (160, 0, 255)}


def find_room(text: str, rooms: list[str]) -> str | None:
    t = norm(text)
    for r in sorted(rooms, key=len, reverse=True):
        if re.search(rf"\b{re.escape(norm(r))}\b", t):
            return r
    return None


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def find_temp(t: str) -> float | None:
    m = re.search(r"(\d{1,2}(?:[.,]\d)?)\s*(?:°|º|graus|ºc|°c|c\b)", t)
    if m:
        return _num(m.group(1))
    m = re.search(r"\ba (\d{1,2}(?:[.,]\d)?)\b", t)
    return _num(m.group(1)) if m and 10 <= _num(m.group(1)) <= 30 else None


def _hh(h: str, m: str | None) -> str:
    return f"{int(h) % 24:02d}:{int(m or 0):02d}"


def find_window(t: str) -> tuple[str, str] | None:
    m = re.search(r"\bd[ao]s?\s*(\d{1,2})(?:h|:)?(\d{2})?\s*(?:h\b)?\s*(?:as|ate|a)\s*(\d{1,2})(?:h|:)?(\d{2})?", t)
    if m:
        return _hh(m.group(1), m.group(2)), _hh(m.group(3), m.group(4))
    m = re.search(r"\bentre as\s*(\d{1,2})(?:h|:)?(\d{2})?\s*e as\s*(\d{1,2})(?:h|:)?(\d{2})?", t)
    if m:
        return _hh(m.group(1), m.group(2)), _hh(m.group(3), m.group(4))
    for word, win in PERIODS.items():
        if re.search(rf"\b(de|a|na|durante a|pela) {word}\b", t) or re.search(rf"\b{word}s?\b", t):
            return win
    return None


def find_category(t: str) -> str | None:
    if re.search(r"aquecedor|tomada|energia|consumo|poupa", t):
        return "energia"
    if re.search(r"\bluz|luzes|lampada", t):
        return "luz"
    if re.search(r"seguranca|alarme", t):
        return "seguranca"
    if re.search(r"conforto|temperatura|humidade", t):
        return "conforto"
    return None


def find_level(t: str) -> str | None:
    if re.search(r"nunca|nao mexas|nao toques|proibid", t):
        return "nunca"
    if re.search(r"sozinh|automatic|sem (me )?perguntar|nao precisas de (me )?perguntar|podes (fazer|decidir)|faz sempre", t):
        return "auto"
    if re.search(r"pergunta|pede(-me)? (a )?(aprovacao|autorizacao|confirmacao)|consulta-me", t):
        return "perguntar"
    return None


def _cond_from(part: str, rooms: list[str]) -> list[RuleCondition]:
    conds = []
    room = find_room(part, rooms)
    if re.search(r"janela", part):
        val = "fechada" if re.search(r"fech", part) else "aberta"
        conds.append(RuleCondition(sensor="janela", divisao=room, valor=val))
    for sensor, word in (("temperatura", r"temperatura|graus"), ("humidade", r"humidade")):
        if re.search(word, part):
            m = re.search(r"(abaixo|menos|inferior|baixar|descer|desce|menor|acima|mais|superior|subir|sobe|maior|passar)"
                          r"[^\d]{0,20}(\d{1,3}(?:[.,]\d)?)", part)
            if m:
                op = "<" if m.group(1) in ("abaixo", "menos", "inferior", "baixar", "descer", "desce", "menor") else ">"
                conds.append(RuleCondition(sensor=sensor, divisao=room, operador=op, valor=_num(m.group(2))))
    if re.search(r"aquecedor|tomada", part):
        val = "desligado" if re.search(r"deslig", part) else "ligado"
        conds.append(RuleCondition(sensor="aquecedor", divisao=room, valor=val))
    return conds


def _actions_from(part: str, rooms: list[str]) -> list[RuleAction]:
    acts = []
    room = find_room(part, rooms)
    for word, rgb in COLOURS.items():
        if re.search(rf"luz.{{0,25}}{word}|{word}.{{0,10}}luz", part):
            acts.append(RuleAction(dispositivo="luz", divisao=room, acao="cor", cor=word))
            break
    else:
        if re.search(r"(acende|liga)\w* (a )?luz", part):
            acts.append(RuleAction(dispositivo="luz", divisao=room, acao="ligar"))
        elif re.search(r"apaga\w* (a )?luz|deslig\w* (a )?luz", part):
            acts.append(RuleAction(dispositivo="luz", divisao=room, acao="desligar"))
    if re.search(r"deslig\w* (o )?(aquecedor|tomada)", part):
        acts.append(RuleAction(dispositivo="aquecedor", divisao=room, acao="desligar"))
    elif re.search(r"\bliga\w* (o )?(aquecedor|tomada)", part):
        acts.append(RuleAction(dispositivo="aquecedor", divisao=room, acao="ligar"))
    return acts


def extract_rule(text: str, rooms: list[str]) -> RuleRequest:
    t = norm(text)
    m = re.match(r"^(?:quero que )?(?:se|quando|sempre que)\s+(.*?)(?:,|\bentao\b|\b(?=avisa|liga|desliga|acende|apaga|poe|mete|notifica))(.*)$", t)
    cond_part, act_part = (m.group(1), m.group(2)) if m else (t, t)
    # "com o aquecedor ligado" inside the condition stays a condition
    conds = _cond_from(cond_part, rooms)
    acts = _actions_from(act_part, rooms)
    avisar = bool(re.search(r"avisa|notifica|diz-me|alerta|manda-me", act_part))
    return RuleRequest(condicoes=conds, acoes=acts, avisar=avisar or not acts)


def extract_pref(text: str, rooms: list[str]) -> PrefRequest | None:
    t = norm(text)
    level = find_level(t)
    if re.search(r"nao me (incomodes|chateies)|silencio|sem sugestoes|nao (quero )?sugestoes", t):
        win = find_window(t) or ("23:00", "07:30")
        return PrefRequest(tipo="quiet_hours", inicio=win[0], fim=win[1])
    if level and (find_category(t) or re.search(r"sozinh|pergunt|nunca", t)):
        return PrefRequest(tipo="autonomy", categoria=find_category(t) or "energia", nivel=level)
    if "humidade" in t:
        m = re.search(r"(\d{2})\s*%", t) or re.search(r"(\d{2})\b", t)
        if m:
            return PrefRequest(tipo="humidity_max_pct", pct=float(m.group(1)))
    m = re.search(r"entre (\d{1,2}(?:[.,]\d)?) e (\d{1,2}(?:[.,]\d)?)", t)
    if m and re.search(r"graus|°|º|temperatura|conforto", t):
        return PrefRequest(tipo="comfort_range_c", min_c=_num(m.group(1)), max_c=_num(m.group(2)))
    temp = find_temp(t)
    if temp is not None:
        win = find_window(t)
        return PrefRequest(tipo="setpoint_c", valor_c=temp, inicio=win[0] if win else None, fim=win[1] if win else None)
    if re.search(r"^(lembra-te|nota|toma nota|fica a saber)", t):
        return PrefRequest(tipo="note", texto=text.split(":", 1)[-1].strip() or text)
    return None


_WALL_WORDS = ((r"tras|fundo|norte", "tras"), (r"frente|sul", "frente"), (r"esquerd|oeste", "esquerda"),
               (r"direit|este\b|leste", "direita"))


def _wall_of(word: str) -> str | None:
    for pat, wall in _WALL_WORDS:
        if re.match(pat, word):
            return wall
    return None


def _metres(num: str, unit: str | None) -> float:
    v = _num(num)
    return v / 100 if unit and unit.startswith("c") else v


def extract_opening(text: str, rooms: list[str]) -> OpeningRequest | None:
    """«acrescenta uma janela na parede da direita do quarto», «a janela da sala tem 1,5 m de largura»,
    «passa a janela do quarto para a parede da esquerda», «remove a janela do quarto»."""
    t = norm(text)
    m = re.search(r"\b(janelas?|portas?)\b", t)
    if not m:
        return None
    tipo = "porta" if m.group(1).startswith("porta") else "janela"
    if re.search(r"\b(acrescent|adicion|poe|coloca|cria|mete|instala|quero|falta|ha|tem|existe)\w*\b.*\b(uma|outra|nova) (janela|porta)", t):
        acao = "adicionar"
    elif re.search(r"\b(remove|tira|apaga|elimina|retira)\w*", t) or re.search(r"nao (ha|tem|existe) (nenhuma )?(janela|porta)", t):
        acao = "remover"
    elif re.search(r"\b(maior|menor|mais (larga|estreita|alta|baixa|comprida|curta)|largura|altura|peitoril|muda|move|passa|"
                   r"desloca|aumenta|diminui|alarga|estreita|encolhe)\w*\b|\d\s*(m|cm|metros?)\b", t):
        acao = "alterar"
    else:
        return None
    req: dict = {"acao": acao, "tipo": tipo, "divisao": find_room(text, rooms)}
    rest = t
    to = re.search(r"para (?:a )?parede (?:da |de |do )?(\w+)", rest)
    if to:
        req["para_parede"] = _wall_of(to.group(1))
        rest = rest.replace(to.group(0), " ")
    at = re.search(r"parede (?:da |de |do )?(\w+)", rest)
    if at:
        req["parede"] = _wall_of(at.group(1))
        rest = rest.replace(at.group(0), " ")
    elif acao != "alterar":  # «uma janela à direita no quarto»: the side is the wall
        side = re.search(r"\b(?:do lado|no lado|a|ao) (direit|esquerd|frente|fundo|tras)\w*", rest)
        if side:
            req["parede"] = _wall_of(side.group(1))
            rest = rest.replace(side.group(0), " ")
    if re.search(r"\b(ao centro|no centro|no meio|a meio)\b", rest):
        req["posicao"] = "centro"
    elif re.search(r"\b(a|ao canto|para a|mais a) (esquerda|tras)\b|\bao fundo\b|canto esquerdo", rest):
        req["posicao"] = "inicio"
    elif re.search(r"\b(a|ao canto|para a|mais a) (direita|frente)\b|canto direito", rest):
        req["posicao"] = "fim"
    w = (re.search(r"(\d+(?:[.,]\d+)?)\s*(m|metros?|cm)?\s*(?:de )?(?:largura|de largo)", t)
         or re.search(r"largura (?:de |para )?(\d+(?:[.,]\d+)?)\s*(m|metros?|cm)?", t))
    h = (re.search(r"(\d+(?:[.,]\d+)?)\s*(m|metros?|cm)?\s*(?:de )?(?:altura|de alto)", t)
         or re.search(r"altura (?:de |para )?(\d+(?:[.,]\d+)?)\s*(m|metros?|cm)?", t))
    s = re.search(r"peitoril (?:de |a |para )?(\d+(?:[.,]\d+)?)\s*(m|metros?|cm)?", t)
    if w:
        req["largura_m"] = _metres(w.group(1), w.group(2))
    if h:
        req["altura_m"] = _metres(h.group(1), h.group(2))
    if s:
        req["peitoril_m"] = _metres(s.group(1), s.group(2))
    if not w and not h:
        plain = re.search(r"\b(?:com|tem|de|para) (\d+(?:[.,]\d+)?)\s*(m|metros?|cm)\b", t)
        if plain:
            req["largura_m"] = _metres(plain.group(1), plain.group(2))
    if re.search(r"\b(maior|mais larga|mais comprida|alarga|aumenta)\w*", t) and "largura_m" not in req:
        req["delta_largura_m"] = 0.3
    if re.search(r"\b(menor|mais estreita|mais curta|estreita|encolhe|diminui)\w*", t) and "largura_m" not in req:
        req["delta_largura_m"] = -0.3
    if re.search(r"mais alta", t) and "altura_m" not in req:
        req["delta_altura_m"] = 0.2
    if re.search(r"mais baixa", t) and "altura_m" not in req:
        req["delta_altura_m"] = -0.2
    try:
        return OpeningRequest(**req)
    except ValueError:
        return None


def extract_layout(text: str, rooms: list[str]) -> LayoutRequest | None:
    t = norm(text)
    rel = None
    for pat, r in ((r"a direita", "direita"), (r"a esquerda", "esquerda"), (r"em frente|a frente", "frente"),
                   (r"atras|por tras", "tras"), (r"junto (a|da|de) janela|perto da janela|ao pe da janela", "junto_janela"),
                   (r"junto (a|da|de) porta|perto da porta", "junto_porta"), (r"no centro|no meio", "centro"),
                   (r"no canto", "canto"), (r"ao lado", "ao_lado")):
        if re.search(pat, t):
            rel = r
            break
    if rel is None:
        return None
    targets = []
    for word, name in ((r"sensor de temperatura|termometro", "sensor de temperatura"), (r"\bluz|lampada", "luz"),
                       (r"aquecedor|tomada", "aquecedor"), (r"camara", "câmara"), (r"detetor de fumo|sensor de fumo", "detetor de fumo"),
                       (r"sensor de (contacto|janela)", "sensor de contacto"), (r"central|hub", "central")):
        if re.search(word, t):
            targets.append(name)
    room_hits = [r for r in rooms if re.search(rf"\b{re.escape(norm(r))}\b", t)]
    room_hits.sort(key=lambda r: t.index(norm(r)))
    if targets:
        return LayoutRequest(alvo=targets[0], relacao=rel, referencia=room_hits[0] if room_hits else None)
    if room_hits:
        return LayoutRequest(alvo=room_hits[0], relacao=rel, referencia=room_hits[1] if len(room_hits) > 1 else None)
    return None


def extract_command(text: str, rooms: list[str]) -> CommandRequest | None:
    t = norm(text)
    if re.search(r"^(arma|ativa) (a casa|o alarme)", t):
        return CommandRequest(dispositivo="seguranca", acao="armar_ausente" if re.search(r"sai|ausente|fora", t) else "armar_casa")
    if re.search(r"^(desarma|desativa) (a casa|o alarme)", t):
        return CommandRequest(dispositivo="seguranca", acao="desarmar")
    m = re.match(r"^(?:podes |consegues )?(liga|desliga|acende|apaga)r?\s+(?:a |o )?(luz|aquecedor|tomada)", t)
    if m:
        on = m.group(1) in ("liga", "acende")
        return CommandRequest(dispositivo="luz" if m.group(2) == "luz" else "aquecedor",
                              divisao=find_room(text, rooms), acao="ligar" if on else "desligar")
    return None


def topic_of(t: str) -> str:
    for pat, topic in ((r"sugest", "sugestoes"), (r"regra", "regras"), (r"o que sabes|preferenc", "preferencias"),
                       (r"janela|porta|fechad|abert", "janela"), (r"humidade|humido", "humidade"),
                       (r"gast|consum|energia|kwh|fatura|eletricidade|custo", "energia"),
                       (r"temperatura|graus|frio|calor|quente", "temperatura"), (r"aquecedor|tomada", "aquecedor"),
                       (r"\bluz|luzes", "luz"), (r"alarme|seguranca|armad", "seguranca")):
        if re.search(pat, t):
            return topic
    return "geral"


def period_of(t: str) -> str:
    if "ontem" in t:
        return "ontem"
    if re.search(r"semana|ultimos dias", t):
        return "semana"
    if re.search(r"\bhoje\b|desde manha|esta manha", t):
        return "hoje"
    return "agora"


def classify_rules(text: str, rooms: list[str], pending_decision: bool = False,
                   pending_suggestion: bool = False) -> ChatIntent:
    t = norm(text)
    room = find_room(text, rooms)
    yes = re.match(r"^(sim|aprovo|aprova|ok|pode ser|faz isso|claro|aceito|aceita|confirmo|avanca|isso)\b", t)
    no = re.match(r"^(nao|rejeito|rejeita|deixa|cancela|esquece|agora nao)\b", t)
    if re.search(r"(faz|fazes|faca) (sempre|isto sempre|isso sempre) assim|sempre assim|a partir de agora (faz|fazes|podes)"
                 r"|faz (isto|isso) sozinh|passa a fazer (isto|isso)", t) and not find_category(t):
        return ChatIntent(intent="responder_proposta", resposta="aprovar", sempre=True)
    if re.search(r"nao (voltes a|precisas de) sugerir|nao sugiras mais|nunca mais sugiras", t):
        return ChatIntent(intent="responder_sugestao", resposta="silenciar")
    if find_level(t) in ("auto", "nunca") or re.search(r"sem (me )?perguntar|pergunta-me|pede-me", t):
        pref = extract_pref(text, rooms)
        if pref is not None and pref.tipo == "autonomy":
            return ChatIntent(intent="alterar_preferencia", divisao=room, preferencia=pref)
    if pending_decision and (yes or no):
        return ChatIntent(intent="responder_proposta", resposta="aprovar" if yes else "rejeitar",
                          comentario=text if len(t) > 12 else None)
    if pending_suggestion and (yes or no):
        return ChatIntent(intent="responder_sugestao", resposta="aprovar" if yes else "rejeitar")
    if re.match(r"^(se|quando|sempre que|quero que (se|quando))\b", t) and re.search(
            r"avisa|notifica|liga|desliga|acende|apaga|alerta|luz|aquecedor", t):
        return ChatIntent(intent="criar_regra", divisao=room, regra=extract_rule(text, rooms))
    op = extract_opening(text, rooms) if not t.endswith("?") else None
    if op is not None:
        return ChatIntent(intent="configurar_layout", divisao=room, abertura=op)
    lay = extract_layout(text, rooms)
    if lay is not None and not t.endswith("?"):
        return ChatIntent(intent="configurar_layout", divisao=room, layout=lay)
    cmd = extract_command(text, rooms)
    if cmd is not None:
        return ChatIntent(intent="comando_dispositivo", divisao=room, comando=cmd)
    if not t.endswith("?") and re.search(
            r"\b(quero|prefiro|gosto|define|mete|poe|coloca|muda|altera|mantem|podes|nao me|lembra-te|nota)\b"
            r"|sozinh|nunca|maxim|minim|limite", t):
        pref = extract_pref(text, rooms)
        if pref is not None:
            return ChatIntent(intent="alterar_preferencia", divisao=room, preferencia=pref)
    if t.endswith("?") or re.match(r"^(quanto|qual|quais|como|esta|estao|ha|tens|o que|a que|que|quando|mostra|diz-me)\b", t):
        return ChatIntent(intent="pergunta_estado", divisao=room, topico=topic_of(t), periodo=period_of(t))
    return ChatIntent(intent="outro", divisao=room)


def classify(text: str, llm, rooms: list[str], pending_decision: bool = False,
             pending_suggestion: bool = False) -> ChatIntent:
    """LLM first (structured output), rules as a fallback and to fill missing fields."""
    rules = classify_rules(text, rooms, pending_decision, pending_suggestion)
    if llm is None or rules.sempre:
        return rules
    from ..llm import LLMError
    ctx = []
    if pending_decision:
        ctx.append("Há uma proposta pendente à espera de resposta.")
    if pending_suggestion:
        ctx.append("Há uma sugestão pendente à espera de resposta.")
    try:
        got = llm.structured(SYSTEM.replace("{rooms}", ", ".join(rooms) or "(nenhuma)"),
                             (" ".join(ctx) + "\n" if ctx else "") + f"Mensagem: {text}", ChatIntent)
    except LLMError as e:
        log.info("chat classifier falling back to rules: %s", e)
        return rules
    got.origem = "llm"
    # fields the rules extracted reliably fill what the LLM left out
    if got.intent == rules.intent:
        for f in ("divisao", "topico", "periodo", "preferencia", "regra", "layout", "abertura", "comando", "resposta"):
            if getattr(got, f) is None and getattr(rules, f) is not None:
                setattr(got, f, getattr(rules, f))
        if got.abertura is not None and got.abertura.divisao is None:
            got.abertura.divisao = got.divisao
        if got.intent == "criar_regra" and got.regra is not None and not got.regra.condicoes and rules.regra:
            got.regra = rules.regra
    elif got.intent == "outro" and rules.intent != "outro":
        return rules
    return got
