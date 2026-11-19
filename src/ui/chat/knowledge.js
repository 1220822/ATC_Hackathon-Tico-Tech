// "O que a casa sabe de ti": everything the house knows about the user, with edit / revoke / erase.
// On top, a summary that is changed in place: the target temperature of each room and how much the
// house may do on its own per area. Opened by the chat header button (window event "tt:knowledge").

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

const nf = new Intl.NumberFormat("pt-PT", { maximumFractionDigits: 1 });

const EDIT_HINT = {
  setpoint_c: "Temperatura em °C (ex.: 21)", comfort_range_c: "Intervalo em °C (ex.: 19-22)",
  humidity_max_pct: "Humidade máxima em % (ex.: 65)", autonomy: "auto, perguntar ou nunca",
  quiet_hours: "Horas sem sugestões (ex.: 23:00-07:30)", note: "Texto da nota",
};

const LEVELS = [["auto", "Faz sozinha"], ["perguntar", "Pergunta-me"], ["nunca", "Nunca"]];

const ORIGEM_TEMP = { tua: "definida por ti", geral: "preferência geral da casa", casa: "valor da casa (não disseste nada)" };

// Inline form: one field, Guardar / Cancelar; Enter saves, Escape cancels (and does not close the panel).
function inlineForm({ value = "", placeholder, label, type = "text", attrs = {}, onSave, onCancel }) {
  const form = $el("form", "kn-form");
  const input = $el("input");
  input.type = type;
  input.value = value;
  if (placeholder) input.placeholder = placeholder;
  input.setAttribute("aria-label", label);
  Object.entries(attrs).forEach(([k, v]) => input.setAttribute(k, v));
  const guardar = $el("button", "primario", "Guardar");
  guardar.type = "submit";
  const cancelar = $el("button", null, "Cancelar");
  cancelar.type = "button";
  const erro = $el("div", "kn-erro");
  erro.setAttribute("role", "alert");
  form.append(input, guardar, cancelar, erro);
  cancelar.addEventListener("click", onCancel);
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); onCancel(); } });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!input.value.trim()) { erro.textContent = "Escreve um valor."; input.focus(); return; }
    guardar.disabled = true;
    try {
      await onSave(input.value.trim());
    } catch (err) {
      erro.textContent = err.message;
      guardar.disabled = false;
      input.focus();
    }
  });
  setTimeout(() => { input.focus(); input.select(); });
  return form;
}

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
    b.addEventListener("click", () => {
      b.hidden = true;
      const form = inlineForm({
        placeholder: EDIT_HINT[p.tipo] || "Novo valor", label: `Novo valor: ${p.descricao}`,
        onSave: (text) => onEdit(p, text),
        onCancel: () => { form.remove(); b.hidden = false; b.focus(); },
      });
      body.appendChild(form);
    });
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

function tempTile(t, refresh) {
  const tile = $el("div", "kn-tile");
  tile.appendChild($el("div", "kn-rotulo", t.divisao));
  const valor = $el("div", "kn-valor", t.c == null ? "—" : `${nf.format(t.c)} °C`);
  const origem = $el("div", "meta", ORIGEM_TEMP[t.origem] || "");
  const mudar = $el("button", null, "Mudar");
  mudar.setAttribute("aria-label", `Mudar a temperatura alvo ${t.divisao}`);
  mudar.dataset.focus = `temp:${t.divisao_id}`;
  tile.append(valor, origem);
  if (t.frase && t.origem === "tua") tile.appendChild($el("div", "frase", `«${t.frase}»`));
  tile.appendChild(mudar);
  mudar.addEventListener("click", () => {
    mudar.hidden = true;
    const form = inlineForm({
      value: t.c == null ? "" : String(t.c), label: `Temperatura alvo ${t.divisao} em °C`, type: "number",
      attrs: { min: "10", max: "30", step: "0.5", inputmode: "decimal" },
      onSave: async (v) => {
        await api("/knowledge/setpoint", { divisao_id: t.divisao_id, c: Number(v.replace(",", ".")) });
        refresh(`temp:${t.divisao_id}`);
      },
      onCancel: () => { form.remove(); mudar.hidden = false; mudar.focus(); },
    });
    tile.appendChild(form);
  });
  return tile;
}

