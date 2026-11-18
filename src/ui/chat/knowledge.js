// "O que a casa sabe de ti": everything the house knows about the user, with edit / revoke / erase.
// Opened by the chat header button (window event "tt:knowledge").

const $el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status}`);
  return data;
}

const EDIT_HINT = {
  setpoint_c: "Nova temperatura em °C (ex.: 21):", comfort_range_c: "Novo intervalo em °C (ex.: 19-22):",
  humidity_max_pct: "Nova humidade máxima em % (ex.: 65):", autonomy: "Nível: auto, perguntar ou nunca",
  quiet_hours: "Horas sem sugestões (ex.: 23:00-07:30):", note: "Texto da nota:",
};

function item(p, { onEdit, onRevoke, revokeLabel = "Revogar", extra } = {}) {
  const row = $el("div", `item${p.ativa ? "" : " inativo"}`);
  const body = $el("div");
  body.appendChild($el("div", null, p.descricao));
  const meta = [p.ambito, p.origem, p.data].filter(Boolean).join(" · ");
  body.appendChild($el("div", "meta", meta + (extra ? ` · ${extra}` : "")));
  if (p.frase) body.appendChild($el("div", "frase", `«${p.frase}»`));
  row.appendChild(body);
  if (onEdit && p.editavel && p.ativa) {
    const b = $el("button", null, "Editar");
    b.addEventListener("click", () => onEdit(p));
    row.appendChild(b);
  }
  if (onRevoke && p.ativa) {
    const b = $el("button", null, revokeLabel);
    b.addEventListener("click", () => onRevoke(p));
    row.appendChild(b);
  }
  return row;
}

function section(corpo, title, rows, empty) {
  corpo.appendChild($el("h3", null, title));
  if (!rows.length) corpo.appendChild($el("div", "vazio", empty));
  rows.forEach((r) => corpo.appendChild(r));
}

async function open() {
  document.querySelector(".tt-modal-fundo")?.remove();
  const fundo = $el("div", "tt-modal-fundo");
  const modal = $el("div", "tt-modal");
  modal.setAttribute("role", "dialog");
  modal.setAttribute("aria-label", "O que a casa sabe de ti");
  const header = $el("header");
  header.appendChild($el("h2", null, "O que a casa sabe de ti"));
  const apagar = $el("button", "perigo", "Apagar tudo");
  const fechar = $el("button", null, "Fechar");
  header.append(apagar, fechar);
  const corpo = $el("div", "corpo");
  modal.append(header, corpo);
  fundo.appendChild(modal);
  document.body.appendChild(fundo);
  const close = () => fundo.remove();
  fechar.addEventListener("click", close);
  fundo.addEventListener("click", (e) => { if (e.target === fundo) close(); });
  document.addEventListener("keydown", function esc(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); } });

  async function render() {
    corpo.innerHTML = "";
    let k;
    try {
      k = await api("/knowledge");
    } catch (e) {
      corpo.appendChild($el("div", "vazio", `Não consegui ler: ${e.message}`));
      return;
    }
    const onEdit = async (p) => {
      const text = window.prompt(EDIT_HINT[p.tipo] || "Novo valor:");
      if (!text) return;
      try { await api(`/knowledge/prefs/${p.id}/edit`, { text }); } catch (e) { alert(e.message); }
      render();
    };
    const onRevoke = async (p) => {
      if (!confirm(`Revogar: ${p.descricao}?`)) return;
      try { await api(`/knowledge/prefs/${p.id}/revoke`, {}); } catch (e) { alert(e.message); }
      render();
    };
    corpo.appendChild($el("p", "rgpd", k.nota));
    section(corpo, "Preferências ativas", k.preferencias.map((p) => item(p, { onEdit, onRevoke })),
      "Ainda não guardei preferências tuas.");
    section(corpo, "Regras", k.regras.map((p) => item(p, { onRevoke, revokeLabel: "Revogar (desativa o cenário)", extra: p.onde })),
      "Sem regras.");
    section(corpo, "Sugestões silenciadas", k.silenciadas.map((p) => item(p, { onRevoke, revokeLabel: "Voltar a permitir" })),
      "Nenhuma.");
    section(corpo, "Disposição do 3D (alterações)", k.layout.map((p) => item(p, {})), "Sem alterações.");
    const hist = k.historico.map((p) => item(p, {}, ))
      .map((row, i) => {
        const p = k.historico[i];
        const estado = p.ativa ? "ativa" : (p.substituida_por ? "substituída" : "revogada");
        row.querySelector(".meta").textContent += ` · ${p.tipo_pt} · ${estado}`;
        return row;
      });
    section(corpo, "Histórico de alterações", hist, "Sem histórico.");
    corpo.appendChild($el("p", "rgpd", `Respondeste a ${k.decisoes_respondidas} decisões; essas respostas também contam como o que sei de ti.`));
  }

  apagar.addEventListener("click", async () => {
    const ok = window.prompt("Isto apaga preferências, regras (desativa os cenários), sugestões, decisões e a conversa. Escreve APAGAR para confirmar:");
    if (ok !== "APAGAR") return;
    try {
      const out = await api("/knowledge/erase", { confirm: "APAGAR" });
      alert(`Apagado: ${out.preferencias} preferências, ${out.decisoes} decisões, ${out.sugestoes} sugestões, ${out.mensagens} mensagens.`);
    } catch (e) { alert(e.message); }
    render();
  });
  render();
}

window.addEventListener("tt:knowledge", open);
export { open as openKnowledge };
