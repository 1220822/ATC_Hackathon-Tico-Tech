# Simulador e modo de cenários

Duas formas de ensaiar sem tocar na casa real.

## Simulador

A casa simulada (`src/sim/home.py`) tem exatamente o mesmo JSON que o MCP (`get_home` + histórico de alarmes) e é
semeada a partir do dump real em `docs/capabilities/get_home.json`, por isso o caminho simulado passa pelo mesmo
normalizador. O estado vive em `var/sim_home.json`, partilhado entre a app e o CLI.

- `SIMULATE=true` (ou `--sim`): todas as divisões são simuladas; nenhuma escrita sai para o MCP.
- Com a casa real e `SIM_ROOMS=true`: só as divisões simuladas de `config/home.json` vêm daqui; o simulador nunca mexe
  no alarme real.

### Pela linha de comandos

```bash
python -m src.api --sim                     # num terminal
python scripts/simulate.py rooms            # noutro: divisões e dispositivos
python scripts/simulate.py open | close     # sensor de contacto
python scripts/simulate.py temp 19.5 --room quarto
python scripts/simulate.py humidity 70
python scripts/simulate.py smoke | cancel   # alarme de fumo / cancelar
python scripts/simulate.py arm stay|away|disarm
python scripts/simulate.py motion           # movimento na câmara
python scripts/simulate.py plug on --w 1500
python scripts/simulate.py offline light
python scripts/simulate.py reset            # volta ao estado inicial
```

`--room <id|nome>` escolhe a divisão; sem ele, cada comando usa a primeira divisão que tem esse tipo de dispositivo.

### Cenários de demo

Sequências temporizadas (`--step` segundos entre passos, omissão 4), definidas em `src/sim/scenarios.py`:

| Cenário | O que acontece |
|---|---|
| `janela` | Janela da Sala abre com o aquecedor do Quarto ligado; a temperatura do Quarto desce |
| `fumo` | Alarme de fumo |
| `objetivo` | Pré-condições para um objetivo em linguagem natural |
| `intrusao` | Casa armada + contacto aberto |

```bash
python scripts/simulate.py cenario janela
.\scripts\sim.ps1 intrusao                  # Windows
```

Para reações bem abaixo de 3 s: `POLL_INTERVAL_S=1`.

### Pelo browser: `/simulador`

`http://127.0.0.1:8000/simulador` (fora da navegação) tem botões para cada sensor simulado (abrir/fechar,
temperatura, tomada, câmara, fumo, offline), segurança e alarme, e os quatro cenários, com registo de cada passo.
Serve para uma pessoa (ou um agente no browser) conduzir a demo num separador enquanto a app é gravada noutro.
API: `GET /sim`, `POST /sim/acao`, `POST /sim/cenario`, `POST /sim/parar` (ver [api.md](api.md#simulador)).

## Modo de cenários (vídeos do pitch)

```bash
python -m src.api --cenarios                # http://127.0.0.1:8000/twin   (tecla D: painel do realizador)
python -m src.api --cenarios --port 8010
```

Reproduz os vídeos do pitch (cenários 1–6) sempre com os mesmos textos, a mesma ordem e as mesmas horas
(19/11/2026), **sem casa real, sem MCP real e sem LLM**. Corre a aplicação real — perceção, agentes por A2A,
Coordenador, regras fixas e UI. Só mudam três coisas:

1. A casa é um **mock do MCP** (`src/cenarios/mock_mcp.py`) com a mesma interface, feito a partir de
   `docs/capabilities/get_home.json`.
2. Os textos do chat, dos agentes e do Coordenador vêm de `src/cenarios/fixtures.json`.
3. O **relógio é simulado** (`src/cenarios/clock.py`) e salta para a hora da gravação quando aparece um texto marcado.

O caminho de emergência (intrusão) é o código real: o realizador só injeta a porta e a câmara.

### Painel do realizador

Canto inferior esquerdo de `/twin`, ou atalhos de teclado:

| Tecla | Ação |
|---|---|
| **1–6** | Começa o cenário a partir do seu estado inicial |
| **Shift+1–6** | Continua a partir do anterior, sem repor |
| **A** / **F** | Abre / fecha a porta da Sala |
| **C** | Câmara com 2 pessoas (98 %) |
| **T** | Liga a tomada do Quarto (7 W) |
| **H** | Carrega o histórico de humidade (3 noites) |
| **R** | Repõe tudo |
| **D** | Esconde o painel, antes de gravar |

### Isolamento

Tudo fica em `var/cenarios/` (`sim.db`, a casa do mock, os cenários `TT_` e o 3D ajustado). **Repor tudo** só limpa
essa pasta e nunca toca no `var/home.db`. A disposição do 3D é a de `config/layout_cenarios.json`, com o sensor de
contacto na porta da Sala. Sem `--cenarios`, a aplicação comporta-se exatamente como antes.