function autonomyRow(a, refresh) {
  const row = $el("div", "kn-autonomia");
  row.appendChild($el("span", "kn-rotulo", a.nome));
  const grupo = $el("div", "kn-niveis");
  grupo.setAttribute("role", "group");
  grupo.setAttribute("aria-label", `Autonomia: ${a.nome}`);
  const erro = $el("div", "kn-erro");
  erro.setAttribute("role", "alert");
  LEVELS.forEach(([nivel, label]) => {
    const b = $el("button", null, label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(a.nivel === nivel));
    b.dataset.focus = `aut:${a.categoria}:${nivel}`;
    b.addEventListener("click", async () => {
      if (a.nivel === nivel) return;
      grupo.querySelectorAll("button").forEach((x) => { x.disabled = true; });
      try {
        await api("/knowledge/autonomy", { categoria: a.categoria, nivel });
        refresh(`aut:${a.categoria}:${nivel}`);
      } catch (e) {
        erro.textContent = e.message;
        grupo.querySelectorAll("button").forEach((x) => { x.disabled = false; });
      }
    });
    grupo.appendChild(b);
  });
  row.append(grupo, erro);
  return row;
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
  const opener = document.activeElement;
  const close = () => { fundo.remove(); document.removeEventListener("keydown", esc); opener?.focus?.(); };
  function esc(e) { if (e.key === "Escape") close(); }
  fechar.addEventListener("click", close);
  fundo.addEventListener("click", (e) => { if (e.target === fundo) close(); });
  document.addEventListener("keydown", esc);
  fechar.focus();

  async function render(focusKey) {
    let k;
    try {
      k = await api("/knowledge");
    } catch (e) {
      corpo.replaceChildren($el("div", "vazio", `Não consegui ler: ${e.message}`));
      return;
    }
    corpo.replaceChildren();
    const onEdit = async (p, text) => {
      await api(`/knowledge/prefs/${p.id}/edit`, { text });
      render();
    };
    const onRevoke = async (p) => {
      if (!confirm(`Revogar: ${p.descricao}?`)) return;
      try { await api(`/knowledge/prefs/${p.id}/revoke`, {}); } catch (e) { alert(e.message); }
      render();
    };

    corpo.appendChild($el("h3", null, "Temperatura alvo"));
    const grelha = $el("div", "kn-grelha");
    k.temperaturas.forEach((t) => grelha.appendChild(tempTile(t, render)));
    corpo.appendChild(k.temperaturas.length ? grelha : $el("div", "vazio", "Ainda estou a ler as divisões da casa."));

    corpo.appendChild($el("h3", null, "O que a casa faz sozinha"));
    const aut = $el("div", "kn-cartao");
    k.autonomia.forEach((a) => aut.appendChild(autonomyRow(a, render)));
    corpo.appendChild(aut);

    section(corpo, "Outras preferências", k.preferencias.map((p) => item(p, { onEdit, onRevoke })),
      "Nenhuma. Diz-me no chat, por exemplo «não quero sugestões depois das 23h».");
    section(corpo, "Regras", k.regras.map((p) => item(p, { onRevoke, revokeLabel: "Revogar (desativa o cenário)", extra: p.onde })),
      "Sem regras. Cria uma no chat: «se abrir a janela com o aquecedor ligado, avisa-me».");
    if (k.silenciadas.length) {
      section(corpo, "Sugestões silenciadas", k.silenciadas.map((p) => item(p, { onRevoke, revokeLabel: "Voltar a permitir" })), "");
    }

    const hist = $el("details", "kn-historico");
    hist.appendChild($el("summary", null, `Histórico de alterações (${k.historico.length + k.layout.length})`));
    const rows = k.historico.map((p) => {
      const row = item(p, {});
      const estado = p.ativa ? "ativa" : (p.substituida_por ? "substituída" : "revogada");
      row.querySelector(".meta").textContent += ` · ${p.tipo_pt} · ${estado}`;
      return row;
    });
    if (k.layout.length) rows.push(...k.layout.map((p) => item(p, { extra: "disposição do 3D" })));
    if (!rows.length) hist.appendChild($el("div", "vazio", "Sem histórico."));
    rows.forEach((r) => hist.appendChild(r));
    corpo.appendChild(hist);

    corpo.appendChild($el("p", "rgpd", `Respondeste a ${k.decisoes_respondidas} decisões; essas respostas também contam como o que sei de ti. ${k.nota}`));
    if (focusKey) corpo.querySelector(`[data-focus="${focusKey}"]`)?.focus();
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
