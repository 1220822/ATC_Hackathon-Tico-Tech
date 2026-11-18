# Plano de Implementação — Módulo de Visão Computacional Local

> **Destinatário:** Agente LLM integrado em IDE (Claude Code, Cursor, Antigravity, Copilot Agent, etc.)  
> **Documento de arquitetura (fonte de verdade):** [docs/vision_architecture.md](vision_architecture.md)  
> **Branch alvo:** `feat/camera-vision` (a criar a partir de `main` **apenas** quando o utilizador autorizar)  
> **Base verificada:** `main` em `dc24481`  

---

## 0. Instruções de Execução para o Agente LLM

Antes de escrever qualquer linha de código, lê integralmente [docs/vision_architecture.md](vision_architecture.md) e os ficheiros referenciados em cada fase. Executa **uma fase de cada vez**, validando os critérios de saída (`pytest`) antes de avançar para a fase seguinte.

### 0.1 Contexto Essencial do Sistema (Resumo Operacional)

- **Stack do repositório:** Python 3.11 (`.venv\Scripts\python.exe`), Pydantic v2, FastAPI, Uvicorn, Microsoft Agent Framework + A2A (`src/agents/`), Gémeo Digital 3D em Three.js com SSE (`src/twin/`).
- **Topologia real da casa ("Hack 7", [docs/capabilities/get_home_rooms.json](capabilities/get_home_rooms.json) & [config/home.json](../config/home.json)):**
  - `Sala` (MCP real): `Camera`, `Security hub`, `Contact sensor`, `Smoke sensor` *(única divisão com câmara)*.
  - `Quarto` (MCP real): `Light`, `Plug`, `Temperature sensor`.
  - `Cozinha` (simulada): `temperature`, `contact`, `plug` *(sem câmara)*.
