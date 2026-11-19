# Casa em 3D: verificação (18/11/2026)

## Testes automáticos

`python -m pytest`: 92 testes. Os da vista 3D estão em `tests/test_twin_layout.py`, `tests/test_twin_state.py` e
`tests/test_coordinator.py`:

- `/twin/state` com o formato novo do `get_home` (`rooms[].devices`): 2 divisões e 7 dispositivos, valores normalizados.
- Dispositivo sem layout: continua no `/twin/state`, no centro da sua divisão (`placed: "default"`), com aviso.
- Divisão sem layout: é colocada ao lado das outras, com aviso. Se o layout e o MCP discordarem sobre a divisão, ganha o MCP.
- Divisões simuladas do `config/home.json` (Cozinha): ficam fora da vista 3D.
- Stream SSE: `snapshot`, depois `state` a cada poll e `event`/`decision` por item.
- Coordenador: dry-run não escreve; dispositivo desconhecido → 404; central/segurança → recusado; dispositivo offline → recusado.

## Ensaio em `SIMULATE=true`

O Chrome foi controlado pelo DevTools Protocol. Mediu-se o tempo desde o comando `scripts/simulate.py`
(incluindo o arranque do script) até a página mostrar a mudança.

| Injeção | `POLL_INTERVAL_S=2` (omissão) | `POLL_INTERVAL_S=1` |
|---|---|---|
| `open` (janela da Sala) | 2,64 s | 1,47 s |
| `temp 19.4` (etiqueta do Quarto) | 2,07 s | 1,02 s |
| `plug on` (1500 W) | 1,79 s | 1,02 s |
| `smoke` (Sala vermelha, "FUMO · EM ALARME") | 0,87 s | 1,25 s |
| `cancel` | — | 0,83 s |
| `offline light` | — | 0,87 s |

Todos abaixo de 3 s. Com o valor por omissão, o pior caso é cerca de 2,6 s, por isso **para a demo usar `POLL_INTERVAL_S=1`**.
Também foram verificados, com capturas de ecrã:

- a janela a rodar e a âmbar com a casa armada;
- o mapa de calor;
- a cor da lâmpada;
- o brilho da tomada;
- o fumo, com partículas e o contorno vermelho das paredes;
- o cone da câmara;
- o painel do dispositivo com o gráfico de 6 h;
- o zoom à divisão;
- os balões e a linha do tempo;
- o botão "Desligar", que gera uma decisão dry-run.

## Ensaio contra o MCP real (só leitura)

- Estado: Sala e Quarto com os 7 dispositivos, todos online e sem avisos de layout. A casa estava armada em casa (STAY)
  com o contacto da Sala aberto, e o 3D mostra a janela aberta a âmbar.
- Lâmpada: brilho 1 %, `color_rgb` reportado `3,0,2`. O 3D mostra `255, 0, 170` (magenta).
  **Por confirmar à vista com a lâmpada física.** Com brilho 1 % a cor reportada só tem 3 níveis por canal,
  por isso o tom é aproximado. Para o teste, subir o brilho na app (por exemplo 80 %).
