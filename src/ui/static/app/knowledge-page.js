// "O que a casa sabe de ti" as a page: the target temperatures, how much the house may do on its own,
// your preferences and rules (with the sentence you said), the history and the erase. Same endpoints
// as the old modal (/knowledge, /knowledge/setpoint, /knowledge/autonomy, /knowledge/prefs/:id/...).
import { $, el, nf1, hhmm, renderShell, setConnection } from "./shell.js";

const nf = new Intl.NumberFormat("pt-PT", { maximumFractionDigits: 1 });
const put = (node, ...parts) => node.replaceChildren(...parts.filter((x) => x != null && x !== false));

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status}`);
  return data;
}

const EDIT_HINT = {
  setpoint_c: "Temperatura em °C, por exemplo 21", comfort_range_c: "Intervalo em °C, por exemplo 19-22",
  humidity_max_pct: "Humidade máxima em %, por exemplo 65", autonomy: "auto, perguntar ou nunca",
  quiet_hours: "Horas sem sugestões, por exemplo 23:00-07:30", note: "Texto da nota",
};

// inline form: one field, Guardar / Cancelar; Enter saves, Escape cancels; the error sits under the field
function inlineForm({ value = "", placeholder, label, type = "text", attrs = {}, onSave, onCancel }) {
  const form = el("form", "kn-form");
  const input = el("input");
  input.type = type;
  input.value = value;
  input.name = "valor";
  input.autocomplete = "off";
  if (placeholder) input.placeholder = placeholder;
  input.setAttribute("aria-label", label);
  Object.entries(attrs).forEach(([k, v]) => input.setAttribute(k, v));
  const guardar = el("button", "btn sm primary", "Guardar");
  guardar.type = "submit";
  const cancelar = el("button", "btn sm quiet", "Cancelar");
  cancelar.type = "button";
  const erro = el("p", "kn-form-err");
  erro.setAttribute("role", "alert");
  form.append(input, guardar, cancelar, erro);
  cancelar.addEventListener("click", onCancel);
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); onCancel(); } });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!input.value.trim()) { erro.textContent = "Escreve um valor."; input.focus(); return; }
    guardar.disabled = true;
    guardar.textContent = "A guardar…";
    try { await onSave(input.value.trim()); }
    catch (err) { erro.textContent = err.message; guardar.disabled = false; guardar.textContent = "Guardar"; input.focus(); }
  });
  setTimeout(() => { input.focus(); input.select(); });
  return form;
}

// ------------------------------------------------------------------ sections
// target temperatures: per room an instrument rule from 14 to 26 °C. The dark dot is the target (a native
// range input: drag it, or arrow keys in 0,5 °C steps); a small mark is the temperature now; the stretch
// between them is tinted (orange: the house will heat; blue: the target is below). Saves 600 ms after the
// last change and says so.
const R_MIN = 14, R_MAX = 26, R_STEP = 0.5, SAVE_MS = 600;
let roomsNow = {};  // room id -> current temperature (from /rooms)
const pct = (v) => `${((Math.min(R_MAX, Math.max(R_MIN, v)) - R_MIN) / (R_MAX - R_MIN)) * 100}%`;

function renderTemps(k) {
  const box = $("kn-temps");
  if (!k.temperaturas.length) { put(box, el("p", "kn-empty", "Ainda estou a ler as divisões da casa.")); return; }
  const yours = k.temperaturas.filter((t) => t.origem === "tua").length;
  $("kn-temps-hint").textContent = yours
    ? "Arrasta o ponto ou usa as setas do teclado"
    : "Ainda são os valores de origem da casa. Arrasta o ponto para mudar.";
  put(box, ...k.temperaturas.map(tempRow));
}

function tempRow(t) {
  const row = el("div", "kn-room");
  const now = roomsNow[t.divisao_id];
  let value = t.c ?? 20, timer = null;
  const id = `alvo-${t.divisao_id}`;

  // left: the room, the temperature now, and where the target comes from only when it is yours
  const mine = el("span", "kn-mine", "definido por ti");
  mine.hidden = t.origem !== "tua";
  const label = el("label", "kn-room-name", t.divisao);
  label.htmlFor = id;
  const left = el("div", "kn-room-head", label,
    el("span", "kn-now", now == null ? "sem sensor de temperatura" : `agora ${nf1.format(now)} °C`), mine);

  // the rule
  const rule = el("div", "kn-rule");
  const ticks = el("div", "kn-ticks");
  ticks.setAttribute("aria-hidden", "true");
  for (let v = R_MIN; v <= R_MAX; v += 1) {
    const tk = el("span", "kn-tick" + (v % 2 === 0 ? " major" : ""));
    tk.style.left = pct(v);
    if (v % 4 === 2) tk.append(el("span", "kn-tick-l", `${v}°`));
    ticks.append(tk);
  }
  const gap = el("span", "kn-gap");
  gap.setAttribute("aria-hidden", "true");
  const nowMark = el("span", "kn-nowmark");
  nowMark.setAttribute("aria-hidden", "true");
  if (now != null) nowMark.style.left = pct(now); else nowMark.hidden = true;
  const input = el("input", "kn-range");
  Object.assign(input, { type: "range", id, min: R_MIN, max: R_MAX, step: R_STEP, value });
  input.dataset.focus = `temp:${t.divisao_id}`;
  rule.append(ticks, gap, nowMark, input);

  // right: the target, big, and the save state
  const fig = el("output", "kn-target", nf1.format(value));
  fig.htmlFor = id;
  const status = el("span", "kn-step-status");
  status.setAttribute("role", "status");
  const right = el("div", "kn-room-target", el("span", "kn-target-wrap", fig, el("span", "kn-target-u", "°C")), status);

  const sync = () => {
    fig.textContent = nf1.format(value);
    input.setAttribute("aria-valuetext", `${nf1.format(value)} °C`);
    if (now != null) {
      const a = Math.min(now, value), b = Math.max(now, value);
      gap.style.left = pct(a);
      gap.style.width = `calc(${pct(b)} - ${pct(a)})`;
      gap.className = "kn-gap " + (value - now > 0.25 ? "heat" : now - value > 0.25 ? "cool" : "");
    }
  };
  const save = async () => {
    status.textContent = "A guardar…"; status.className = "kn-step-status";
    try {
      await api("/knowledge/setpoint", { divisao_id: t.divisao_id, c: value });
      t.origem = "tua"; t.c = value; mine.hidden = false;
      status.textContent = "Guardado"; status.className = "kn-step-status ok";
      setTimeout(() => { if (status.textContent === "Guardado") status.textContent = ""; }, 2500);
    } catch (e) { status.textContent = `Não guardei: ${e.message}`; status.className = "kn-step-status bad"; }
  };
  input.addEventListener("input", () => { value = Number(input.value); sync(); status.textContent = ""; });
  input.addEventListener("change", () => { clearTimeout(timer); timer = setTimeout(save, SAVE_MS); });
  sync();

  row.append(left, rule, right);
  return row;
}

// autonomy: a three-stop scale per area, least to most (Nunca · Pergunta-me · Faz sozinha), as native
// radios (arrow keys work); under it, in words, what the house will do with the choice made
const SCALE = [["nunca", "Nunca"], ["perguntar", "Pergunta-me"], ["auto", "Faz sozinha"]];
const CONSEQ = {
  energia: {
    nunca: "Quando houver desperdício, só te informa. Não desliga nada.",
    perguntar: "Quando houver desperdício, propõe desligar e espera pela tua aprovação no chat.",
    auto: "Desliga sozinha o aquecedor ou a tomada que está a desperdiçar e conta-te depois.",
  },
  conforto: {
    nunca: "Avisa quando uma divisão foge do alvo, sem mexer no aquecimento.",
    perguntar: "Propõe ligar ou desligar o aquecimento para chegar ao alvo e espera pela tua aprovação.",
    auto: "Liga e desliga o aquecimento sozinha para manter cada divisão no alvo.",
  },
  seguranca: {
    nunca: "Regista e avisa o que vê. Armar e desarmar continuam a ser contigo.",
    perguntar: "Propõe o que fazer quando algo abre com a casa armada e espera pela tua decisão.",
    auto: "Age sozinha quando algo abre com a casa armada (armar e desarmar pedem sempre confirmação).",
  },
  luz: {
    nunca: "Não muda a luz; só te informa.",
    perguntar: "Propõe mudar a luz (por exemplo, âmbar como aviso) e espera pela tua aprovação.",
    auto: "Muda a luz sozinha como aviso ou por rotina.",
  },
};

function renderAutonomy(k) {
  const allAsk = k.autonomia.every((a) => a.nivel === "perguntar");
  $("kn-aut-note").hidden = !allAsk;
  $("kn-aut-note").textContent = "Neste momento a casa pergunta-te tudo antes de agir. Quando aprovares a mesma coisa três vezes seguidas, pergunta se pode passar a fazê-la sozinha.";
  put($("kn-aut"), ...k.autonomia.map((a) => {
    const row = el("div", "kn-aut-row");
    const conseq = el("p", "kn-conseq");
    const status = el("span", "kn-step-status");
    status.setAttribute("role", "status");
    const say = (lvl) => { conseq.textContent = CONSEQ[a.categoria]?.[lvl] || ""; row.dataset.level = lvl; };
    const scale = el("fieldset", "kn-scale");
    scale.append(el("legend", "sr-only", `O que a casa faz sozinha: ${a.nome}`));
    const track = el("div", "kn-scale-track");
    for (const [nivel, label] of SCALE) {
      const id = `aut-${a.categoria}-${nivel}`;
      const input = el("input");
      input.type = "radio"; input.name = `aut-${a.categoria}`; input.id = id; input.value = nivel;
      input.checked = a.nivel === nivel;
      input.dataset.focus = `aut:${a.categoria}:${nivel}`;
      input.addEventListener("change", async () => {
        const prev = a.nivel;
        say(nivel);
        status.textContent = "A guardar…"; status.className = "kn-step-status";
        try {
          await api("/knowledge/autonomy", { categoria: a.categoria, nivel });
          a.nivel = nivel;
          status.textContent = "Guardado"; status.className = "kn-step-status ok";
          setTimeout(() => { if (status.textContent === "Guardado") status.textContent = ""; }, 2500);
          $("kn-aut-note").hidden = !k.autonomia.every((x) => x.nivel === "perguntar");
        } catch (e) {
          status.textContent = `Não guardei: ${e.message}`; status.className = "kn-step-status bad";
          scale.querySelector(`input[value="${prev}"]`).checked = true;
          say(prev);
        }
      });
      const lab = el("label", "kn-stop", el("span", "kn-stop-dot"), el("span", "kn-stop-label", label));
      lab.htmlFor = id;
      track.append(input, lab);
    }
    scale.append(track);
    say(a.nivel);
    row.append(el("div", "kn-aut-text", el("h3", null, a.nome, status), conseq), scale);
    return row;
  }));
}

function item(p, { onEdit, onRevoke, revokeLabel = "Revogar", extra } = {}) {
  const li = el("li", "kn-item" + (p.ativa ? "" : " is-off"));
  const body = el("div", "kn-item-body", el("p", "kn-item-what", p.descricao),
    p.frase ? el("q", "kn-quote", p.frase) : null,
    el("p", "kn-item-meta", [p.ambito, p.origem, p.data, extra].filter(Boolean).join(" · ")));
  const acts = el("div", "kn-item-acts");
  if (onEdit && p.editavel && p.ativa) {
    const b = el("button", "kn-text-btn", "Editar");
    b.type = "button";
    b.addEventListener("click", () => {
      b.hidden = true;
      const form = inlineForm({
        placeholder: EDIT_HINT[p.tipo] || "Novo valor", label: `Novo valor: ${p.descricao}`,
        onSave: (text) => onEdit(p, text),
        onCancel: () => { form.remove(); b.hidden = false; b.focus(); },
      });
      body.append(form);
    });
    acts.append(b);
  }
  if (onRevoke && p.ativa) {
    const b = el("button", "kn-text-btn danger", revokeLabel);
    b.type = "button";
    b.addEventListener("click", () => onRevoke(p, b));
    acts.append(b);
  }
  li.append(body, acts);
  return li;
}

function list(node, rows, empty) {
  put(node, ...(rows.length ? rows : [el("li", "kn-empty", empty)]));
}

// revoke: a second press confirms (no browser dialog), and the row says what happens
function confirmRevoke(p, b, fn) {
  if (b.dataset.armed) { fn(); return; }
  const label = b.textContent;
  b.dataset.armed = "1";
  b.textContent = "Carrega outra vez para confirmar";
  setTimeout(() => { if (b.isConnected) { delete b.dataset.armed; b.textContent = label; } }, 4000);
}

// ------------------------------------------------------------------ render
async function render(focusKey) {
  let k;
  try {
    k = await api("/knowledge");
    $("kn-erro").hidden = true;
  } catch (e) {
    $("kn-erro").hidden = false;
    $("kn-erro").textContent = `Não consegui ler o que a casa sabe: ${e.message}. Tenta recarregar a página.`;
    return;
  }
  const onEdit = async (p, text) => { await api(`/knowledge/prefs/${p.id}/edit`, { text }); render(); };
  const onRevoke = (p, b) => confirmRevoke(p, b, async () => {
    try { await api(`/knowledge/prefs/${p.id}/revoke`, {}); } catch (e) { b.textContent = e.message; return; }
    render();
  });

  try { (await api("/rooms")).forEach((r) => { roomsNow[r.id] = r.sensores?.temperatura_c ?? null; }); } catch { /* no live rooms */ }
  renderTemps(k);
  renderAutonomy(k);
  list($("kn-prefs"), k.preferencias.map((p) => item(p, { onEdit, onRevoke })),
    "Ainda nenhuma. Diz no chat, por exemplo «não quero sugestões depois das 23h».");
  list($("kn-rules"), k.regras.map((p) => item(p, { onRevoke, revokeLabel: "Revogar", extra: p.onde })),
    "Sem regras. Cria uma no chat: «se abrir a janela com o aquecedor ligado, avisa-me».");
  $("kn-muted-wrap").hidden = !k.silenciadas.length;
  list($("kn-muted"), k.silenciadas.map((p) => item(p, { onRevoke, revokeLabel: "Voltar a permitir" })), "");

  const hist = k.historico.map((p) => {
    const estado = p.ativa ? "ativa" : (p.substituida_por ? "substituída" : "revogada");
    return item(p, { extra: `${p.tipo_pt} · ${estado}` });
  }).concat(k.layout.map((p) => item(p, { extra: "disposição do 3D" })));
  $("kn-hist-n").textContent = `${hist.length} ${hist.length === 1 ? "alteração" : "alterações"}`;
  list($("kn-hist"), hist, "Sem histórico.");
  $("kn-note").textContent = `Respondeste a ${k.decisoes_respondidas} decisões; essas respostas também contam como o que a casa sabe de ti. ${k.nota}`;

  // hero: what the house knows, in numbers
  $("kn-lede").textContent = `${k.preferencias.length} ${k.preferencias.length === 1 ? "preferência" : "preferências"}, `
    + `${k.regras.length} ${k.regras.length === 1 ? "regra" : "regras"} e ${k.decisoes_respondidas} decisões respondidas, `
    + "cada uma com a frase em que a disseste. Podes mudar, revogar ou apagar tudo.";
  if (focusKey) document.querySelector(`[data-focus="${focusKey}"]`)?.focus();
}

// ------------------------------------------------------------------ erase (typed confirmation, inline)
const eraseInput = $("kn-erase-input"), eraseBtn = document.querySelector(".kn-erase-btn"), eraseMsg = $("kn-erase-msg");
eraseInput.addEventListener("input", () => { eraseBtn.disabled = eraseInput.value.trim().toUpperCase() !== "APAGAR"; });
$("kn-erase").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (eraseInput.value.trim().toUpperCase() !== "APAGAR") return;
  eraseBtn.disabled = true;
  eraseBtn.textContent = "A apagar…";
  try {
    const out = await api("/knowledge/erase", { confirm: "APAGAR" });
    eraseMsg.textContent = `Apagado: ${out.preferencias} preferências, ${out.decisoes} decisões, ${out.sugestoes} sugestões e ${out.mensagens} mensagens.`;
    eraseInput.value = "";
    render();
  } catch (err) {
    eraseMsg.textContent = `Não apaguei: ${err.message}`;
  } finally {
    eraseBtn.textContent = "Apagar tudo";
  }
});

// ------------------------------------------------------------------ the shell (house state), every 5 s
async function shellTick() {
  try {
    const res = await fetch("/twin/state?n=0");
    if (!res.ok) throw new Error(res.statusText);
    const st = await res.json();
    renderShell(st);
    setConnection(true, `atualizado ${hhmm(st.ts)}`);
  } catch { setConnection(false, "sem ligação"); }
}

render();
shellTick();
setInterval(shellTick, 5000);
