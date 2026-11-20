# Configuração

## `.env`

Copiar `.env.example` para `.env`. O `.env` está no `.gitignore` e **nunca** vai para o repositório.
`src/config.py` lê as variáveis do ambiente e do `.env`.

### Connected Home MCP

| Variável | Omissão | Descrição |
|---|---|---|
| `CONNECTED_HOME_MCP_URL` | URL do hackathon | Servidor MCP |
| `CONNECTED_HOME_API_KEY` | — | Chave da conta |
| `CONNECTED_HOME_CALLBACK_PORT` | `5434` | Porta do callback OAuth |

Autenticação: `python scripts/connected_home_auth.py login` (uma vez). Depois, `python scripts/mcp.py` lista e chama
qualquer ferramenta do MCP pela linha de comandos.

### LLM

Qualquer endpoint compatível com a API da OpenAI.

| Variável | Omissão | Descrição |
|---|---|---|
| `LLM_BASE_URL` | — | Ex.: `http://<servidor>:1234/v1` |
| `LLM_MODEL` | `qwen3.8:latest` | Modelo do chat, agentes e Coordenador |
| `LLM_API_KEY` | — | Chave, se o fornecedor exigir |
| `LLM_REASONING_EFFORT` | `high` | Raciocínio do Coordenador (`none`/`low`/`medium`/`high`; vazio = não enviar) |
| `CHAT_LLM` | `true` | `false` = chat só com regras PT-PT |
| `CHAT_REASONING_EFFORT` | `high` | Raciocínio na leitura das mensagens |
| `CHAT_TIMEOUT_S` | `600` | Tempo máximo de resposta do chat |

### Agentes A2A

| Variável | Omissão | Descrição |
|---|---|---|
| `AGENTS` | `true` | `false` = só perceção + emergência |
| `AGENTS_HOST` | `127.0.0.1` | |
| `AGENTS_BASE_PORT` | `9100` | Coordenador 9100, Segurança 9101, Energia 9102, Conforto 9103 |
| `AGENT_TIMEOUT_S` | `600` | |
| `AGENT_COOLDOWN_S` | `60` | Mesmo (evento, divisão) não é reenviado antes disto |
| `API_URL` | `http://127.0.0.1:8000` | Onde as ferramentas dos agentes leem a casa |

### Modos

| Variável | Omissão | Descrição |
|---|---|---|
| `DRY_RUN` | `true` | `false` = o Coordenador escreve mesmo na casa real |
| `SIMULATE` | `false` | `true` = tudo simulado (igual a `--sim`) |
| `SIM_ROOMS` | `false` | `true` = junta as divisões simuladas de `config/home.json` à casa real |

### Perceção e histórico

| Variável | Omissão | Descrição |
|---|---|---|
| `POLL_INTERVAL_S` | `7` (`2` em simulação) | Intervalo de leitura; `1` para reações bem abaixo de 3 s |
| `TICK_INTERVAL_S` | `90` | Evento periódico `tick` |
| `TEMP_DROP_C` / `TEMP_DROP_WINDOW_S` | `0.5` / `600` | Queda de temperatura que gera evento |
| `SMOKE_ALARM_TYPES` | `EMERGENCY,SMOKE,FIRE` | Tipos de alarme tratados como fumo |
| `SETPOINT_C` | `21` | Temperatura alvo global (sobreposta por divisão em `config/home.json`) |
| `STORE_INTERVAL_S` | `30` | Intervalo de gravação das leituras |
| `HISTORY_DB` | `var/home.db` | `var/home_sim.db` com `SIMULATE=true` |
| `HOME_CONFIG_FILE` | `config/home.json` | |
| `LAYOUT_FILE` | `config/layout.json` | |

### Sugestões

| Variável | Omissão | Descrição |
|---|---|---|
| `INSIGHT_MIN_DAYS` | `2` | Mesmo padrão em pelo menos N dias |
| `INSIGHT_MAX_PER_DAY` | `2` | |
| `INSIGHT_EVERY_S` | `1800` | Intervalo entre corridas dos detetores |

