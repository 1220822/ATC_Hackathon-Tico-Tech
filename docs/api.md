# API HTTP

Servidor FastAPI em `src/api.py` (por omissão `http://127.0.0.1:8000`). Swagger interativo em `/docs`.

As divisões aceitam **id ou nome** (`sala`, `Quarto`, …). Todos os tempos são epoch em segundos.

## Páginas

| Método | Caminho | Descrição |
|---|---|---|
| GET | `/` | Visão geral 2D (um cartão por divisão) |
| GET | `/twin` | Casa em 3D + chat |
| GET | `/conhecimento` | "O que a casa sabe de ti" |
| GET | `/simulador` | Painel do simulador |

## Estado da casa

| Método | Caminho | Descrição |
|---|---|---|
| GET | `/rooms` | Divisões com os valores dos sensores |
| GET | `/rooms/status` | Online/offline por dispositivo |
| GET | `/rooms/{id}` | Uma divisão |
| GET | `/home/energy` | Potência atual por divisão + `yesterdayConsumption` do MCP |
| GET | `/historic/rooms/{id}/{metric}?start=&end=` | Leituras em bruto |
| GET | `/historic/aggregated/{agg}/rooms/{id}/{metric}?period_s=&start=&end=` | Agregadas; `agg` = `avg`, `sum`, `min`, `max` |
| GET | `/events` | Eventos guardados |
| GET | `/decisions` | Decisões do Coordenador |

Métricas: `temperature`, `humidity`, `power_w`, `energy_today_kwh`, `energy_yesterday_kwh`, `open`, `on`, `battery`
(os nomes antigos `power`, `contact`, `light` continuam a funcionar).

## Gémeo 3D e layout

| Método | Caminho | Descrição |
|---|---|---|
| GET | `/twin/state?n=30` | Layout + valores dos dispositivos + últimos `n` eventos |
| GET | `/twin/stream` | O mesmo, em direto (Server-Sent Events) |
| GET | `/layout` | Layout atual |
| POST | `/layout/manual` | Grava o layout ajustado à mão |
| POST | `/layout/undo` | Desfaz a última alteração |

## Chat e Coordenador

| Método | Caminho | Corpo | Descrição |
|---|---|---|---|
| POST | `/chat` | `{text}` | Mensagem ao Coordenador → novas mensagens |
| GET | `/chat/messages?after=<id>` | | Histórico (polling) |
| POST | `/chat/clear` | | Limpa a conversa |
| POST | `/chat/action` | `{action, id, response, comment}` | Botões: confirmar/cancelar, decisão, sugestão |
| GET | `/chat/pending` | | Decisões pendentes, pergunta em aberto, sugestões |
| POST | `/coordinator/request` | `{device_id, action: "on"\|"off"}` | Pedido do utilizador (guardrails, dry-run) |
| GET | `/agents/status` | | Agentes A2A vistos pelo Coordenador |

## Conhecimento (RGPD)

| Método | Caminho | Descrição |
|---|---|---|
| GET | `/knowledge` | Preferências, regras, sugestões silenciadas, histórico |
| POST | `/knowledge/prefs/{pid}/edit` | Nova versão de uma preferência |
| POST | `/knowledge/prefs/{pid}/revoke` | Desativa (uma regra desativa também o cenário MCP) |
| POST | `/knowledge/setpoint` | Altera a temperatura alvo |
| POST | `/knowledge/autonomy` | Altera a autonomia de uma categoria |
| POST | `/knowledge/erase` | Apaga tudo (preferências, regras, sugestões, chat, decisões); o histórico dos sensores fica |

## Sugestões

| Método | Caminho | Descrição |
|---|---|---|
| GET | `/insights` | Sugestões atuais |
| POST | `/insights/run` | Corre os detetores agora |
| GET | `/insights/{sid}/evidence` | Dados que originaram a sugestão (gráfico do **Porquê?**) |

## Ferramentas dos agentes (só leitura)

| Método | Caminho |
|---|---|
| GET | `/tools/consumo_por_hora?divisao=&horas=` |
| GET | `/tools/tendencia_temperatura?divisao=&horas=&periodo_min=` |
| GET | `/tools/ultima_analise_camara` |

## Visão

| Método | Caminho | Descrição |
|---|---|---|
| GET | `/vision/live/status` | Estado do stream |
| GET | `/vision/live.jpg` | Último frame |
| GET | `/vision/live.mjpg` | Stream MJPEG |
| POST | `/vision/analyze` | Envia o frame atual ao modelo de visão |
| GET | `/vision/frames/{filename}` | Snapshot guardado em `var/frames/` |

## Simulador

| Método | Caminho | Corpo | Descrição |
|---|---|---|---|
| GET | `/sim` | | Divisões e dispositivos alteráveis, cenários, o que está a correr, registo |
| POST | `/sim/acao` | `{acao, divisao?, valor?}` | Uma alteração (abrir/fechar, temperatura, tomada, fumo, …) |
| POST | `/sim/cenario` | `{cenario, divisao?, passo_s?}` | Corre um cenário em background |
| POST | `/sim/parar` | | Para o cenário a correr |

## Modo de cenários (só com `--cenarios`)

| Método | Caminho | Corpo | Descrição |
|---|---|---|---|
| GET | `/cenarios` | | Estado do realizador |
| POST | `/cenarios/acao` | `{acao, cenario?}` | `acao` ∈ `cenario`, `continuar`, `abrir_porta`, `fechar_porta`, `camara`, `tomada`, `historico`, `repor` |
