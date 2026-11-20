// Simulador: the simulated sensors and the demo scenarios of scripts/simulate.py, from the browser.
// Talks to /sim (src/sim/control.py). Every button says what it does and where, so a person or a
// browser agent can drive a demo here while the app is recorded in another tab.
import { $, el, nf1, hhmmss, hhmm, renderShell, setConnection } from "./shell.js";

const NOMES = { contactSensor: "Janela/porta", temperatureSensor: "Temperatura e humidade", plug: "Tomada (aquecedor)",
                smokeSensor: "Detetor de fumo", camera: "Câmara", bulb: "Luz" };
const MODO = { DISARMED: "desarmada", STAY: "armada (em casa)", AWAY: "armada (ausente)" };

async function call(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

let busy = false;
async function act(path, body) {
  if (busy) return;
  busy = true;
  document.body.classList.add("sim-busy");
  showError("");
  try {
    render(await call(path, body));
  } catch (e) {
    showError(e.message);
  } finally {
    busy = false;
    document.body.classList.remove("sim-busy");
  }
}

function showError(msg) {
  const box = $("sim-erro");
  box.textContent = msg;
  box.hidden = !msg;
}

function button(label, onClick, { aria, primary = false, disabled = false } = {}) {
  const b = el("button", `btn sm${primary ? " primary" : ""}`, label);
  b.type = "button";
  if (aria) b.setAttribute("aria-label", aria);
  b.disabled = disabled;
  b.addEventListener("click", onClick);
  return b;
}

function field(label, control) {
  const id = `f${Math.random().toString(36).slice(2, 8)}`;
  control.id = id;
  const l = el("label", null, label);
  l.htmlFor = id;
  const wrap = el("div", "sim-field");
  wrap.append(l, control);
  return wrap;
}

// ------------------------------------------------------------------ scenarios (built once, then updated)
let scenBuilt = false;
const scenCards = new Map();
function renderScenarios(view) {
  const box = $("sim-scen");
  if (!scenBuilt) {
    box.replaceChildren();
    for (const c of view.cenarios) {
      const card = el("article", "sim-card");
      card.setAttribute("aria-labelledby", `cen-${c.id}`);
      const h = el("h3", null, c.titulo);
      h.id = `cen-${c.id}`;
      const step = el("select");
      for (const s of [2, 4, 6, 10]) step.append(new Option(`${s} s`, String(s), s === 4, s === 4));
      const run = button("Correr cenário", () => act("/sim/cenario", { cenario: c.id, passo_s: Number(step.value) }),
        { aria: `Correr o cenário ${c.titulo}`, primary: true });
      const fields = el("div", "sim-fields", field("Entre passos", step), run);
      const onde = el("p", "sim-onde", c.onde.map((o) => `${o.sensor}: ${o.divisao}`).join(" · "));
      onde.hidden = !c.onde.length;
      const why = el("p", "sim-why");
      card.append(h, el("p", null, c.descricao), onde, fields, why);
      box.append(card);
      scenCards.set(c.id, { card, run, why, c });
    }
    if (!view.cenarios.length) box.append(el("p", "sim-empty", "Sem cenários."));
    scenBuilt = true;
  }
  const running = view.a_correr;
  for (const [id, { card, run, why, c: base }] of scenCards) {
    const c = view.cenarios.find((x) => x.id === id) || base;
    const blocked = c.disponivel ? "" : c.motivo;
    run.disabled = !!blocked || !!running;
    why.textContent = blocked;
    why.hidden = !blocked;
    card.classList.toggle("ativo", running?.cenario === id);
  }
  $("sim-running").hidden = !running;
  if (running) $("sim-running-text").textContent = `A correr: ${running.titulo} · ${running.divisao} · desde as ${hhmmss(running.desde)}`;
}

// ------------------------------------------------------------------ rooms
function deviceRow(room, d) {
  const name = NOMES[d.tipo] || d.nome;
  const where = room.nome;
  const row = el("div", "sim-dev");
  let state = "", on = false;
  const actions = el("div", "sim-row-actions");
  const a = (body) => act("/sim/acao", { divisao: room.id, ...body });
  if (d.tipo === "contactSensor") {
    state = d.aberto ? "Aberta" : "Fechada"; on = d.aberto;
    actions.append(
      button("Abrir", () => a({ acao: "abrir" }), { aria: `Abrir a janela/porta · ${where}`, disabled: d.aberto }),
      button("Fechar", () => a({ acao: "fechar" }), { aria: `Fechar a janela/porta · ${where}`, disabled: !d.aberto }));
  } else if (d.tipo === "temperatureSensor") {
    state = `${d.temperatura_c != null ? nf1.format(d.temperatura_c) : "—"} °C · ${d.humidade_pct != null ? Math.round(d.humidade_pct) : "—"} %`;
    const t = el("input"); t.type = "number"; t.step = "0.1"; t.min = "-10"; t.max = "45";
    t.value = d.temperatura_c ?? "";
    const hu = el("input"); hu.type = "number"; hu.step = "1"; hu.min = "0"; hu.max = "100";
    hu.value = d.humidade_pct != null ? Math.round(d.humidade_pct) : "";
    actions.append(
      field("Temperatura (°C)", t),
      button("Definir temperatura", () => a({ acao: "temperatura", valor: t.value }), { aria: `Definir a temperatura · ${where}` }),
      field("Humidade (%)", hu),
      button("Definir humidade", () => a({ acao: "humidade", valor: hu.value }), { aria: `Definir a humidade · ${where}` }));
  } else if (d.tipo === "plug") {
    state = d.ligada ? `Ligada · ${Math.round(d.potencia_w || 0)} W` : "Desligada"; on = d.ligada;
    actions.append(
      button("Ligar", () => a({ acao: "ligar_tomada" }), { aria: `Ligar a tomada (aquecedor) · ${where}`, disabled: d.ligada }),
      button("Desligar", () => a({ acao: "desligar_tomada" }), { aria: `Desligar a tomada (aquecedor) · ${where}`, disabled: !d.ligada }));
  } else if (d.tipo === "camera") {
    state = "—";
    actions.append(button("Simular movimento", () => a({ acao: "movimento" }), { aria: `Simular movimento na câmara · ${where}` }));
  } else if (d.tipo === "smokeSensor") {
    state = "—";
    actions.append(button("Disparar fumo", () => a({ acao: "fumo" }), { aria: `Disparar o detetor de fumo · ${where}` }));
  } else if (d.tipo === "bulb") {
    state = d.ligada ? "Acesa" : "Apagada"; on = d.ligada;
  }
  actions.append(button(d.online ? "Pôr offline" : "Pôr online",
    () => a({ acao: d.online ? "offline" : "online", valor: d.tipo }),
    { aria: `${d.online ? "Pôr offline" : "Pôr online"}: ${name} · ${where}` }));
  const st = el("span", `sim-dev-state${on ? " on" : ""}${d.online ? "" : " off-line"}`, d.online ? state : `offline · ${state}`);
  row.append(el("span", "sim-dev-name", name), st, actions);
  return row;
}

let roomsKey = "";
function renderRooms(view) {
  const box = $("sim-rooms");
  if (box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;  // typing a value
  const key = JSON.stringify(view.divisoes);
  if (key === roomsKey) return;
  roomsKey = key;
  const focused = document.activeElement?.getAttribute("aria-label");
  box.replaceChildren(...view.divisoes.map((r) => {
    const card = el("article", "sim-room");
    const h = el("h3", null, r.nome);
    if (r.simulada) h.append(el("span", "sim-tag", "só simulada"));
    card.append(h, ...r.dispositivos.map((d) => deviceRow(r, d)));
    return card;
  }));
  if (!view.divisoes.length) box.append(el("p", "sim-empty", "Não há divisões simuladas."));
  if (focused) box.querySelector(`[aria-label="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
}

// ------------------------------------------------------------------ the rest
function render(view) {
  if (!view.disponivel) {
    $("sim-scope").textContent = view.motivo;
    $("sim-scen").replaceChildren(el("p", "sim-empty", "O simulador não está ativo nesta execução."));
    $("sim-rooms").replaceChildren();
    return;
  }
  $("sim-scope").textContent = view.casa_toda
    ? "Casa toda simulada (--sim): podes mudar qualquer sensor, o alarme e a segurança."
    : "Casa real: só as divisões simuladas mudam aqui; o alarme e a segurança são da central real.";
  renderScenarios(view);
  renderRooms(view);
  $("casa").hidden = !view.seguranca;
  if (view.seguranca) {
    $("sim-sec").textContent = `Casa ${MODO[view.seguranca.modo] || view.seguranca.modo}${view.seguranca.alarme ? " · em alarme" : ""}`;
  }
  const log = $("sim-log");
  log.replaceChildren(...view.registo.map((r) => {
    const t = el("time", null, hhmmss(r.ts));
    t.dateTime = new Date(r.ts * 1000).toISOString();
    return el("li", null, t, el("span", null, r.texto));
  }));
  if (!view.registo.length) log.append(el("li", "sim-empty", "Ainda nada. Cada botão e cada passo de um cenário aparece aqui."));
}

document.querySelectorAll("#casa [data-acao]").forEach((b) => b.addEventListener("click", () =>
  act("/sim/acao", { acao: b.dataset.acao, valor: b.dataset.valor || null })));
$("sim-parar").addEventListener("click", () => act("/sim/parar", {}));

// reset: a second click within 4 s confirms (the same pattern as the knowledge page)
const reset = $("sim-repor");
reset.addEventListener("click", () => {
  if (!reset.dataset.armed) {
    reset.dataset.armed = "1";
    reset.textContent = "Carrega outra vez para repor";
    setTimeout(() => { if (reset.dataset.armed) { delete reset.dataset.armed; reset.textContent = "Repor o simulador"; } }, 4000);
    return;
  }
  delete reset.dataset.armed;
  reset.textContent = "Repor o simulador";
  act("/sim/acao", { acao: "repor" });
});

async function tick() {
  if (busy) return;
  try { render(await call("/sim")); } catch (e) { showError(e.message); }
}
async function shellTick() {
  try {
    const res = await fetch("/twin/state?n=0");
    if (!res.ok) throw new Error(res.statusText);
    const st = await res.json();
    renderShell(st);
    setConnection(true, `atualizado ${hhmm(st.ts)}`);
  } catch { setConnection(false, "sem ligação"); }
}
tick();
shellTick();
setInterval(tick, 1500);
setInterval(shellTick, 5000);