### Visão

| Variável | Omissão | Descrição |
|---|---|---|
| `VISION_ENABLED` | `true` | |
| `CAMERA_RTSP_URL` | — | Vazio = imagem simulada; `rtsp://…` ou `webcam:0` |
| `VISION_BASE_URL` / `VISION_MODEL` / `VISION_API_KEY` | herdam do `LLM_*` | |
| `VISION_REASONING_EFFORT` | `none` | A câmara não pensa (rápida) |
| `VISION_TIMEOUT_S` | `45` | Margem para cold-start do modelo |
| `VISION_KEEP_ALIVE` | `30m` | Mantém o modelo em VRAM (Ollama) |
| `VISION_WARMUP` | `true` | Pré-carrega o modelo no arranque |
| `VISION_COOLDOWN_S` / `VISION_STAY_COOLDOWN_S` | `10` / `30` | |
| `VISION_TRIGGER_DISARMED` | `false` | `true` só para testes |
| `VISION_MAX_FRAMES` | `50` | Snapshots retidos em `var/frames/` |

## Ficheiros de configuração (versionados)

### `config/home.json`

Parâmetros por divisão e divisões que só existem no simulador. Validado com Pydantic (`src/home_config.py`).

```json
{
  "rooms": {"Quarto": {"setpoint_c": 21.0}},
  "simulated_rooms": [
    {"name": "Cozinha", "type": "kitchen", "devices": ["temperature", "contact", "plug"],
     "setpoint_c": 19.0, "plug_power_change_w": 100}
  ]
}
```

Uma divisão simulada recebe o id `sim-<slug(nome)>`.

### `config/layout.json`

Onde fica cada divisão e dispositivo na casa 3D (metros; x à direita, y para cima, z para o observador). Gerar para
outra casa com `python scripts/layout_init.py --force` (ou `--from-dump` para usar `docs/capabilities/get_home.json`)
e ajustar à mão. `config/layout_cenarios.json` é a variante usada no modo de cenários.

## Ficheiros em runtime (`var/`, gitignored)

| Ficheiro | Conteúdo |
|---|---|
| `var/home.db` | Histórico, eventos, decisões, preferências (casa real) |
| `var/home_sim.db` | O mesmo com `SIMULATE=true` |
| `var/sim_home.json` | Estado da casa simulada (partilhado entre a app e `scripts/simulate.py`) |
| `var/layout_user.json` | Layout 3D ajustado pelo utilizador |
| `var/frames/` | Snapshots da câmara |
| `var/cenarios/` | Tudo o que o modo de cenários escreve |

## Scripts

| Script | Uso |
|---|---|
| `scripts/connected_home_auth.py` | OAuth (PKCE) com o Keycloak do hackathon: `login`, `headers`, `tools`, `logout` |
| `scripts/mcp.py` | Chamar qualquer ferramenta do MCP: `python scripts/mcp.py get_home` |
| `scripts/dump_capabilities.py` | Grava as respostas das ferramentas de leitura em `docs/capabilities/` (com segredos redigidos) |
| `scripts/simulate.py` | Injetar eventos na casa simulada (ver [cenarios-e-simulador.md](cenarios-e-simulador.md)) |
| `scripts/layout_init.py` | Gerar `config/layout.json` a partir do `get_home` |
| `scripts/seed_history.py` | Acrescentar N dias de histórico plausível antes do mais antigo (`--days`, `--dry-run`) |
| `scripts/run.ps1` | Arranque em Windows: liberta portas, `--sim --agents` (`-Reset`, `-Open`, `-DryRun`, `-NoAgents`) |
| `scripts/sim.ps1` | Atalho para `simulate.py` (`.\scripts\sim.ps1 janela`) |
| `scripts/stop.ps1` | Liberta as portas 8000 e 9100–9103 |
| `scripts/dev.ps1` | Carrega o `.env` e abre o VS Code com essas variáveis |
