# ATC_Hackathon-Tico-Tech
Repositório Oficial da Equipa 7 - Tico&amp;Tech, para o Aveiro City Hackathon 2026

## Como correr

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                       # testes
python -m src.perception [--sim]       # perceção: estado inicial por divisão + eventos
python -m src.api [--sim] [--port 8000]   # API + UI em http://127.0.0.1:8000
                                       # casa em 3D em http://127.0.0.1:8000/twin
python scripts/simulate.py rooms       # divisões do simulador
python scripts/simulate.py temp 18 --room cozinha
python -m src.storage.recorder         # só perceção + gravação do histórico (deixar a correr à noite)
python -m src.storage.recorder --status   # o que está gravado
```

Guião da demo: [docs/demo.md](docs/demo.md).

- Divisões: o MCP agrupa os dispositivos em `rooms[].devices` (Sala, Quarto). As divisões
  **simuladas** e os parâmetros por divisão (`setpoint_c`, limiares) estão em `config/home.json`.
- `SIMULATE=false` (omissão): só o MCP real. `SIM_ROOMS=true` junta-lhe as divisões simuladas do
  config (desligado por agora: só dados reais). `SIMULATE=true` / `--sim`: tudo simulado (testes);
  nesse modo nenhuma escrita sai para o MCP, e os testes nunca escrevem na casa real.
- Memórias em `var/home.db` (`var/home_sim.db` com `SIMULATE=true`), SQLite local:
  - **dados**: `readings` a cada `STORE_INTERVAL_S` (temperatura, humidade, `power_w`, energia de hoje e
    de ontem, `open`, `on`, bateria), `events` (todos os eventos) e `decisions` (propostas, decisão,
    explicação, autonomia e resposta do utilizador);
  - **preferências**: `preferences`, versionadas (alterar cria uma linha nova; revogar desativa).
  O `var/history.db` antigo é importado uma vez. Se o gravador e a API correrem ao mesmo tempo, só um grava.

### Chat com o Coordenador, autonomia e sugestões

- O utilizador fala só com o Coordenador (painel à direita em `/twin`, `POST /chat`). Cada mensagem é
  classificada com output estruturado (LLM + Pydantic; se o LLM falhar, regras em PT-PT): perguntas de
  estado (respondidas com dados reais), preferências, regras (cenário `TT_` no MCP via
  `guard()`/`execute()`), disposição do 3D, respostas a propostas e sugestões, comandos diretos.
  Tudo o que fica guardado ou é enviado é primeiro reformulado e confirmado.
- Vários pedidos numa só mensagem ("vou sair, arma a casa e liga o aquecedor"): o que é imediato executa
  logo; o que precisa de confirmação é perguntado um de cada vez. Cada mensagem é lida pelo LLM com
  raciocínio (`CHAT_REASONING_EFFORT`, omissão `high`), que devolve a lista de pedidos; as regras PT-PT
  só completam campos em falta e servem sozinhas quando não há LLM. "Quando a temperatura estiver boa" = a temperatura alvo.
- Autonomia por categoria (`energia`, `conforto`, `seguranca`, `luz`): `auto` executa (respeita o
  `DRY_RUN`), `perguntar` (omissão) deixa a decisão pendente no chat com Aprovar / Rejeitar / Alterar,
  `nunca` só regista. Ao fim de 3 aprovações seguidas (ou com "faz sempre assim"), a casa pergunta se
  passa a fazer aquilo sozinha. O caminho de emergência ignora todas as preferências.
- Sugestões proativas (`src/insights/`): os padrões são detetados em código sobre o histórico real
  (≥ `INSIGHT_MIN_DAYS` dias). O LLM só redige a frase e a frase é verificada contra a evidência. No
  máximo 2 por dia. **Porquê?** desenha o histórico que originou a sugestão.
- "O que a casa sabe de ti": ver, editar, revogar e apagar tudo (`/knowledge`).
- Janelas e portas da planta, pelo chat: "acrescenta uma janela na parede da direita do quarto",
  "a janela da sala tem 1,5 m de largura", "passa a janela do quarto para a parede da esquerda",
  "remove a janela do quarto". Sem sobreposições, cabem na parede, e o sensor da janela vai com ela.
- O sensor de contacto chama-se pelo que vigia no 3D: se estiver numa porta (pelo chat, "o sensor de
  contacto está junto à porta", ou arrastado para junto dela em **Ajustar 3D**), os eventos, os agentes, as
  respostas e os nomes das regras novas dizem "porta" (`TT_porta_aberta_...`) em vez de "janela".
- 3D: "o quarto fica à direita da sala" ou o modo **Ajustar 3D** (arrastar, gravar, desfazer). As
  alterações ficam em `var/layout_user.json`; o `config/layout.json` continua a ser a base versionada.

### Casa em 3D (`/twin`)

- Mostra só as divisões e os dispositivos do `get_home` (as divisões simuladas do `config/home.json`
  ficam de fora). O sítio de cada coisa vem de `config/layout.json`; para outra casa:
  `python scripts/layout_init.py --force` (lê o `get_home`, só leitura) e depois ajustar à mão.
- Tempo real por Server-Sent Events (`/twin/stream`), com recurso a polling de 2 s.
- A câmara aponta sempre para a divisão inteira (centro do chão, cone até ao canto mais afastado), também
  depois de mudada pelo chat ou à mão.
- Câmara em direto: com `CAMERA_RTSP_URL=rtsp://...` no `.env` (e `opencv-python`), a API mantém o stream
  aberto (RTSP por TCP) e o painel da câmara no 3D mostra-o (`/vision/live.mjpg`, `/vision/live.jpg`) com o
  botão **Analisar agora**, que envia o frame atual ao modelo de visão (`VISION_MODEL`, por omissão o
  `LLM_MODEL`, qwen3.8). Os eventos da câmara e alarmes com a casa armada usam o mesmo frame.
