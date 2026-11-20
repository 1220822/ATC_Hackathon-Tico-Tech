// The shell shared by every page: brand, navigation and the state of the house (security, mode,
// agents, connection, alarm band). Pages feed it the /twin/state object with renderShell(st).
import { hydrateIcons, icon } from "./icons.js";

export const nf0 = new Intl.NumberFormat("pt-PT", { maximumFractionDigits: 0 });
export const nf1 = new Intl.NumberFormat("pt-PT", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
export const MODE_PT = { DISARMED: "Desarmada", STAY: "Armada em casa", AWAY: "Armada ausente" };
export const AREA_VAR = {
  "Segurança": "--a-seguranca", "Energia": "--a-energia", "Conforto": "--a-conforto", "Emergência": "--a-emergencia",
  "Coordenador": "--a-coordenador", "Sistema": "--a-sistema",
};
export const areaColor = (area) => `var(${AREA_VAR[area] || "--a-sistema"})`;
export const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("pt-PT", { hour: "2-digit", minute: "2-digit" });
export const hhmmss = (ts) => new Date(ts * 1000).toLocaleTimeString("pt-PT");
export function ago(ts) {
  if (!ts) return "nunca";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "há instantes";
  if (s < 3600) return `há ${Math.round(s / 60)} min`;
  if (s < 86400) return `há ${Math.round(s / 3600)} h`;
  return `há ${Math.round(s / 86400)} d`;
}
export const $ = (id) => document.getElementById(id);
export function el(tag, cls, ...children) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const c of children) if (c != null && c !== false) e.append(c);  // strings become text, never HTML
  return e;
}

const STATUS_PT = {
  proposta: "à espera", executada: "feito", aprovada: "aprovada", rejeitada: "rejeitada", erro: "erro",
  "dry-run": "só registado", "informação": "informação", vetoed: "vetada",
};
export const statusPt = (s) => STATUS_PT[s] || s;
// LLM failures arrive as raw exception text: say it in words, keep the original in the title
export function plain(msg) {
  if (/timed out|Timeout/i.test(msg)) return "O modelo de linguagem não respondeu a tempo; a casa seguiu sem esta opinião.";
  if (/^Resposta inválida do agente/i.test(msg)) return "A resposta do modelo veio num formato que não percebi; foi ignorada.";
  if (/tempo limite excedido na infer[eê]ncia/i.test(msg)) return msg.replace(/:\s*tempo limite excedido na infer[eê]ncia\.?/i, ": o modelo de visão não respondeu a tempo.");
  if (/falha na captura do frame/i.test(msg)) return msg.replace(/:\s*falha na captura do frame\.?/i, ": não consegui obter imagem da câmara.");
  return msg;
}

export function statusClass(s) {
  if (s === "proposta") return "warn";
  if (s === "executada" || s === "aprovada") return "ok";
  if (s === "erro" || s === "vetoed") return "bad";
  return "idle";
}

hydrateIcons();
document.querySelectorAll("[data-knowledge]").forEach((b) => b.addEventListener("click", () => {
  closeMenu();
  window.dispatchEvent(new CustomEvent("tt:knowledge"));
}));

let connText = "a ligar…", connOk = false;
export function setConnection(ok, text) {
  connOk = ok; connText = text;
  const c = $("conn");
  if (!c) return;
  c.className = "conn " + (ok ? "ok" : "erro");
  $("conn-text").textContent = text;
  c.title = `Ligação à casa: ${text}`;
  renderMenuState();
}

// ------------------------------------------------------------------ the state line
let last = null;
let alarmSince = null;
// the alarm band's real height (it wraps on a phone) as --band-h, so a first-screen hero ends on the fold
function measureBand() {
  const band = $("alarm");
  const h = band && !band.hidden ? band.offsetHeight : 0;
  document.body.style.setProperty("--band-h", `${h}px`);
}
window.addEventListener("resize", measureBand);

function agentsSummary(st) {
  const agents = st.agents || [];
  const up = agents.filter((a) => a.online !== false).length;
  const busy = agents.some((a) => a.busy);
  const state = (a) => (a.busy ? "a pensar" : a.online === true ? "pronto" : a.online === false ? "indisponível" : "em espera");
  return { agents, up, busy, cls: up === agents.length ? "ok" : up === 0 ? "bad" : "warn",
           detail: agents.map((a) => `${a.name}: ${state(a)}`).join("\n") };
}
const secText = (st) => (st.alarm?.any ? "Em alarme" : (MODE_PT[st.security.status] || st.security.status));
const secClass = (st) => (st.alarm?.any ? "bad" : st.security.armed ? "warn" : "ok");
const modeText = (st) => [st.mode === "simulado" ? "Casa simulada" : null, st.dry_run ? "modo de ensaio" : null].filter(Boolean).join(", ");

