import { icon } from "/static/app/icons.js";
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
// three short starters, wrapped (never a sideways scroller); the longer demo lines are typed
const EXAMPLES = ["A janela da sala está fechada?", "Quanto gastei ontem?", "Tens alguma sugestão?"];

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// categorical series colours, fixed order (the agents' palette on paper: blue, orange, teal)
const SERIES = ["#2c62b4", "#c4501c", "#11806f"];

export function chartLegend(series) {
  const box = $el("div", "tt-legenda");
  box.style.cssText = "display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:6px;font-size:11px";
  series.forEach((s, i) => {
    const item = $el("span", null);
    const sw = $el("i", null);
    sw.style.cssText = `display:inline-block;width:12px;height:2px;margin-right:6px;vertical-align:3px;background:${SERIES[i % SERIES.length]}`;
    item.append(sw, `${s.label || `Série ${i + 1}`}${s.unit ? ` (${s.unit})` : ""}`);
    box.appendChild(item);
  });
  return box;
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
    r.setAttribute("y", 0); r.setAttribute("height", height); r.setAttribute("fill", "rgba(226,83,27,.10)");
    svg.appendChild(r);
  }
  for (const v of [y0, (y0 + y1) / 2, y1]) {  // faint grid
    const g = document.createElementNS(svgNS, "line");
    g.setAttribute("x1", pad); g.setAttribute("x2", 300 - pad); g.setAttribute("y1", Y(v)); g.setAttribute("y2", Y(v));
    g.setAttribute("stroke", "rgba(24,24,27,.08)"); g.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(g);
  }
  series.forEach((s, i) => {
    const pl = document.createElementNS(svgNS, "polyline");
    pl.setAttribute("points", s.points.map((p) => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" "));
    pl.setAttribute("fill", "none"); pl.setAttribute("stroke", SERIES[i % SERIES.length]); pl.setAttribute("stroke-width", "2");
    pl.setAttribute("stroke-linejoin", "round"); pl.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(pl);
  });
  for (const [v, y] of [[y1, pad - 3], [y0, height - 3]]) {
    const t = document.createElementNS(svgNS, "text");
    t.setAttribute("x", 2); t.setAttribute("y", y); t.style.fill = "var(--ink-3, #69665f)"; t.setAttribute("font-size", "9");
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
  h.appendChild($el("small", null, "Fala só com ele; trata do resto com os agentes."));
  const limpar = $el("button", "tt-icon-btn");
  limpar.type = "button";
  limpar.append(icon("trash"));
  limpar.title = "Limpar a conversa";
  limpar.setAttribute("aria-label", "Limpar a conversa");
  limpar.addEventListener("click", async () => {
    if (!confirm("Apagar toda a conversa? As preferências, as regras e as decisões dos agentes ficam.")) return;
    limpar.disabled = true;
    try {
      await api("/chat/clear", {});
      lista.replaceChildren();
      answered.clear();
    } catch (e) {
      render({ id: `err${Date.now()}`, ts: Date.now() / 1000, role: "coordenador", text: `Não consegui limpar: ${e.message}` });
    } finally {
      limpar.disabled = false;
      input.focus();
    }
  });
  topo.append(h, limpar);
  const lista = $el("div", "tt-chat-lista");
  lista.setAttribute("role", "log");
  lista.setAttribute("aria-live", "polite");
  lista.setAttribute("aria-label", "Conversa com o Coordenador");
  const sugestoes = $el("div", "tt-chat-sugestoes");
  sugestoes.setAttribute("aria-label", "Perguntas de exemplo");
  for (const ex of EXAMPLES) {
    const b = $el("button", null, ex);
    b.type = "button";
    b.addEventListener("click", () => send(ex));
    sugestoes.appendChild(b);
  }
  const form = $el("form", "tt-chat-form");
  const input = $el("input");
  input.name = "mensagem";
  input.autocomplete = "off";
  input.setAttribute("enterkeyhint", "send");
  input.placeholder = "Escreve ao Coordenador…";
  input.setAttribute("aria-label", "Mensagem para o Coordenador");
  const enviar = $el("button", "tt-send");
  enviar.append(icon("send"), $el("span", "tt-send-label", "Enviar"));
  enviar.type = "submit";
  form.append(input, enviar);
  container.append(topo, lista, ...(compact ? [] : [sugestoes]), form);

  let lastId = 0;
  const answered = new Set();  // decision / confirm ids already answered: their buttons are disabled
  let busy = false;

  // an answered card stops asking: its buttons go (the "Porquê?" stays), the eyebrow says what you chose
  function markAnswered(div, choice) {
    div.classList.add("respondido");
    div.querySelectorAll(".tt-botoes button:not(.porque)").forEach((b) => b.remove());
    const olho = div.querySelector(".tt-olho");
    if (olho && !olho.dataset.respondido) {
      olho.dataset.respondido = "1";
      olho.firstChild.textContent += choice ? ` · respondeste «${choice}»` : " · respondida";
    }
  }

  // messages that wait for the user are cards: [class, eyebrow, prefix to drop from the text]
  function cardOf(m) {
    if (m.role === "user") return null;
    if (m.decision_id) return ["pendente", "Proposta para aprovares", /^Proposta para aprovares:\s*/];
    if (m.confirm_id) return ["confirmar", "Confirmação", null];
    if (m.suggestion_id) return ["sugestao", "Sugestão", null];
    if (m.kind === "rule") return ["regra", "Regra tua", null];
    return null;
  }

  function render(m) {
    if (document.getElementById(`tt-msg-${m.id}`)) return;
    if (m.role === "user" && typeof m.id === "number") {
      // the server stores the message as soon as it arrives, so the poll can bring it back while the
      // answer is still on its way: it takes the place of the copy shown on sending, never a second bubble
      const pending = [...lista.querySelectorAll('[id^="tt-msg-tmp"]')].find((n) => n.dataset.text === m.text);
      if (pending) { pending.id = `tt-msg-${m.id}`; delete pending.dataset.text; return; }
    }
    const div = $el("div", `tt-msg ${m.role === "user" ? "user" : "coordenador"}`);
    div.id = `tt-msg-${m.id}`;
    if (String(m.id).startsWith("tmp")) div.dataset.text = m.text;
    const card = cardOf(m);
    const key = m.decision_id || m.confirm_id || m.suggestion_id || "";
    let text = m.text;
    if (card) {
      const [cls, eyebrow, prefix] = card;
      div.classList.add("cartao", cls);
      if (prefix) text = text.replace(prefix, "");
      const olho = $el("div", "tt-olho", eyebrow);
      olho.appendChild($el("span", "hora", hhmm(m.ts)));
      div.appendChild(olho);
      if (key && answered.has(key)) markAnswered(div);
    }
    const quem = $el("div", "quem", m.role === "user" ? "Tu" : "Coordenador");
    quem.appendChild($el("span", "hora", hhmm(m.ts)));
    div.append(quem, $el("div", "texto", text));
    if (m.image) {  // what the camera saw (the frame the vision model analysed)
      const img = document.createElement("img");
      img.className = "tt-imagem";
      img.alt = "Imagem da câmara analisada";
      img.src = m.image;
      img.loading = "lazy";
      div.appendChild(img);
    }
    const buttons = m.buttons || [];
    if (buttons.length || m.why || m.suggestion_id) {
      const row = $el("div", "tt-botoes");
      for (const b of buttons) {
        if (answered.has(key)) break;  // answered: no dead buttons
        const btn = $el("button", /Aprovar|Confirmar|Aceitar|Está bem/.test(b.label) ? "primario" : null, b.label);
        btn.type = "button";
        btn.addEventListener("click", () => press(b, row, key));
        row.appendChild(btn);
      }
      if (m.why || m.suggestion_id) {
        const why = $el("button", "porque", "Porquê?");
        why.type = "button";
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
        if ((ev.series || []).length > 1) box.appendChild(chartLegend(ev.series));
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
    const card = row.closest(".cartao");
    if (card) markAnswered(card, b.label);
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
