// Chat with the Coordinator (the user's only conversation with the house).
// mountChat(container) builds the panel; messages come from /chat/messages (polling 2 s).
// Buttons post to /chat/action. Other modules listen to window events:
//   "tt:layout-edit"  -> the 3D page opens its drag mode;  "tt:knowledge" -> opens "O que a casa sabe de ti".

const POLL_MS = 2000;
const $el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" });
const EXAMPLES = ["A janela da sala está fechada?", "Quanto gastei ontem?", "Quero o quarto a 21 °C de manhã",
  "Se abrir a janela com o aquecedor ligado, avisa-me", "Tens alguma sugestão?"];

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// small SVG line chart: series [{label, unit, points: [[ts, value], ...]}], optional highlight windows
export function lineChart(series, { height = 120, windows = [] } = {}) {
  const all = series.flatMap((s) => s.points);
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 300 ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  if (!all.length) return svg;
  const xs = all.map((p) => p[0]), ys = all.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 - y0 < 1) { y0 -= 0.5; y1 += 0.5; }
  const pad = 14;
  const X = (t) => pad + (x1 === x0 ? 0 : ((t - x0) / (x1 - x0)) * (300 - 2 * pad));
  const Y = (v) => height - pad - ((v - y0) / (y1 - y0)) * (height - 2 * pad);
  for (const w of windows) {
    const r = document.createElementNS(svgNS, "rect");
    r.setAttribute("x", X(Math.max(w[0], x0))); r.setAttribute("width", Math.max(1, X(Math.min(w[1], x1)) - X(Math.max(w[0], x0))));
    r.setAttribute("y", 0); r.setAttribute("height", height); r.setAttribute("fill", "rgba(242,177,60,.16)");
    svg.appendChild(r);
  }
  const colors = ["#6aa8ff", "#3ccf7f", "#f2b13c"];
  series.forEach((s, i) => {
    const pl = document.createElementNS(svgNS, "polyline");
    pl.setAttribute("points", s.points.map((p) => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" "));
    pl.setAttribute("fill", "none"); pl.setAttribute("stroke", colors[i % 3]); pl.setAttribute("stroke-width", "1.6");
    pl.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(pl);
  });
  for (const [v, y] of [[y1, pad - 3], [y0, height - 3]]) {
    const t = document.createElementNS(svgNS, "text");
    t.setAttribute("x", 2); t.setAttribute("y", y); t.setAttribute("fill", "#95a0b3"); t.setAttribute("font-size", "9");
    t.textContent = `${(Math.round(v * 10) / 10).toLocaleString("pt-PT")}${series[0]?.unit ? " " + series[0].unit : ""}`;
    svg.appendChild(t);
  }
  return svg;
}