- Three.js r160 está copiado em `src/ui/static/vendor/three/` (funciona sem internet).
- Ensaio sem tocar na casa: `python -m src.api --sim` num terminal, `python scripts/simulate.py cenario janela` noutro.
  Para a reação ficar bem abaixo de 3 s, `POLL_INTERVAL_S=1`.

### Agentes LLM por A2A (Coordenador, Segurança, Energia, Conforto)

- Cada agente é um agente do **Microsoft Agent Framework** com o modelo do servidor da equipa
  (`LLM_BASE_URL`, `LLM_MODEL=qwen3.8:latest` no `.env`), servido pelo protocolo **A2A**
  (agent card + JSON-RPC): Coordenador 9100, Segurança 9101, Energia 9102, Conforto 9103.
- A API envia os eventos relevantes aos especialistas por A2A e recebe propostas em JSON. Cada
  proposta passa por regras fixas (dispositivo e ação permitidos, online, não é já o estado atual,
  nada se liga durante fumo, não se contraria uma ação recente de maior prioridade) e depois vai,
  por A2A, ao **agente Coordenador (LLM)**, que aprova ou recusa com um motivo. As aprovadas voltam a
  passar pelas regras fixas com o estado mais recente e só então são executadas (`execute()`).
  Sem resposta válida do Coordenador LLM, nada é executado.
- Prioridades: Emergência > Utilizador > Segurança > Energia > Conforto. Os botões Ligar/Desligar
  do 3D são pedidos do utilizador e executam-se logo (sem LLM), depois das regras fixas.
- `DRY_RUN=false` no `.env`: o Coordenador **atua na casa real** (luzes e tomadas, via
  `update_device_capabilities`). Com `DRY_RUN=true` só regista. Nunca arma nem desarma a segurança.
- Regras fixas, sem LLM: **fumo** → desliga as tomadas e põe a luz a vermelho; **intrusão** (alarme
  disparado com a casa em Home ou Away) → luz a vermelho no máximo, reposta como estava quando o
  alarme é cancelado.

```powershell
python -m src.api --agents                # casa real + agentes (num processo à parte) + 3D em /twin
# ou em dois terminais:
python -m src.agents                      # servidores A2A dos quatro agentes
python -m src.api                         # perceção + Coordenador + 3D em http://127.0.0.1:8000/twin
```

- O Coordenador (chat e agentes) usa sempre o qwen3.8 com raciocínio (`LLM_REASONING_EFFORT`, omissão
  `high`): mais lento, mas é a qualidade das respostas que conta. A câmara não pensa (`VISION_REASONING_EFFORT=none`). `AGENTS=false` desliga os agentes.
- No 3D, o cabeçalho mostra o modo (MCP real · atua / dry-run) e cada agente (à espera, a pensar…, tempo da última resposta, indisponível)
  e as propostas e decisões aparecem na linha do tempo e em balões sobre o dispositivo.
