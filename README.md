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