export function mountChat(container, { title = "Coordenador", compact = false } = {}) {
  container.classList.add("tt-chat");
  container.innerHTML = "";
  const topo = $el("div", "tt-chat-topo");
  const h = $el("h2", null, title);
  h.appendChild($el("small", null, "Fala só comigo; eu trato do resto com os agentes."));
  const saber = $el("button", null, "O que a casa sabe de ti");
  saber.addEventListener("click", () => window.dispatchEvent(new CustomEvent("tt:knowledge")));
  topo.append(h, saber);
  const lista = $el("div", "tt-chat-lista");
  const sugestoes = $el("div", "tt-chat-sugestoes");
  for (const ex of EXAMPLES) {
    const b = $el("button", null, ex);
    b.addEventListener("click", () => send(ex));
    sugestoes.appendChild(b);
  }
  const form = $el("form", "tt-chat-form");
  const input = $el("input");
  input.placeholder = "Escreve ao Coordenador…";
  input.setAttribute("aria-label", "Mensagem para o Coordenador");
  const enviar = $el("button", null, "Enviar");
  enviar.type = "submit";
  form.append(input, enviar);
  container.append(topo, lista, ...(compact ? [] : [sugestoes]), form);

  let lastId = 0;
  const answered = new Set();  // decision / confirm ids already answered: their buttons are disabled
  let busy = false;

  function render(m) {
    if (document.getElementById(`tt-msg-${m.id}`)) return;
    const div = $el("div", `tt-msg ${m.role === "user" ? "user" : "coordenador"}`);
    div.id = `tt-msg-${m.id}`;
    if (m.decision_id) div.classList.add("pendente");
    if (m.suggestion_id) div.classList.add("sugestao");
    if (m.kind === "rule") div.classList.add("regra");
    const quem = $el("div", "quem", m.role === "user" ? "Tu" : "Coordenador");
    quem.appendChild($el("span", "hora", hhmm(m.ts)));
    div.append(quem, $el("div", "texto", m.text));
    const buttons = m.buttons || [];
    if (buttons.length || m.why || m.suggestion_id) {
      const row = $el("div", "tt-botoes");
      const key = m.decision_id || m.confirm_id || m.suggestion_id || "";
      for (const b of buttons) {
        const btn = $el("button", /Aprovar|Confirmar|Aceitar|Está bem/.test(b.label) ? "primario" : null, b.label);
        btn.disabled = answered.has(key);
        btn.addEventListener("click", () => press(b, row, key));
        row.appendChild(btn);
      }
      if (m.why || m.suggestion_id) {
        const why = $el("button", null, "Porquê?");
        why.addEventListener("click", () => showWhy(m, div, why));
        row.appendChild(why);
      }
      div.appendChild(row);
    }
    lista.appendChild(div);
  }

  async function showWhy(m, div, btn) {
    const old = div.querySelector(".tt-porque");
    if (old) { old.remove(); return; }
    const box = $el("div", "tt-porque");
    if (m.why) {
      box.appendChild($el("div", null, m.why.explicacao || ""));
      if (m.why.debate?.length) {
        const ul = $el("ul");
        for (const d of m.why.debate) ul.appendChild($el("li", null, d));
        box.appendChild(ul);
      }
    }
    if (m.suggestion_id) {
      try {
        const ev = await api(`/insights/${m.suggestion_id}/evidence`);
        box.appendChild($el("div", null, ev.resumo || ""));
        box.appendChild(lineChart(ev.series || [], { windows: ev.janelas || [] }));
        if (ev.legenda) box.appendChild($el("div", null, ev.legenda));
      } catch (e) {
        box.appendChild($el("div", null, "Não consegui carregar o histórico."));
      }
    }
    div.appendChild(box);
    btn.blur();
  }

  async function press(b, row, key) {
    if (b.action === "layout_manual") { window.dispatchEvent(new CustomEvent("tt:layout-edit")); }
    let comment = null;
    if (b.ask) {
      comment = window.prompt(b.ask);
      if (comment === null) return;
    }
    answered.add(key);
    row.querySelectorAll("button").forEach((x) => { if (x.textContent !== "Porquê?") x.disabled = true; });
    try {
      const out = await api("/chat/action", { action: b.action, id: b.id || null, response: b.response || null, comment });
      out.messages.forEach(render);
      bump(out.messages);
      if (b.action.startsWith("layout")) window.dispatchEvent(new CustomEvent("tt:layout-changed"));
    } catch (e) {
      render({ id: `err${Date.now()}`, ts: Date.now() / 1000, role: "coordenador", text: `Erro: ${e.message}` });
    }
    lista.scrollTop = lista.scrollHeight;
  }

  function bump(msgs) {
    for (const m of msgs) if (typeof m.id === "number") lastId = Math.max(lastId, m.id);
  }

  async function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true;
    enviar.disabled = true;
    input.value = "";
    const wait = $el("div", "tt-escrever", "O Coordenador está a pensar…");
    render({ id: `tmp${Date.now()}`, ts: Date.now() / 1000, role: "user", text });
    lista.appendChild(wait);
    lista.scrollTop = lista.scrollHeight;
    try {
      const out = await api("/chat", { text });
      lista.querySelectorAll('[id^="tt-msg-tmp"]').forEach((n) => n.remove());
      out.messages.forEach(render);
      bump(out.messages);
      if (out.messages.some((m) => m.layout_changed)) window.dispatchEvent(new CustomEvent("tt:layout-changed"));
    } catch (e) {
      render({ id: `err${Date.now()}`, ts: Date.now() / 1000, role: "coordenador", text: `Erro: ${e.message}` });
    } finally {
      wait.remove();
      busy = false;
      enviar.disabled = false;
      input.focus();
      lista.scrollTop = lista.scrollHeight;
    }
  }

  form.addEventListener("submit", (ev) => { ev.preventDefault(); send(input.value); });

  async function poll() {
    try {
      const msgs = await api(`/chat/messages?after=${lastId}`);
      if (msgs.length) {
        const atBottom = lista.scrollHeight - lista.scrollTop - lista.clientHeight < 40;
        msgs.forEach(render);
        bump(msgs);
        if (atBottom || msgs.some((m) => m.role !== "user")) lista.scrollTop = lista.scrollHeight;
        if (msgs.some((m) => m.layout_changed)) window.dispatchEvent(new CustomEvent("tt:layout-changed"));
      }
    } catch { /* API down: try again */ }
  }
  (async () => {
    try {
      const pend = await api("/chat/pending");
      // decisions that are no longer pending: their buttons start disabled
      const open = new Set((pend.decisions || []).map((d) => d.id));
      if (pend.confirm) open.add(pend.confirm.id);
      for (const s of pend.suggestions || []) open.add(s.id);
      const msgs = await api("/chat/messages?after=0&n=80");
      for (const m of msgs) {
        const key = m.decision_id || m.confirm_id || m.suggestion_id;
        if (key && !open.has(key)) answered.add(key);
      }
      msgs.forEach(render);
      bump(msgs);
      lista.scrollTop = lista.scrollHeight;
    } catch { /* first poll will retry */ }
    setInterval(poll, POLL_MS);
  })();

  return { send, element: container };
}
