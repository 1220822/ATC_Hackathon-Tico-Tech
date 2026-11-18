# ATC_Hackathon-Tico-Tech
Repositório Oficial da Equipa 7 - Tico&amp;Tech, para o Aveiro City Hackathon 2026

## Como correr

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest                       # testes
python -m src.perception [--sim]       # perceção: estado inicial por divisão + eventos
python -m src.api [--sim] [--port 8000]   # API + UI em http://127.0.0.1:8000
python scripts/simulate.py rooms       # divisões do simulador
python scripts/simulate.py temp 18 --room cozinha
```

- Divisões: o MCP agrupa os dispositivos em `rooms[].devices` (Sala, Quarto). As divisões
  **simuladas** e os parâmetros por divisão (`setpoint_c`, limiares) estão em `config/home.json`.
- `SIMULATE=false`: MCP real + divisões simuladas do config. `SIMULATE=true`: tudo simulado.
- Histórico: SQLite em `var/history.db` (`var/history_sim.db` com `SIMULATE=true`), gravado a
  cada `STORE_INTERVAL_S` segundos.
