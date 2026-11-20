// Director panel of the scenario mode (only served with `python -m src.api --cenarios`).
// Buttons and shortcuts: 1–6 start a scenario from its initial state, Shift+1–6 continue into it (1 -> 6
// without resetting), A / F open / close the Sala door, C camera (2 people, 98 %), T Quarto plug on (7 W),
// H humidity history (3 nights), R reset everything, D show / hide this panel (when hidden, a small
// «Realizador» button in the same corner opens it again).

const $el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

async function act(body) {
  const res = await fetch("/cenarios/acao", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

const ACTIONS = [
  ["abrir_porta", "Abrir porta da Sala", "A"],
  ["fechar_porta", "Fechar porta da Sala", "F"],
  ["camara", "Câmara: 2 pessoas (98 %)", "C"],
  ["tomada", "Ligar tomada do Quarto (7 W)", "T"],
  ["historico", "Carregar histórico de humidade (3 noites)", "H"],
];

const box = $el("section", "realizador");
box.setAttribute("aria-label", "Painel de realizador (modo de cenários)");
let hidden = false;
try { hidden = localStorage.getItem("tt-realizador-oculto") === "1"; } catch { /* storage blocked */ }
box.hidden = hidden;

const topo = $el("div", "rz-topo");
const titulo = $el("strong", null, "Realizador");
const hora = $el("span", "rz-hora", "—");
const fechar = $el("button", "rz-fechar", "×");
fechar.type = "button";
fechar.title = "Esconder (tecla D)";
fechar.setAttribute("aria-label", "Esconder o painel de realizador");
topo.append(titulo, hora, fechar);

const lista = $el("div", "rz-cenarios");
const acoes = $el("div", "rz-acoes");
const repor = $el("button", "rz-perigo", "Repor tudo");
repor.type = "button";
repor.title = "Tecla R";
const nota = $el("div", "rz-nota", "1–6 começa o cenário · Shift+1–6 continua a partir do anterior · D esconde");
const erro = $el("div", "rz-erro");
erro.setAttribute("role", "alert");
box.append(topo, lista, acoes, repor, erro, nota);

// shown only while the panel is hidden, so the menu can be opened again with the mouse (not only with «D»)
const abrir = $el("button", "rz-abrir", "Realizador");
abrir.type = "button";
abrir.title = "Mostrar o painel de realizador (tecla D)";
abrir.append($el("kbd", null, "D"));
abrir.hidden = !hidden;
abrir.addEventListener("click", () => toggle(false));
document.body.append(box, abrir);

let busy = false;
async function run(body, { reload = false } = {}) {
  if (busy) return;
  busy = true;
  box.classList.add("ocupado");
  erro.textContent = "";
  try {
    const view = await act(body);
    render(view);
    if (reload) location.reload();  // the chat and the timeline start again from the new state
  } catch (e) {
    erro.textContent = e.message;
  } finally {
    busy = false;
    box.classList.remove("ocupado");
  }
}

for (const [acao, label, key] of ACTIONS) {
  const b = $el("button", null, label);
  b.type = "button";
  b.append($el("kbd", null, key));
  b.addEventListener("click", () => run({ acao }));
  acoes.appendChild(b);
}
const resetAll = () => {
  if (confirm("Repor tudo? Apaga a conversa, as regras, as preferências e o histórico da simulação (var/cenarios/). O var/home.db não é tocado.")) {
    run({ acao: "repor" }, { reload: true });
  }
};
repor.addEventListener("click", resetAll);
fechar.addEventListener("click", () => toggle(true));

let built = false;
function render(view) {
  hora.textContent = view.hora;
  if (!built) {
    for (const c of view.cenarios) {
      const row = $el("div", "rz-cenario");
      row.dataset.id = c.id;
      const start = $el("button", "rz-comecar");
      start.type = "button";
      start.title = `Começar do estado inicial · tecla ${c.id}`;
      start.append($el("b", null, c.id), $el("span", null, c.titulo), $el("small", null, `${c.slide} · ${c.inicio}`));
      start.addEventListener("click", () => run({ acao: "cenario", cenario: c.id }, { reload: true }));
      const cont = $el("button", "rz-continuar", "↪");
      cont.type = "button";
      cont.title = `Continuar a partir do cenário anterior, sem repor · Shift+${c.id}`;
      cont.setAttribute("aria-label", `Continuar para o cenário ${c.id} sem repor`);
      cont.addEventListener("click", () => run({ acao: "continuar", cenario: c.id }));
      row.append(start, cont);
      lista.appendChild(row);
    }
    built = true;
  }
  lista.querySelectorAll(".rz-cenario").forEach((r) => r.classList.toggle("ativo", r.dataset.id === view.ativo));
}

function toggle(hide = !box.hidden) {
  box.hidden = hide;
  abrir.hidden = !hide;
  (hide ? abrir : fechar).focus({ preventScroll: true });
  try { localStorage.setItem("tt-realizador-oculto", hide ? "1" : "0"); } catch { /* storage blocked */ }
}

window.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  const digit = /^Digit([1-6])$/.exec(e.code);
  if (digit) {
    e.preventDefault();
    if (e.shiftKey) run({ acao: "continuar", cenario: digit[1] });
    else run({ acao: "cenario", cenario: digit[1] }, { reload: true });
    return;
  }
  const key = e.key.toUpperCase();
  if (key === "D") { toggle(); return; }
  if (key === "R") { resetAll(); return; }
  const found = ACTIONS.find(([, , k]) => k === key);
  if (found) run({ acao: found[0] });
});

async function refresh() {
  try { render(await (await fetch("/cenarios")).json()); } catch { /* API restarting */ }
}
refresh();
setInterval(refresh, 1000);