export function renderShell(st) {
  last = st;
  const homeEl = $("home-name");
  if (st.home_name && homeEl) homeEl.textContent = st.home_name;
  const sec = $("sec");
  sec.className = "st " + secClass(st);
  sec.textContent = secText(st);
  sec.title = `Segurança da casa: ${secText(st)}`;

  const mode = $("mode");
  const m = modeText(st);
  mode.hidden = !m;
  mode.textContent = m ? m.charAt(0).toUpperCase() + m.slice(1) : "";
  mode.title = st.dry_run ? "Dry-run: as decisões ficam registadas, nada é enviado à casa." : "";

  const ag = agentsSummary(st);
  const link = $("agents");
  if (ag.agents.length) {
    link.className = "st st-agents " + ag.cls;
    link.replaceChildren("Agentes ", el("b", null, `${ag.up}/${ag.agents.length}`), ag.busy ? " · a pensar" : "");
    link.title = ag.detail;
    link.setAttribute("aria-label", `Agentes disponíveis: ${ag.up} de ${ag.agents.length}. ${ag.detail.replaceAll("\n", ", ")}`);
  }
  const btn = $("menu-btn");
  btn.className = "menu-btn " + (st.alarm?.any ? "bad" : ag.cls === "ok" ? "" : "warn");
  renderMenuState();

  const band = $("alarm");
  band.hidden = !st.alarm?.any;
  measureBand();
  if (!st.alarm?.any) { alarmSince = null; return; }
  alarmSince ??= st.ts;  // the house's time (the server's clock), not the browser's
  const alarm = st.alarm;
  const room = alarm.smoke_room && st.rooms?.find((r) => r.id === alarm.smoke_room);
  $("alarm-title").textContent = alarm.smoke ? `Fumo detetado${room ? ` na ${room.name}` : ""}`
    : alarm.intrusion ? "Possível intrusão, a casa está em alarme" : alarm.hub ? "A central de segurança está em alarme"
    : "A casa está em alarme";
  $("alarm-sub").textContent = `desde as ${hhmm(alarmSince)} · se foste tu, desliga aqui`;
}

// ------------------------------------------------------------------ the phone menu
function renderMenuState() {
  const box = $("menu-state");
  if (!box || !last) return;
  const ag = agentsSummary(last);
  const rows = [["Segurança", secText(last)], ["Agentes", `${ag.up} de ${ag.agents.length} disponíveis`],
    ...(modeText(last) ? [["Modo", modeText(last)]] : []), ["Ligação", connText]];
  if (last.home_name) rows.unshift(["Casa", last.home_name]);
  box.replaceChildren(...rows.flatMap(([k, v]) => [el("dt", null, k), el("dd", null, v)]));
}
function openMenu() {
  $("menu").hidden = false;
  $("menu-btn").setAttribute("aria-expanded", "true");
  $("menu-btn").textContent = "Fechar";
  document.body.classList.add("menu-open");
  $("menu").querySelector("a, button")?.focus();
}
function closeMenu() {
  const m = $("menu");
  if (!m || m.hidden) return;
  m.hidden = true;
  $("menu-btn").setAttribute("aria-expanded", "false");
  $("menu-btn").textContent = "Menu";
  document.body.classList.remove("menu-open");
  $("menu-btn").focus();
}
$("menu-btn")?.addEventListener("click", () => ($("menu").hidden ? openMenu() : closeMenu()));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
matchMedia("(min-width: 681px)").addEventListener("change", (e) => { if (e.matches) closeMenu(); });

$("alarm-off")?.addEventListener("click", async () => {
  // the chat confirms the disarm; on pages without the chat panel, the message goes straight to /chat
  if (document.querySelector(".tt-chat")) {
    window.dispatchEvent(new CustomEvent("tt:chat-send", { detail: "Foi falso alarme, desliga o alarme" }));
  } else {
    location.href = "/twin?alarme=1";
  }
});