- **Servidor interno de modelos — Ollama (confirmado pela equipa; validado ao vivo em [docs/vision_architecture.md §2.3](vision_architecture.md#L46-L57)):**
  - **Runtime:** **Ollama** (não LM Studio), expondo API OpenAI-compatible em `/v1/chat/completions` e endpoints nativos `/api/ps` e `/api/show`.
  - **URLs:** servidor da equipa em `http://192.168.2.161:1234/v1` (porta `1234`); se correr **Ollama localmente**, a porta por omissão é **`11434`** (`http://localhost:11434/v1`, configurável via `LLM_BASE_URL` / `VISION_BASE_URL` no `.env`).
  - **Modelo residente em VRAM:** **`qwen3.8:latest`** (`27.3B`, `Q4_K_M`, `27.36 GB` em VRAM) com capabilities `['completion', 'vision', 'tools', 'thinking']`. Reutilizar o mesmo modelo dos agentes evita *model swapping* e *cold-start* (`OLLAMA_KEEP_ALIVE` padrão = 5 min de inatividade).
  - **Parâmetros Ollama validados:** `reasoning_effort="none"` (HTTP 200 validado em ensaio visual; latência ~3–8 s por frame) e suporte nativo a `response_format={"type": "json_object"}` em `/v1/chat/completions`. `Authorization: Bearer ...` é aceite/ignorado pelo Ollama.
- **Câmara física:** o MCP da Altice não fornece vídeo/frames. A captura ocorre fora do MCP (`CAMERA_RTSP_URL`). Atualmente a câmara física ainda não está na rede, pelo que o desenvolvimento e os testes usam `FileFrameSource` (com `RtspFrameSource` e `WebcamFrameSource` prontos atrás do mesmo `Protocol`).
- **Padrão de integração escolhido ([docs/vision_architecture.md §3.2](vision_architecture.md#L68-L132)):**
  - **Opção 1 (Enriquecimento Push + Consulta Pull):** `VisionWorker` escuta triggers no `EventBus` numa thread dedicada, captura e analisa o frame via `qwen3.8:latest`, guarda `VisionObservation` em `SharedMemory` + JPEG em `var/frames/`, regista o evento sintético `camera_analysis` em `SharedMemory` e publica-o no `EventBus`.
  - **Separação de responsabilidades:** Visão = factos estruturados objetivos (`VisionObservation`); Agente de Segurança = `resumo` final em PT-PT + `propostas` (`AgentReply`); `Coordinator` = único ponto de escrita.

### 0.2 Invariantes Obrigatórias (NUNCA violar)

1. **Zero escritas no MCP** a partir de `src/vision/` — apenas perceção enriquecida e leitura.
2. **Emergência determinística intocável:** `smoke_detected` → `Coordinator.emergency()` ([src/coordinator.py](../src/coordinator.py#L176-L230)) nunca pode ser atrasado, bloqueado ou cancelado pela visão.
3. **Não bloquear o `EventBus`:** `EventBus.publish()` ([src/memory.py](../src/memory.py#L28-L37)) é síncrono na thread de polling. `VisionWorker.on_event()` deve apenas validar/enfileirar e regressar imediatamente (<1 ms).
4. **Ordem obrigatória de publicação de `camera_analysis`:** `memory.set_vision(obs)` → `memory.add_events([event])` → `bus.publish([event])` (sem `add_events`, o stream SSE `/twin/stream` não recebe o evento).
5. **Compatibilidade total com testes existentes:** novos parâmetros em `AgentBridge.__init__` e `create_app` devem ser opcionais (`vision_worker=None`) para não partir nenhum teste existente em `tests/`.
6. **Segurança e higiene:** nunca fazer commit de segredos (`.env`, credenciais RTSP), validar nomes de ficheiro contra *path traversal* em `GET /vision/frames/{filename}`, manter importação de `cv2` **lazy** (testes offline não podem depender de OpenCV), e redigir mensagens user-facing em **PT-PT** sem emojis nem markdown.

### 0.3 Comando de Verificação Padrão

Executar no final de **cada fase**:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

---

## Fase 0 — Pré-voo e Preparação da Branch

> **Atenção:** só criar a branch quando o utilizador confirmar explicitamente.

1. Verificar estado limpo do repositório e baseline dos testes:
   ```powershell
   git status
   .venv\Scripts\python.exe -m pytest -q
   ```
2. Criar e mudar para a branch (quando autorizado):
   ```powershell
   git checkout -b feat/camera-vision
   ```
3. Confirmar que `tests/` passa 100% antes de alterar qualquer ficheiro.

---

## Fase 1 — Fundação: Configuração, Modelos de Dados, `EventType` e `SharedMemory`

**Referência arquitetural:** [docs/vision_architecture.md §5.1](vision_architecture.md#L150-L195), [§5.3](vision_architecture.md#L213-L224), [§7.2](vision_architecture.md#L261-L273), [§7.3](vision_architecture.md#L275-L290).

### Tarefas

1. **Atualizar [src/config.py](../src/config.py) e [.env.example](../.env.example):**
   - Adicionar os campos seguintes a `Settings` e resolver a cadeia de fallback diretamente dentro de `get_settings()` (mantendo o padrão `@lru_cache`, sem `@property` nem `@computed_field`):
     - `vision_enabled: bool` (`VISION_ENABLED`, default `True`)
     - `camera_rtsp_url: str` (`CAMERA_RTSP_URL`, default `""`)
     - `vision_base_url: str` (`os.environ.get("VISION_BASE_URL") or llm_base_url`)
     - `vision_model: str` (`os.environ.get("VISION_MODEL") or llm_model`)
     - `vision_api_key: str` (`os.environ.get("VISION_API_KEY") or llm_api_key`)
     - `vision_timeout_s: float` (`VISION_TIMEOUT_S`, default `25.0`)
     - `vision_cooldown_s: float` (`VISION_COOLDOWN_S`, default `10.0`)
     - `vision_stay_cooldown_s: float` (`VISION_STAY_COOLDOWN_S`, default `30.0`)
     - `vision_trigger_disarmed: bool` (`VISION_TRIGGER_DISARMED`, default `False`)
     - `vision_sim_image: Path | None` (`VISION_SIM_IMAGE`, default `None`)
     - `vision_max_frames: int` (`VISION_MAX_FRAMES`, default `50`)
     - `vision_frames_dir: Path` (`VISION_FRAMES_DIR`, default `VAR_DIR / "frames"`)
   - Atualizar `.env.example` com o bloco `# --- Visão / Câmara ---` exatamente conforme [docs/vision_architecture.md §7.3](vision_architecture.md#L275-L291).

2. **Registar evento sintético em [src/perception/state.py](../src/perception/state.py#L46-L55):**
   - Adicionar `"camera_analysis"` ao `Literal` `EventType` com comentário explícito indicando que é um **evento sintético** publicado pelo `VisionWorker` (não produzido por `diff()`).

3. **Criar `src/vision/__init__.py` e `src/vision/models.py`:**
   - Implementar `VisionObservation(BaseModel)` com `model_config = ConfigDict(frozen=True)` e todos os campos exatos de [docs/vision_architecture.md §5.1](vision_architecture.md#L150-L195):
     - Identificação/contexto: `id` (default `uuid.uuid4().hex[:10]`), `ts` (default `time.time`), `room_id`, `room_name`, `camera_id`, `trigger_type`, `trigger_event_id`, `context: Literal["intrusion", "smoke", "general"]`, `status: Literal["ok", "capture_error", "inference_error", "timeout"] = "ok"`.
     - Factos visuais: `has_person: bool = False`, `person_count: int = 0`, `category: Literal["person", "pet", "smoke_or_fire", "empty", "other", "unclear"] = "unclear"`, `window_or_door_open: bool | None = None`, `smoke_or_fire_visible: bool = False`, `objects: tuple[str, ...] = ()`, `overall_confidence: float = Field(default=0.0, ge=0.0, le=1.0)`, `scene_note_pt: str = ""`.
     - Operacionais: `frame_path: str | None = None`, `error: str | None = None`, `model: str = ""`, `latency_ms: int = 0`.
   - Adicionar helpers puros em `src/vision/models.py`:
     - `vision_severity(obs: VisionObservation, armed: bool) -> Severity` seguindo exatamente [docs/vision_architecture.md §5.3](vision_architecture.md#L213-L224).
     - `format_vision_message(obs: VisionObservation) -> str` gerando frase determinística curta em PT-PT para `Event.message` (incluindo divisão, pessoas/fumo/janela aberta ou estado de erro).
     - `to_event(obs: VisionObservation, armed: bool) -> Event` construindo `Event(type="camera_analysis", severity=..., device_id=obs.camera_id, role="camera", room=obs.room_id, room_name=obs.room_name or None, data=obs.model_dump(), message=...)`.

4. **Estender [src/memory.py](../src/memory.py):**
   - Adicionar armazenamento thread-safe (sob `self._lock`) para a última observação visual por divisão (`_vision_by_room: dict[str, Any]`) e a última global (`_latest_vision: Any = None`).
   - Implementar `set_vision(self, obs) -> None` e `latest_vision(self, room: str | None = None)` (lookup por `room_id` ou `room_name` case-insensitive se `room` for fornecido; caso contrário devolve a última observação global).
   - Evitar import circular em `memory.py` (usar `TYPE_CHECKING` ou tipagem leve).

### Critério de Saída (Fase 1)

- `.venv\Scripts\python.exe -m pytest -q` passa sem erros.
- `VisionObservation` é imutável (`ValidationError` / `TypeError` ao tentar mutar atributos), serializa com `model_dump()`, e `to_event()` valida em `Event` com `type="camera_analysis"`.

---

## Fase 2 — Captura de Frames e Armazenamento (`src/vision/capture.py`)

**Referência arquitetural:** [docs/vision_architecture.md §6](vision_architecture.md#L228-L250).

### Tarefas

1. **Criar `src/vision/capture.py`:**
   - Definir `class CaptureError(RuntimeError): pass` e `class FrameSource(Protocol)` com `name: str` e `def capture(self, room_id: str | None = None) -> bytes: ...`.
   - Implementar **`FileFrameSource`**:
     - Aceita caminho de imagem explícito (`image_path: Path | None`) ou fallback para uma imagem de fixture padrão / diretório de fixtures (`tests/fixtures/vision/`).
     - Se o ficheiro configurado não existir ou estiver ilegível, levanta `CaptureError`.
     - Retorna os bytes da imagem (JPEG/PNG).
   - Implementar **`RtspFrameSource`**:
     - Recebe `url: str`, `timeout_s: float = 5.0`, `max_width: int = 1024`, `jpeg_quality: int = 80`.
     - Importação **lazy** de `cv2` (levanta `CaptureError("opencv não instalado")` se indisponível — nunca falha no import do módulo!).
     - Aplica *flush* de frames antigos (descartar alguns frames iniciais ou ler o frame mais recente) com timeout controlado e redimensionamento proporcional se `width > max_width`, codificando para JPEG.
     - Nunca regista credenciais RTSP nos logs (redigir `rtsp://user:pass@` antes de logar erros).
   - Implementar **`WebcamFrameSource`**:
     - Recebe `index: int = 0` (ativado quando `CAMERA_RTSP_URL` começa por `webcam:`, ex. `webcam:0`).
     - Importação **lazy** de `cv2`; em Windows (`os.name == "nt"`), tenta `cv2.CAP_DSHOW` com fallback para backend padrão; liberta sempre o `VideoCapture` no bloco `finally`.
     - **Gestão de dependência OpenCV:** não adicionar `opencv-python` como dependência obrigatória em `requirements.txt` (os testes offline e `FileFrameSource` não dependem de OpenCV); documentar em comentário em `requirements.txt` (ou num `requirements-vision.txt` opcional) que RTSP/webcam em Windows/macOS deve usar **`opencv-python`** (não `opencv-python-headless`) ou `ffmpeg`.
   - Implementar **`FrameStore`**:
     - Guarda bytes JPEG em `frames_dir` (`var/frames/<YYYYMMDD_HHMMSS>_<room_slug>_<obs_id>.jpg`), criando a pasta se não existir.
     - Devolve o nome/caminho relativo seguro (ex.: `"20261118_143000_sala_a1b2c3d4e5.jpg"`).
     - Aplica retenção automática após cada escrita, apagando os ficheiros `.jpg` mais antigos além de `max_frames` (`VISION_MAX_FRAMES`).
     - Expor helper `resolve_frame_path(frames_dir: Path, filename: str) -> Path | None` usando validação por prefixo canónico (`.resolve()`) — mais robusta que filtragem de caracteres:
       ```python
       def resolve_frame_path(frames_dir: Path, filename: str) -> Path | None:
           if not filename or Path(filename).name != filename:
               return None
           base = frames_dir.resolve()
           candidate = (base / filename).resolve()
           if candidate.parent != base or not candidate.is_file():
               return None
           return candidate
       ```
   - Implementar factory **`make_frame_source(settings: Settings) -> FrameSource`**:
     - `settings.simulate` ou `not settings.camera_rtsp_url` → `FileFrameSource`.
     - `settings.camera_rtsp_url.lower().startswith("webcam:")` → `WebcamFrameSource`.
     - Caso contrário → `RtspFrameSource`.

2. **Gerador programático de imagem de teste em `tests/test_vision.py` (sem binários no git):**
   - Criar um helper leve em `tests/test_vision.py` (ex.: `make_solid_png(w, h, rgb)` com `struct` + `zlib`, ou constante JPEG 1×1 mínima) para gerar ficheiros de imagem em `tempfile.TemporaryDirectory()` sem poluir o repositório com blobs binários.

3. **Adicionar testes unitários de captura em `tests/test_vision.py`:**
   - `FileFrameSource` lê bytes válidos; ficheiro inexistente levanta `CaptureError`.
   - `FrameStore` guarda ficheiro, aplica retenção `max_frames`, e `resolve_frame_path` bloqueia `../secret.txt` e caminhos absolutos.
   - `make_frame_source` seleciona a implementação certa consoante `simulate` e `camera_rtsp_url` (incluindo `webcam:0`).

### Critério de Saída (Fase 2)

- `.venv\Scripts\python.exe -m pytest -q` passa 100% **sem** OpenCV instalado nem câmara física ligada.

---

## Fase 3 — Analisador Multimodal (`src/vision/analyzer.py`)

**Referência arquitetural:** [docs/vision_architecture.md §2.3](vision_architecture.md#L46-L57), [§3.1](vision_architecture.md#L62-L66), [§5.1–§5.2](vision_architecture.md#L150-L212).

### Tarefas

1. **Criar `src/vision/analyzer.py`:**
   - Definir `class VisionAnalyzer(Protocol)`:
     ```python
     class VisionAnalyzer(Protocol):
         model_name: str
         def analyze(
             self,
             image_bytes: bytes,
             *,
             context: Literal["intrusion", "smoke", "general"],
             room_name: str = "",
             security_status: str = "DISARMED",
             trigger_type: str = "manual",
         ) -> dict[str, Any]: ...
     ```
     *(ou devolver diretamente os campos validados num `VisionAnalysisPayload(BaseModel)` que o `VisionWorker` combina com os metadados de evento/frame para criar `VisionObservation` — abordagem muito limpa!)*
   - Implementar prompts em PT-PT especializados por `context`:
     - **`intrusion`:** instrui o VLM a identificar e contar pessoas (`has_person`, `person_count`), distinguir falsos alarmes (animal, sombras/cortina, divisão vazia), **verificar visualmente se a janela/porta está aberta, fechada ou não visível** (`window_or_door_open: true | false | null`), listar objetos relevantes (`objects`), dar `overall_confidence` (`0.0–1.0`) e `scene_note_pt` (1 frase factual curta PT-PT, sem recomendações nem decisões).
     - **`smoke`:** instrui o VLM a verificar fumo/neblina/chamas/clarão (`smoke_or_fire_visible: true | false`), contar pessoas em risco na divisão (`has_person`, `person_count`), listar objetos/indícios, `overall_confidence` e `scene_note_pt`.
     - **`general`:** análise geral factual da divisão (pessoas, estado da janela/porta, anomalias/fumo, `scene_note_pt`).
   - **Extrair helper partilhado de parsing JSON (`src/json_utils.py`):**
     - Mover `_json_candidates` de [src/agents/specs.py](../src/agents/specs.py#L92-L110) para `src/json_utils.py` (`json_candidates(text: str)`), reexportando `_json_candidates = json_candidates` em `src/agents/specs.py` (retrocompatível). Assim `src/vision/analyzer.py` **não** depende de `src/agents/`.
   - Implementar **`OpenAIVisionAnalyzer`**:
     - Mantém uma instância persistente de `httpx.Client` (`self._client = httpx.Client(timeout=timeout_s, transport=transport)`), protegida por `self._lock = threading.Lock()` (para garantir *thread-safety* total entre a worker thread e `analyze_now()`), com método `close()` chamado no encerramento do `VisionWorker`.
     - Deteta MIME type pelos *magic bytes* (`image/png` se `\x89PNG`, caso contrário `image/jpeg`), codifica em `data:<mime>;base64,...`.
     - Envia `POST {base_url.rstrip('/')}/chat/completions` (compatível com o endpoint `/v1` do **Ollama**) com:
       - `model`: `settings.vision_model`
       - `temperature`: `0.0`
       - `max_tokens`: `500`
       - `response_format`: `{"type": "json_object"}` *(suportado nativamente pelo Ollama para forçar JSON sintaticamente válido; se algum servidor devolver HTTP 400 por não suportar `response_format`, repetir 1x sem este campo)*
       - `reasoning_effort`: `settings.llm_reasoning_effort` (se não vazio; omissão `"none"`)
       - Header `Authorization: Bearer {api_key}` *(ignorado pelo Ollama, mantido por compatibilidade OpenAI)*
       - Timeout: `settings.vision_timeout_s` *(margem de 25 s para cobrir eventual cold-start do Ollama se o modelo tiver expirado após 5 min de inatividade)*
     - Faz parse tolerante da resposta usando `json_candidates` de `src/json_utils.py` (rede de segurança contra `<think>...</think>` ou code fences caso outro modelo Ollama ignore `reasoning_effort`/`response_format`).
     - Distingue claramente `httpx.TimeoutException` (mapeado para `status="timeout"`) de erros HTTP/rede/JSON inválido (mapeado para `status="inference_error"`).
   - Implementar **`MockVisionAnalyzer`**:
     - Aceita um resultado pré-configurado ou callable `(context, trigger_type) -> payload`, para testes determinísticos e modo offline sem servidor Ollama.
   - Implementar factory **`make_analyzer(settings: Settings) -> VisionAnalyzer`**.

2. **Expandir `tests/test_vision.py` (Fase 3):**
   - Parsing de JSON limpo, JSON envolvido em ```` ```json ... ``` ```` e JSON precedido de `<think>raciocínio</think>`.
   - Payload HTTP enviado por `OpenAIVisionAnalyzer` (usando `httpx.MockTransport` — zero rede!): verifica `model`, `max_tokens=500`, `temperature=0.0`, `response_format={"type": "json_object"}`, `reasoning_effort="none"`, presença de `image_url` base64 e prompt correto para `intrusion` (menciona janela/porta) vs. `smoke` (menciona fumo/chamas).
   - Fallback de `response_format`: se a 1.ª resposta for HTTP 400 mencionando `response_format`, repete 1x sem esse campo e conclui com sucesso.
   - Tratamento de timeout (`httpx.ReadTimeout`) → `TimeoutError` / `status="timeout"`, HTTP 500 e JSON malformado → erro estruturado sem exceção não tratada.

### Critério de Saída (Fase 3)

- `.venv\Scripts\python.exe -m pytest -q` passa 100% offline usando `httpx.MockTransport`.

---

## Fase 4 — Orquestrador Assíncrono `VisionWorker` (`src/vision/worker.py`)

**Referência arquitetural:** [docs/vision_architecture.md §3.2](vision_architecture.md#L68-L132), [§4.1](vision_architecture.md#L136-L150), [§4.2](vision_architecture.md#L152-L165).

### Tarefas

1. **Criar `src/vision/worker.py`:**
   - Definir `VISION_TRIGGER_EVENTS = ("camera_motion", "camera_sound", "security_alarm", "tamper_alarm", "contact_opened", "smoke_detected")`.
   - Implementar função pura **`should_trigger_vision(event: Event, state: HomeState | None, settings: Settings) -> tuple[bool, Literal["intrusion", "smoke", "general"], DeviceState | None]`**:
     - Se `not settings.vision_enabled` ou `state is None`: `(False, "general", None)`.
     - **Filtro *room-aware*:** encontrar a câmara alvo:
       - Se `event.room`: `cam = state.by_role("camera", event.room)`. Se não houver câmara nessa divisão → `(False, "general", None)`.
       - Se `event.room is None` (evento global sem divisão, ex. `smoke_detected` / `security_alarm` sem dispositivo): `cam = state.by_role("camera")`. Se não existir nenhuma câmara na casa → `(False, "general", None)`.
     - **Matriz de disparo ([docs/vision_architecture.md §4.1](vision_architecture.md#L136-L150)):**
       - `smoke_detected`: `True`, `"smoke"` em qualquer modo (`AWAY`/`STAY`/`DISARMED`).
       - `security_alarm`: `True`, `"intrusion"` em qualquer modo (cobre `kind="intrusion"` e `kind="other"`; o cooldown/single-flight já deduplica qualquer sobreposição).
       - `tamper_alarm`: `True`, `"intrusion"` em qualquer modo.
       - `contact_opened`: `True`, `"intrusion"` apenas se `state.security_status in ("AWAY", "STAY")`.
       - `camera_motion`: `True`, `"intrusion"` se `state.security_status in ("AWAY", "STAY")`, ou em `"DISARMED"` **apenas** se `settings.vision_trigger_disarmed` for `True`.
       - `camera_sound`: `True`, `"intrusion"` apenas se `state.security_status == "AWAY"`.
       - Qualquer outro evento: `False`.
   - Implementar classe **`VisionWorker`**:
     - Construtor: `(memory: SharedMemory, bus: EventBus, settings: Settings, source: FrameSource | None = None, analyzer: VisionAnalyzer | None = None, store: FrameStore | None = None)`.
     - **Serialização thread-safe (`self._io_lock = threading.Lock()`):** protege o acesso conjunto a `self._source.capture(...)` e `self._analyzer.analyze(...)` entre a worker thread (`_process_job`) e chamadas síncronas da FastAPI (`analyze_now`), evitando *race conditions* tanto no `httpx.Client` como em `cv2.VideoCapture`.
     - Métodos de ciclo de vida: `start() -> VisionWorker`, `stop(timeout_s: float = 5.0) -> None` (sinaliza paragem, faz `join(timeout_s)` e fecha `analyzer.close()` se existir), `is_alive() -> bool`.
     - Método público **`will_handle(self, event: Event, state: HomeState | None) -> bool`**: delega em `should_trigger_vision(...)` (usado pelo `AgentBridge` para decidir se difere `camera_motion`/`camera_sound`).
     - Método público **`on_event(self, event: Event) -> None`** (callback do `EventBus` — **deve ser O(1) e não-bloqueante**):
       - Avalia `should_trigger_vision`.
       - Aplica proteção **single-flight + cooldown por `room_key`**:
         - Cooldown: `settings.vision_stay_cooldown_s` (30 s) para `camera_motion` em `STAY`; `settings.vision_cooldown_s` (10 s) nos restantes casos.
         - **Upgrade de prioridade (enriquecimento informativo):** `smoke_detected` fura o cooldown de uma análise anterior de intrusão/movimento; se já houver uma análise de prioridade inferior em voo, guarda o pedido `smoke_detected` num slot único `_pending_priority` para correr mal a atual termine. *(Documentar no código que isto serve apenas para enriquecimento visual — a resposta crítica de emergência corre sempre imediatamente via `Coordinator.emergency()`.)*
       - Coloca o job na fila/condição da worker thread e regressa imediatamente.
     - Worker loop (thread daemon `"vision-worker"`):
       - Protegido por `try / except Exception` em cada job (um erro nunca mata a thread).
       - Fluxo de processamento de um job (`_process_job`):
         1. Captura frame (`source.capture(room_id)`) → se falhar, cria `VisionObservation(status="capture_error", error=...)`.
         2. Se capturou: guarda em `store.save(...)` → chama `analyzer.analyze(...)` medindo `latency_ms` → se timeout cria `status="timeout"`, se erro cria `status="inference_error"`, se sucesso cria `status="ok"`.
         3. Executa **sempre** pela ordem exata:
            ```python
            self.memory.set_vision(obs)
            ev = to_event(obs, armed=bool(state and state.armed))
            self.memory.add_events([ev])
            self.bus.publish([ev])
            ```
     - Método síncrono **`analyze_now(self, room: str | None = None, context: Literal["intrusion", "smoke", "general"] = "general") -> VisionObservation`**:
       - Para o endpoint `POST /vision/analyze` (ignora cooldown de eventos e modo `DISARMED`; captura, analisa, grava em `SharedMemory`, publica `camera_analysis` e devolve a `VisionObservation`).

2. **Expandir `tests/test_vision.py` (Fase 4):**
   - Testar todas as combinações da matriz (`AWAY`, `STAY`, `DISARMED` com `vision_trigger_disarmed=False` e `True`) para os 6 triggers + eventos ignorados (`temperature_changed`, `contact_closed`, `tick`, `security_mode_changed`).
   - Testar filtro *room-aware*: `contact_opened` na `Cozinha` (armado) NÃO dispara; `contact_opened` na `Sala` (armado) DISPARA.
   - Testar rajada do `cenario intrusao` (`contact_opened` + `security_alarm` + `camera_motion` em 2 s) → exatamente **1** chamada ao analyzer.
   - Testar cooldown diferenciado em `STAY` (30 s) vs `AWAY` (10 s).
   - Testar upgrade de prioridade: `camera_motion` seguido de `smoke_detected` 1 s depois → `smoke_detected` fura o cooldown e executa com `context="smoke"`.
   - Testar fallback em erro de captura e erro/timeout do analyzer → `camera_analysis` é publicado com `status != "ok"` e `memory.add_events` é chamado **antes** de `bus.publish`.

### Critério de Saída (Fase 4)

- `.venv\Scripts\python.exe -m pytest -q` passa 100% (todos os testes novos + existentes).

---

## Fase 5 — Integração Cirúrgica: `AgentBridge`, Agente Segurança, FastAPI e Twin 3D

**Referência arquitetural:** [docs/vision_architecture.md §3.2](vision_architecture.md#L68-L132), [§4.2](vision_architecture.md#L152-L165), [§7.2](vision_architecture.md#L261-L273).

### Tarefas

1. **Atualizar [src/agents/bridge.py](../src/agents/bridge.py):**
   - Adicionar `"camera_analysis"` a `SECURITY_EVENTS`.
   - Em `AgentBridge.__init__`, adicionar parâmetro opcional `vision_worker=None` (default `None` para retrocompatibilidade total).
   - Em `on_event(self, e: Event)` (chamado de qualquer thread — **usar sempre agendamento cross-thread seguro no loop asyncio**):
     - Se `e.type == "camera_analysis"`: agendar cancelamento do watchdog no loop com `self._loop.call_soon_threadsafe(self._cancel_watchdog, e.room)`.
     - Se `e.type in ("camera_motion", "camera_sound")` e `self._vision_worker is not None and self._vision_worker.is_alive() and self._vision_worker.will_handle(e, self.memory.state)`:
       - **Não** colocar `e` já na fila de `"seguranca"`.
       - Agendar o watchdog via `self._loop.call_soon_threadsafe(self._schedule_watchdog, e.room, e)` — dentro de `_schedule_watchdog` (já na thread do loop asyncio!), criar o `TimerHandle` com `self._loop.call_later(self.s.vision_timeout_s + 5.0, self._watchdog_fire, room_key, e)` e guardá-lo em `self._watchdogs[room_key]` (cancelando eventual handle anterior da mesma divisão). *(Atenção: nunca guardar o retorno de `call_soon_threadsafe`, pois esse `Handle` não cancela o `TimerHandle` interno de `call_later`.)*
       - Em `stop()`, cancelar todos os handles pendentes em `self._watchdogs`.
     - `security_alarm`, `tamper_alarm` e `contact_opened` (armado) continuam a seguir **de imediato** para `"seguranca"` (paralelismo intencional documentado em [docs/vision_architecture.md §3.2](vision_architecture.md#L105-L120)).
   - Em `build_prompt(events, state, now)`:
     - Para eventos `e.type == "camera_analysis"`, acrescentar à linha do evento um **resumo compacto** extraído de `e.data` (sem despejar o JSON completo), ex.:
       `pessoas=1, categoria=person, janela_aberta=sim, fumo=não, confiança=0.85, nota="Uma pessoa junto à janela aberta."` (ou `status=capture_error` quando `status != "ok"`).

2. **Atualizar [src/agents/specs.py](../src/agents/specs.py#L47-L53):**
   - Ajustar ligeiramente as `instructions` de `"seguranca"` para mencionar `camera_analysis` (usar confirmação visual de pessoas, janela aberta ou falso alarme para decidir se propõe acender luzes; se não houver pessoas e parecer falso alarme, devolver `"propostas": []`).

3. **Atualizar [src/agents/tools.py](../src/agents/tools.py):**
   - Adicionar `@tool(description="Última análise visual da câmara (pessoas, estado da janela/porta, fumo visível, nota factual).") async def ultima_analise_camara(divisao: Annotated[str | None, Field(description="Nome ou id da divisão; vazio = última disponível")] = None) -> Any:` que faz `await _get("/tools/ultima_analise_camara", divisao=divisao)`.
   - Incluir `ultima_analise_camara` em `TOOLS_BY_AGENT["seguranca"]`.

4. **Atualizar [src/api.py](../src/api.py):**
   - Em `create_app(memory, settings, coordinator=None, vision_worker=None)`:
     - `GET /tools/ultima_analise_camara`: aceita query param `divisao: str | None = None`; lê `memory.latest_vision(divisao)` e devolve `obs.model_dump()` (ou `{"disponivel": False, "mensagem": "Ainda não existe análise visual."}`).
     - `GET /vision/frames/{filename}`: valida com `resolve_frame_path(settings.vision_frames_dir, filename)`; retorna `404` se inválido ou inexistente, caso contrário `FileResponse(..., media_type="image/jpeg")`.
     - `POST /vision/analyze`: aceita body opcional `{"divisao": str | None, "contexto": "intrusion" | "smoke" | "general"}`; retorna `503` se `vision_worker is None`, caso contrário executa `vision_worker.analyze_now(...)` (em threadpool do FastAPI se rota `def`) e devolve `obs.model_dump()`.
   - Em `main()`:
     - Se `settings.vision_enabled`: instanciar `vision_worker = VisionWorker(memory, bus, settings).start()` e subscrever explicitamente:
       ```python
       bus.subscribe(vision_worker.on_event, types=VISION_TRIGGER_EVENTS)
       ```
       *(logo após `bus.subscribe(coordinator.on_event, types=["smoke_detected", "alarm_cancelled"])`)*.
     - Passar `vision_worker=vision_worker` para `AgentBridge(...)` e `create_app(...)`.
     - Parar `vision_worker.stop()` no bloco `finally:`.

5. **Atualizar [src/twin/state.py](../src/twin/state.py#L23-L31):**
   - Adicionar `"camera_analysis": "Segurança"` em `AREA_BY_EVENT`.

6. **Adicionar testes de integração em `tests/test_vision.py`:**
   - `AgentBridge` + `VisionWorker` vivo: `camera_motion` (em `AWAY`) é diferido → `camera_analysis` cancela o watchdog e o Agente de Segurança é chamado **1 única vez** com os factos visuais no prompt.
   - `AgentBridge` + `VisionWorker` morto (`is_alive() == False`): `camera_motion` vai **diretamente** para `"seguranca"` sem bloqueio.
   - `AgentBridge` watchdog: se `VisionWorker` vivo não publicar `camera_analysis` dentro do timeout (usar timeout curto no teste, ex. `0.1 s`), o watchdog reencaminha o `camera_motion` original para `"seguranca"`.
   - Paralelismo intencional (`security_alarm` + `camera_motion` quase simultâneos): `security_alarm` acorda `"seguranca"` de imediato, `camera_motion` é diferido/deduplicado, e quando `camera_analysis` chega a seguir `"seguranca"` recebe a 2.ª chamada já com os factos visuais no prompt (sem crash nem race condition).
   - Concorrência `analyze_now()` vs. `_process_job()`: duas chamadas simultâneas (trigger + `POST /vision/analyze`) serializam corretamente via `_io_lock` sem erro no `httpx.Client`.
   - Interação de cooldowns: dois `camera_analysis` com `severity="warning"`/`"critical"` a 15 s de intervalo **não** são bloqueados pelo cooldown de 60 s do `AgentBridge`; dois com `severity="info"` são deduplicados no bridge.
   - FastAPI (`TestClient`): testar `GET /tools/ultima_analise_camara`, `POST /vision/analyze`, `GET /vision/frames/<valido>.jpg` (200) e `GET /vision/frames/..%2F..%2F.env` (404).
   - Twin 3D (`twin_state` / `twin_stream`): verificar que `camera_analysis` aparece em `events` com `"area": "Segurança"`.

### Critério de Saída (Fase 5)

- `.venv\Scripts\python.exe -m pytest -q` passa **100%** (todos os testes anteriores + nova suite completa de visão).

---

## Fase 6 — Validação End-to-End ao Vivo (Simulador + Servidor Ollama)

**Referência arquitetural:** [docs/vision_architecture.md §8.1](vision_architecture.md#L294-L305).

Executar contra o servidor **Ollama** da equipa em `http://192.168.2.161:1234/v1` (ou contra Ollama local em `http://localhost:11434/v1`) com `qwen3.8:latest` em modo seguro (`SIMULATE=true`, `DRY_RUN=true`):

1. Arrancar a API + agentes em modo simulado (garantindo explicitamente `VISION_TRIGGER_DISARMED="false"` para o Ensaio D):
   ```powershell
   $env:SIMULATE="true"; $env:DRY_RUN="true"; $env:POLL_INTERVAL_S="1"
   $env:VISION_ENABLED="true"; $env:VISION_TRIGGER_DISARMED="false"
   $env:LLM_BASE_URL="http://192.168.2.161:1234/v1"; $env:LLM_MODEL="qwen3.8:latest"
   # Para Ollama local na própria máquina, usar: $env:LLM_BASE_URL="http://localhost:11434/v1"
   .venv\Scripts\python.exe -m src.api --sim --agents
   ```
   *(Opcional: confirmar antes/depois via `GET http://192.168.2.161:1234/api/ps` — ou `localhost:11434/api/ps` — que apenas `qwen3.8:latest` permanece carregado em VRAM sem model swapping.)*
2. Executar os 5 ensaios funcionais + validação de encerramento limpo:
   - **Ensaio A — Intrusão real (`cenario intrusao` com imagem de pessoa + janela aberta):** confirmar 1 única chamada visual, `has_person=True`, `window_or_door_open=True`, e resposta do Agente de Segurança com contexto visual.
   - **Ensaio B — Falso alarme (`cenario intrusao` com imagem vazia / animal):** confirmar `has_person=False`, `category="empty"|"pet"`, e identificação de falso alarme pelo Agente de Segurança.
   - **Ensaio C — Emergência de fumo (`cenario fumo`):** confirmar que `Coordinator.emergency()` atua de imediato (0 atraso) e `VisionWorker` publica `camera_analysis` informativo em paralelo.
   - **Ensaio D — Eficiência energética (`cenario janela` em `DISARMED`):** confirmar **0 chamadas** ao módulo de visão.
   - **Ensaio E — On-demand & Snapshot:** chamar `POST /vision/analyze`, verificar `GET /tools/ultima_analise_camara` e abrir `GET /vision/frames/<filename>`.
   - **Ensaio F — Encerramento limpo (Ctrl+C):** parar o processo e confirmar que o bloco `finally` de `src/api.py` executa `vision_worker.stop()` (fecha o `httpx.Client` e termina a worker thread sem ficar pendurado).
3. Verificação final de higiene antes de commit/PR:
   ```powershell
   git status
   git diff --stat
   .venv\Scripts\python.exe -m pytest -q
   ```
   Confirmar que não há segredos, ficheiros temporários em `var/`, nem alterações não intencionais fora dos ficheiros listados na Fase 5.

