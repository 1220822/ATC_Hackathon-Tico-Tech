// Visão geral: the house in one screen. Rooms and energy every 3 s (/rooms, /home/energy); security,
// agents and the agents' activity live from /twin/stream (SSE), with polling as a fallback.
import { icon, ROLE } from "./icons.js";
import { $, el, nf0, nf1, ago, hhmm, areaColor, MODE_PT, renderShell, setConnection, statusPt, statusClass, plain } from "./shell.js";

const nf3 = new Intl.NumberFormat("pt-PT", { maximumFractionDigits: 3 });
const REFRESH_MS = 3000;
const SPARK_MS = 60000;
const QUIET = new Set(["temperature_changed", "humidity_changed", "plug_power_changed", "device_online", "tick"]);

let rooms = [];
let energy = null;
let twin = null;      // latest /twin/state
let pending = 0;
const activity = new Map();

$("today").textContent = new Date().toLocaleDateString("pt-PT", { weekday: "long", day: "numeric", month: "long" });
document.querySelector("[data-knowledge-open]")?.addEventListener("click", () => window.dispatchEvent(new CustomEvent("tt:knowledge")));

// ------------------------------------------------------------------ helpers
function setNum(node, text) {
  if (node.textContent === text) return;
  const had = node.textContent !== "—" && node.textContent !== "";
  node.textContent = text;
  if (had) { node.classList.remove("tick"); void node.offsetWidth; node.classList.add("tick"); }
}
const openKey = (s) => Object.keys(s).find((k) => k.endsWith("_aberta"));
const noun = (k) => (k ? k.replace("_aberta", "") : "janela");
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// sparkline: area + line + end dot, no axes (the value is printed next to it)
const NS = "http://www.w3.org/2000/svg";
function spark(points, color = "var(--ink)") {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 200 40");
  svg.setAttribute("preserveAspectRatio", "none");
  if (points.length < 2) return svg;
  const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 - y0 < 1e-6) { y0 -= 1; y1 += 1; }
  const X = (x) => ((x - x0) / Math.max(x1 - x0, 1)) * 196 + 2;
  const Y = (y) => 36 - ((y - y0) / (y1 - y0)) * 32;
  const line = points.map(([x, y]) => `${X(x).toFixed(1)},${Y(y).toFixed(1)}`).join(" ");
  const mk = (tag, a) => { const n = document.createElementNS(NS, tag); for (const k in a) n.setAttribute(k, a[k]); return n; };
  svg.append(mk("polygon", { points: `2,40 ${line} 198,40`, fill: color, "fill-opacity": ".08" }));
  svg.append(mk("polyline", { points: line, fill: "none", stroke: color, "stroke-width": "1.75", "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
  return svg;
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

// ------------------------------------------------------------------ headline: one sentence about the house
function render() {
  const head = $("headline");
  const alarm = twin?.alarm?.any;
  const devices = rooms.flatMap((r) => r.dispositivos);
  const off = devices.filter((d) => !d.online).length;
  let sentence;
  const openRooms = rooms.filter((r) => { const k = openKey(r.sensores); return k && r.sensores[k]; });
  const heating = rooms.some((r) => r.sensores.aquecedor_ligado);
  if (alarm) sentence = ["A casa está ", "em alarme", "."];
  else if (openRooms.length && heating) sentence = [`${cap(noun(openKey(openRooms[0].sensores)))} da ${openRooms[0].nome} `, "aberta", " com o aquecedor ligado."];
  else if (openRooms.length) sentence = [`${cap(noun(openKey(openRooms[0].sensores)))} da ${openRooms[0].nome} está `, "aberta", "."];
  else if (pending) sentence = ["Tens ", `${pending} ${pending === 1 ? "decisão" : "decisões"}`, " à tua espera."];
  else if (rooms.length) sentence = ["Está tudo em ordem em casa.", "", ""];
  if (sentence) {
    const [a, b, c] = sentence;
    head.replaceChildren(a, b ? el("em", null, b) : "", c);
  }
  const bits = [];
  if (rooms.length) bits.push(`${rooms.length} ${rooms.length === 1 ? "divisão" : "divisões"}`);
  if (devices.length) bits.push(`${devices.length} dispositivos${off ? `, ${off} sem ligação` : ", todos ligados"}`);
  const agents = twin?.agents || [];
  if (agents.length) bits.push(`${agents.filter((a) => a.online !== false).length} de ${agents.length} agentes disponíveis`);
  $("lede").textContent = bits.join(" · ");

  renderPins(openRooms);
  renderKpis();
  renderRooms();
}

// pins on the hero photograph: the window and the house's power, live
function renderPins(openRooms) {
  const win = openRooms[0] || rooms.find((r) => openKey(r.sensores));
  const pw = $("pin-window");
  if (win) {
    const k = openKey(win.sensores), open = !!win.sensores[k];
    $("pin-window-k").textContent = `${cap(noun(k))} da ${win.nome}`;
    setNum($("pin-window-v"), open ? "Aberta" : "Fechada");
    pw.classList.toggle("alert", open);
    pw.hidden = false;
  } else pw.hidden = true;
  if (energy) {
    setNum($("pin-power-v"), `${nf0.format(energy.potencia_total_w)} W`);
    $("pin-power").hidden = false;
  }
  requestAnimationFrame(guardPins);
}

// a pin never lies on the words, and never hangs off the hero: it steps aside
function guardPins() {
  const hero = document.querySelector(".hero").getBoundingClientRect();
  const blocks = [".hero-copy"].map((q) => document.querySelector(q).getBoundingClientRect());
  for (const pin of document.querySelectorAll(".pin")) {
    if (pin.hidden) continue;
    pin.style.visibility = "";
    const r = pin.getBoundingClientRect();
    if (!r.width) continue;  // hidden by CSS at this width
    const hit = (b) => r.left < b.right + 12 && r.right > b.left - 12 && r.top < b.bottom + 12 && r.bottom > b.top - 12;
    const out = r.top < hero.top + 8 || r.left < hero.left + 8 || r.right > hero.right - 8;
    if (out || blocks.some(hit)) pin.style.visibility = "hidden";
  }
}
window.addEventListener("resize", () => requestAnimationFrame(guardPins));

// one count on the page: the power figure counts up once on the first reading (1 s), then just updates
let counted = false;
function countUp(node, to, fmt) {
  if (counted || matchMedia("(prefers-reduced-motion: reduce)").matches || !to) { counted = true; setNum(node, fmt(to)); return; }
  counted = true;
  const t0 = performance.now(), ms = 1000;
  const step = (t) => {
    const k = Math.min((t - t0) / ms, 1), e = 1 - Math.pow(1 - k, 3);
    node.textContent = fmt(Math.round(to * e));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderKpis() {
  if (energy) {
    if (!counted) countUp($("k-power"), energy.potencia_total_w, (v) => nf0.format(v));
    else setNum($("k-power"), nf0.format(energy.potencia_total_w));
    setNum($("k-yday"), energy.consumo_ontem_kwh == null ? "—" : nf3.format(energy.consumo_ontem_kwh));
    $("k-yday-foot").textContent = energy.consumo_ontem_kwh != null && energy.tarifa
      ? `≈ ${(energy.consumo_ontem_kwh * energy.tarifa).toLocaleString("pt-PT", { style: "currency", currency: energy.moeda || "EUR" })} à tarifa da casa`
      : "medido pela casa";
  }
  // temperature: the mean, and how far it sits from the mean target (the comparison has a name)
  const temps = rooms.filter((r) => r.sensores.temperatura_c != null);
  if (temps.length) {
    const avg = temps.reduce((s, r) => s + r.sensores.temperatura_c, 0) / temps.length;
    setNum($("k-temp"), nf1.format(avg));
    const withTarget = temps.filter((r) => r.setpoint_c != null);
    const foot = $("k-temp-foot");
    if (withTarget.length) {
      const d = withTarget.reduce((s, r) => s + r.sensores.temperatura_c - r.setpoint_c, 0) / withTarget.length;
      const near = Math.abs(d) < 0.5;
      const txt = near ? "no alvo" : `${nf1.format(Math.abs(d))} °C ${d > 0 ? "acima" : "abaixo"} do alvo`;
      const delta = el("span", "delta", txt);
      delta.style.setProperty("--d", near ? "var(--ok)" : d > 0 ? "var(--signal)" : "var(--a-conforto)");
      foot.replaceChildren(delta);
      foot.title = withTarget.map((r) => `${r.nome}: ${nf1.format(r.sensores.temperatura_c)} °C, alvo ${nf1.format(r.setpoint_c)} °C`).join("\n");
    } else foot.textContent = `${temps.length} ${temps.length === 1 ? "divisão" : "divisões"} com sensor`;
  }
  setNum($("k-pending"), String(pending));
  $("k-pending-unit").textContent = pending === 1 ? "decisão" : "decisões";
  const link = $("k-pending-foot");
  link.textContent = pending ? "Responder no chat →" : "Nada à espera de ti";
  link.setAttribute("aria-label", pending ? "Responder no chat da casa 3D" : "Abrir a casa 3D");
  $("k-pending-box").classList.toggle("attention", pending > 0);
  $("now-ts").textContent = `atualizado às ${new Date().toLocaleTimeString("pt-PT")}`;
}

// the power chart: one series, from zero (a magnitude), an hour tick per full hour, the current point
// in the signal colour, crosshair + tooltip on hover (dataviz: a plotted series answers the pointer)
let powerPts = [];
function powerChart(points = powerPts) {
  powerPts = points;
  const box = $("k-power-spark");
  if (points.length < 2) {
    box.replaceChildren(el("div", "empty-chart", "Ainda sem histórico de potência suficiente."));
    $("k-power-foot").textContent = "O gráfico aparece quando houver pelo menos duas leituras.";
    return;
  }
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, at) => { const n = document.createElementNS(NS, tag); for (const k in at) n.setAttribute(k, at[k]); return n; };
  const x0 = points[0][0], x1 = points[points.length - 1][0];
  const peak = points.reduce((a, b) => (b[1] > a[1] ? b : a));
  const top = Math.max(peak[1] * 1.15, 10);
  const fx = (x) => (x - x0) / Math.max(x1 - x0, 1);       // 0..1
  const fy = (y) => 1 - y / top;                             // 0..1, from zero
  const svg = mk("svg", { viewBox: "0 0 100 100", preserveAspectRatio: "none", "aria-hidden": "true" });
  for (const g of [0, .5, 1]) {  // recessive grid: zero, half, top
    svg.append(mk("line", { x1: 0, x2: 100, y1: fy(top * g) * 100, y2: fy(top * g) * 100, stroke: g === 0 ? "var(--ink)" : "var(--rule)",
      "stroke-width": 1, "vector-effect": "non-scaling-stroke", ...(g === 0 ? { "stroke-opacity": ".5" } : {}) }));
  }
  const line = points.map(([x, y]) => `${(fx(x) * 100).toFixed(2)},${(fy(y) * 100).toFixed(2)}`).join(" ");
  svg.append(mk("polygon", { points: `0,100 ${line} 100,100`, fill: "var(--signal)", "fill-opacity": ".09" }));
  svg.append(mk("polyline", { points: line, fill: "none", stroke: "var(--signal)", "stroke-width": 2, "stroke-linejoin": "round",
    "vector-effect": "non-scaling-stroke" }));
  const parts = [svg];
  const yl = (v) => { const n = el("span", "y", `${nf0.format(v)} W`); n.style.top = `${fy(v) * 100}%`; return n; };
  parts.push(yl(Math.round(top / 2)), yl(Math.round(top)));
  // an hour label per full hour (at most 7), the first one at the left edge
  const start = Math.ceil(x0 / 3600) * 3600;
  for (let t = start; t <= x1; t += 3600) {
    const n = el("span", "x", hhmm(t).replace(":00", " h"));
    n.style.left = `${fx(t) * 100}%`;
    parts.push(n);
  }
  const last = points[points.length - 1];
  const dot = el("span", "now-dot");
  dot.style.left = `${fx(last[0]) * 100}%`;
  dot.style.top = `${fy(last[1]) * 100}%`;
  const cross = el("span", "cross"); cross.hidden = true;
  const tip = el("span", "tip"); tip.hidden = true;
  parts.push(dot, cross, tip);
  box.replaceChildren(...parts);
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", `Potência da casa nas últimas 6 horas: pico de ${nf0.format(peak[1])} W às ${hhmm(peak[0])}, agora ${nf0.format(last[1])} W.`);
  $("k-power-foot").replaceChildren("Nas últimas 6 horas, o pico foi de ", el("b", null, `${nf0.format(peak[1])} W`), ` às ${hhmm(peak[0])}.`);
  box.onpointermove = (e) => {
    const r = box.getBoundingClientRect();
    const t = x0 + ((e.clientX - r.left) / r.width) * (x1 - x0);
    const p = points.reduce((a, b) => (Math.abs(b[0] - t) < Math.abs(a[0] - t) ? b : a));
    const x = fx(p[0]) * r.width;
    cross.hidden = tip.hidden = false;
    cross.style.left = `${x}px`;
    tip.style.left = `${Math.min(Math.max(x, 48), r.width - 48)}px`;
    tip.textContent = `${hhmm(p[0])} · ${nf0.format(p[1])} W`;
  };
  box.onpointerleave = () => { cross.hidden = tip.hidden = true; };
}

// ------------------------------------------------------------------ rooms: a ledger
// One <tr> per room, built once and then updated in place (so the trend's hover survives the 3 s
// refresh). Every column is the same reading in every room: a room without it shows "—".
const roomSparks = {};
const ledger = new Map();  // room id -> { tr, cells }
const COLS = ["name", "temp", "hum", "win", "heat", "light", "trend"];
const LABELS = { temp: "Temperatura", hum: "Humidade", win: "Janela", heat: "Aquecedor", light: "Luz", trend: "Últimas 12 h" };
const none = () => el("span", "none", "—");
// replaceChildren turns null into the text "null": skip empty parts
const put = (node, ...parts) => node.replaceChildren(...parts.filter((x) => x != null && x !== false));

function renderRooms() {
  if (!rooms.length) return;
  const body = $("rooms");
  body.querySelector(".ledger-empty")?.remove();
  for (const r of rooms) {
    let row = ledger.get(r.id);
    if (!row) {
      const tr = el("tr", "room-row");
      const cells = {};
      for (const c of COLS) {
        const cell = el(c === "name" ? "th" : "td", `c-${c}`);
        if (c === "name") cell.scope = "row";
        else cell.dataset.label = LABELS[c];
        cells[c] = cell;
        tr.append(cell);
      }
      row = { tr, cells };
      ledger.set(r.id, row);
      body.append(tr);
    }
    fillRow(row, r);
  }
}

function fillRow({ tr, cells }, r) {
  const s = r.sensores;
  const k = openKey(s), open = !!(k && s[k]);
  tr.classList.toggle("is-open", open);

  // name + one line of state: devices, simulated
  const ok = r.dispositivos.filter((d) => d.online).length, total = r.dispositivos.length;
  const off = r.dispositivos.filter((d) => !d.online).map((d) => ROLE[d.papel]?.name || d.nome);
  const state = el("span", "room-state " + (ok === total ? "ok" : ok === 0 ? "bad" : "warn"),
    ok === total ? `${total} ${total === 1 ? "dispositivo ligado" : "dispositivos, todos ligados"}`
      : ok === 0 ? "nenhum dispositivo ligado" : `${off.join(", ")} sem ligação`);
  state.title = r.dispositivos.map((d) => `${ROLE[d.papel]?.name || d.nome}: ${d.online ? "ligado" : `sem ligação, ${ago(d.ultima_comunicacao)}`}`).join("\n");
  put(cells.name, el("span", "room-name", r.nome), state,
    r.simulado ? $("t-sim").content.firstElementChild.cloneNode(true) : null);

  // temperature: the figure, and where it sits against the target (±4 °C)
  if (s.temperatura_c != null) {
    const fig = el("span", "t-fig", el("span", "t-v", nf1.format(s.temperatura_c)), el("span", "t-u", "°C"));
    const parts = [fig];
    if (r.setpoint_c != null) {
      const lo = r.setpoint_c - 4, hi = r.setpoint_c + 4;
      const pct = (v) => `${Math.min(Math.max((v - lo) / (hi - lo), 0), 1) * 100}%`;
      const d = s.temperatura_c - r.setpoint_c;
      const scale = el("span", "t-scale", el("span", "t-track"));
      const tgt = el("span", "t-target"); tgt.style.left = pct(r.setpoint_c);
      const now = el("span", "t-now"); now.style.left = pct(s.temperatura_c);
      now.style.setProperty("--d", Math.abs(d) < 0.5 ? "var(--ok)" : d > 0 ? "var(--signal)" : "var(--a-conforto)");
      scale.append(tgt, now);
      scale.setAttribute("aria-hidden", "true");
      const cap = Math.abs(d) < 0.5 ? "no alvo" : `${nf1.format(Math.abs(d))} °C ${d > 0 ? "acima" : "abaixo"} do alvo`;
      parts.push(scale, el("span", "t-cap", cap, el("span", "t-tgt", ` de ${nf1.format(r.setpoint_c)} °C`)));
    }
    put(cells.temp, ...parts);
  } else put(cells.temp, none(), el("span", "t-cap", "sem sensor"));

  put(cells.hum, s.humidade_pct != null ? el("span", "v", `${nf0.format(s.humidade_pct)} %`) : none());
  cells.hum.classList.add("n");

  if (k) {
    const w = el("span", "v state" + (open ? " open" : ""), open ? "Aberta" : "Fechada");
    if (noun(k) === "porta") cells.win.dataset.label = "Porta";
    put(cells.win, w);
  } else put(cells.win, none());

  if ("aquecedor_ligado" in s) {
    put(cells.heat, el("span", "v state" + (s.aquecedor_ligado ? " on" : ""), s.aquecedor_ligado ? "Ligado" : "Desligado"),
      s.aquecedor_potencia_w != null ? el("span", "sub", `${nf0.format(s.aquecedor_potencia_w)} W`) : null);
  } else put(cells.heat, none());

  if (s.luz) {
    const on = s.luz.power === "on";
    const sw = el("span", "swatch");
    sw.setAttribute("aria-hidden", "true");
    if (on && s.luz.cor) sw.style.background = `rgb(${s.luz.cor.join(",")})`;
    put(cells.light, el("span", "v state" + (on ? " lit" : ""), on ? sw : null, on ? "Ligada" : "Desligada"),
      on && s.luz.brilho != null ? el("span", "sub", `${s.luz.brilho} %`) : null);
  } else put(cells.light, none());

  if (!cells.trend.dataset.drawn) drawTrend(cells.trend, r);
  // a reading this room does not have: "—" in the table, left out of the phone's block
  for (const c of COLS.slice(1)) cells[c].classList.toggle("is-none", cells[c].children.length === 1 && !!cells[c].querySelector(".none"));
}

// the room's temperature over 12 h: one line, the current point, crosshair + tooltip on hover
function drawTrend(cell, r) {
  const pts = roomSparks[r.id];
  if (!pts || pts.length < 2) { cell.replaceChildren(r.sensores.temperatura_c == null ? none() : el("span", "sub", "sem histórico")); return; }
  cell.dataset.drawn = "1";
  const ys = pts.map((p) => p[1]);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const box = el("span", "trend");
  box.append(spark(pts, "var(--a-conforto)"));
  const x0 = pts[0][0], x1 = pts[pts.length - 1][0];
  let y0 = lo, y1 = hi; if (y1 - y0 < 1e-6) { y0 -= 1; y1 += 1; }
  const fx = (x) => (((x - x0) / Math.max(x1 - x0, 1)) * 196 + 2) / 200, fy = (y) => (36 - ((y - y0) / (y1 - y0)) * 32) / 40;
  const last = pts[pts.length - 1];
  const dot = el("span", "trend-dot"); dot.style.left = `${fx(last[0]) * 100}%`; dot.style.top = `${fy(last[1]) * 100}%`;
  const tip = el("span", "tip"); tip.hidden = true;
  box.append(dot, tip);
  box.setAttribute("role", "img");
  box.setAttribute("aria-label", `${r.nome}, últimas 12 horas: entre ${nf1.format(lo)} e ${nf1.format(hi)} °C`);
  box.onpointermove = (e) => {
    const b = box.getBoundingClientRect();
    const t = x0 + ((e.clientX - b.left) / b.width) * (x1 - x0);
    const p = pts.reduce((a, c) => (Math.abs(c[0] - t) < Math.abs(a[0] - t) ? c : a));
    tip.hidden = false;
    tip.style.left = `${fx(p[0]) * 100}%`;
    tip.textContent = `${hhmm(p[0])} · ${nf1.format(p[1])} °C`;
  };
  box.onpointerleave = () => { tip.hidden = true; };
  cell.replaceChildren(box, el("span", "sub", `${nf1.format(lo)}–${nf1.format(hi)} °C`));
  cell.classList.remove("is-none");
}

// ------------------------------------------------------------------ activity (events + decisions)
// the team: one line per agent (what it looks after, its live state, its recent decisions); each one
// filters the feed, and the filter lives in the URL (?agente=Energia) so it survives a reload
const ROLES = {
  Coordenador: "Fala contigo e decide. É o único que mexe na casa.",
  "Segurança": "Portas, janelas, fumo e alarme.",
  Energia: "Consumo, tomadas e o aquecedor.",
  Conforto: "Temperatura e humidade de cada divisão.",
};
const ORDER = ["Coordenador", "Segurança", "Energia", "Conforto"];
let agentFilter = new URLSearchParams(location.search).get("agente") || "Todos";
const whoOf = (it) => (it.kind === "decision" ? it.agent : it.area === "Emergência" ? "Segurança" : it.area);

function renderTeam() {
  const agents = new Map((twin?.agents || []).map((a) => [a.name, a]));
  const counts = {};
  for (const it of activity.values()) if (it.kind === "decision") counts[it.agent] = (counts[it.agent] || 0) + 1;
  const box = $("team");
  box.replaceChildren(...ORDER.map((name) => {
    const a = agents.get(name);
    const state = !a ? ["idle", "sem dados"] : a.busy ? ["busy", "a pensar…"] : a.online === true
      ? ["ok", a.last_latency_s != null ? `pronto · ${nf1.format(a.last_latency_s)} s` : "pronto"]
      : a.online === false ? ["bad", "indisponível"] : ["idle", "à espera de um evento"];
    const id = el("span", "agent-id" + (name === "Coordenador" ? " coord" : ""));
    id.style.setProperty("--area", areaColor(name));
    const n = counts[name] || 0;
    const b = el("button", "member",
      el("span", "member-name", id, name, el("span", `member-state s-${state[0]}`, state[1])),
      el("span", "member-role", ROLES[name]),
      el("span", "member-count", n ? `${n} ${n === 1 ? "decisão recente" : "decisões recentes"}` : "sem decisões recentes"));
    b.type = "button";
    b.dataset.agent = name;
    if (a?.last_error) b.title = `Último erro: ${a.last_error}`;
    return b;
  }));
  document.querySelectorAll(".member").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.agent === agentFilter)));
}
document.querySelector(".team").addEventListener("click", (e) => {
  const b = e.target.closest(".member");
  if (!b) return;
  agentFilter = b.dataset.agent;
  const url = new URL(location.href);
  if (agentFilter === "Todos") url.searchParams.delete("agente"); else url.searchParams.set("agente", agentFilter);
  history.replaceState(null, "", url);
  renderTeam();
  renderFeed();
});

function addActivity(items, fresh) {
  for (const it of items) {
    if (activity.has(it.id) || (it.kind !== "decision" && QUIET.has(it.type))) continue;
    activity.set(it.id, { ...it, fresh });
  }
  renderTeam();
  renderFeed();
}

function renderFeed() {
  const all = [...activity.values()].sort((a, b) => b.ts - a.ts);
  const list = all.filter((it) => agentFilter === "Todos" || whoOf(it) === agentFilter).slice(0, 8);
  $("feed-empty").hidden = list.length > 0;
  if (list.length === 0 && all.length) $("feed-empty").textContent = `${agentFilter} ainda não fez nada que valha a pena mostrar.`;
  $("activity").replaceChildren(...list.map((it) => {
    const decision = it.kind === "decision";
    const who = whoOf(it);
    const id = el("span", "agent-id" + (who === "Coordenador" ? " coord" : ""));
    id.style.setProperty("--area", areaColor(who));
    const time = el("time", null, hhmm(it.ts));
    time.dateTime = new Date(it.ts * 1000).toISOString();
    const text = plain(it.message || "");
    const what = el("p", "f-what", text);
    if (text !== it.message) what.title = it.message;
    const meta = el("p", "f-meta", el("span", "f-who", id, who), it.room_name ? ` · ${it.room_name}` : null,
      decision ? " · decisão" : null);
    const pendingLink = decision && it.status === "proposta" ? el("a", "f-act link", "Responder no chat") : null;
    if (pendingLink) pendingLink.href = "/twin";
    const li = el("li", ["f-item", decision ? "is-decision" : "", it.severity === "critical" ? "critical" : "", it.fresh ? "new" : ""].filter(Boolean).join(" "),
      time, el("div", "f-body", meta, what, pendingLink),
      decision ? el("span", "mark " + statusClass(it.status), statusPt(it.status)) : el("span", "f-kind", "evento"));
    it.fresh = false;
    return li;
  }));
}

// ------------------------------------------------------------------ data
async function refresh() {
  try {
    [rooms, energy] = await Promise.all([getJSON("/rooms"), getJSON("/home/energy")]);
    $("erro").hidden = true;
  } catch (err) {
    $("erro").hidden = false;
    $("erro").textContent = `Sem dados da casa: ${err.message}`;
  }
  render();
}

async function refreshPending() {
  try {
    const p = await getJSON("/chat/pending");
    pending = (p.decisions || []).length + (p.confirm ? 1 : 0);
  } catch { /* chat off */ }
}

async function refreshSparks() {
  const now = Date.now() / 1000;
  try {
    const per = await Promise.all((energy?.divisoes || []).map((d) =>
      getJSON(`/historic/aggregated/avg/rooms/${encodeURIComponent(d.id)}/power_w?period_s=600&start=${now - 6 * 3600}&end=${now + 1}`)
        .then((x) => x.buckets).catch(() => [])));
    const sum = new Map();
    for (const rows of per) for (const b of rows) sum.set(b.ts, (sum.get(b.ts) || 0) + b.value);
    const pts = [...sum.entries()].sort((a, b) => a[0] - b[0]);
    powerChart(pts);
  } catch { /* no history */ }
  await Promise.all(rooms.filter((r) => r.sensores.temperatura_c != null).map(async (r) => {
    try {
      const x = await getJSON(`/historic/aggregated/avg/rooms/${encodeURIComponent(r.id)}/temperature?period_s=900&start=${now - 12 * 3600}&end=${now + 1}`);
      roomSparks[r.id] = x.buckets.map((b) => [b.ts, b.value]);
      const row = ledger.get(r.id);
      if (row) { delete row.cells.trend.dataset.drawn; drawTrend(row.cells.trend, r); }
    } catch { /* no history */ }
  }));
  renderRooms();
}

function onTwin(st) {
  twin = st;
  renderShell(st);
  render();
  renderTeam();
}

function connect() {
  let last = 0;
  const poll = async () => {
    try {
      const st = await getJSON("/twin/state");
      onTwin(st);
      addActivity([...st.events, ...st.decisions], false);
      setConnection(true, `atualizado ${hhmm(st.ts)}`);
    } catch (err) { setConnection(false, "sem ligação"); }
  };
  if (!("EventSource" in window)) { setInterval(poll, REFRESH_MS); poll(); return; }
  const es = new EventSource("/twin/stream");
  const touch = () => { last = Date.now(); };
  es.addEventListener("snapshot", (e) => {
    touch();
    const st = JSON.parse(e.data);
    onTwin(st);
    addActivity([...(st.events || []), ...(st.decisions || [])], false);
    setConnection(true, "em direto");
  });
  es.addEventListener("state", (e) => { touch(); const st = JSON.parse(e.data); onTwin(st); setConnection(true, `em direto · ${hhmm(st.ts)}`); });
  es.addEventListener("event", (e) => { touch(); addActivity([JSON.parse(e.data)], true); });
  es.addEventListener("decision", (e) => { touch(); addActivity([JSON.parse(e.data)], true); refreshPending(); });
  es.onerror = () => setConnection(false, "a religar…");
  setInterval(() => { if (Date.now() - last > 20000) poll(); }, 3000);
}

await refresh();
refreshPending();
refreshSparks();
connect();
setInterval(refresh, REFRESH_MS);
setInterval(refreshPending, 5000);
setInterval(refreshSparks, SPARK_MS);
