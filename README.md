# ATC_Hackathon-Tico-Tech
Repositório Oficial da Equipa 7 - Tico&amp;Tech, para o Aveiro City Hackathon 2026

## Como correr

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                       # testes
python -m src.perception [--sim]       # perceção: estado inicial por divisão + eventos
python -m src.api [--sim] [--port 8000]   # API + UI em http://127.0.0.1:8000
                                       # gémeo digital 3D em http://127.0.0.1:8000/twin
python scripts/simulate.py rooms       # divisões do simulador
python scripts/simulate.py temp 18 --room cozinha
```

- Divisões: o MCP agrupa os dispositivos em `rooms[].devices` (Sala, Quarto). As divisões
  **simuladas** e os parâmetros por divisão (`setpoint_c`, limiares) estão em `config/home.json`.
- `SIMULATE=false`: MCP real + divisões simuladas do config. `SIMULATE=true`: tudo simulado.
- Histórico: SQLite em `var/history.db` (`var/history_sim.db` com `SIMULATE=true`), gravado a
  cada `STORE_INTERVAL_S` segundos.

### Gémeo digital 3D (`/twin`)

- Mostra só as divisões e os dispositivos do `get_home` (as divisões simuladas do `config/home.json`
  ficam de fora). O sítio de cada coisa vem de `config/layout.json`; para outra casa:
  `python scripts/layout_init.py --force` (lê o `get_home`, só leitura) e depois ajustar à mão.
- Tempo real por Server-Sent Events (`/twin/stream`), com recurso a polling de 2 s.
- Three.js r160 está copiado em `src/ui/static/vendor/three/` (funciona sem internet).
- Ensaio: `python -m src.api --sim` num terminal, `python scripts/simulate.py cenario janela` noutro.
  Para a reação ficar bem abaixo de 3 s, `POLL_INTERVAL_S=1`.

### Agentes LLM por A2A (Segurança, Energia, Conforto)

- Cada agente é um agente do **Microsoft Agent Framework** com o modelo do servidor da equipa
  (`LLM_BASE_URL`, `LLM_MODEL=qwen3.8:latest` no `.env`), servido pelo protocolo **A2A**
  (agent card + JSON-RPC) nas portas 9101–9103.
- O Coordenador (no processo da API) envia-lhes os eventos relevantes por A2A, recebe propostas em
  JSON, arbitra (Emergência > Utilizador > Segurança > Energia > Conforto) e só ele escreve, via
  `execute()`, em **dry-run** por omissão. As ferramentas dos agentes são só de leitura.
- O fumo segue um caminho de emergência determinístico, sem LLM.

```powershell
python -m src.api --sim --agents          # tudo num comando (os agentes correm num processo à parte)
# ou em dois terminais:
python -m src.agents                      # servidores A2A dos agentes
python -m src.api --sim                   # perceção + Coordenador + 3D em http://127.0.0.1:8000/twin
```

- `LLM_REASONING_EFFORT=none` (omissão) desliga o raciocínio do modelo: ~1 s por resposta com o
  qwen3.8 em vez de 8–10 s, com as mesmas propostas. `AGENTS=false` desliga os agentes.
- No 3D, o cabeçalho mostra cada agente (à espera, a pensar…, tempo da última resposta, indisponível)
  e as propostas e decisões aparecem na linha do tempo e em balões sobre o dispositivo.
