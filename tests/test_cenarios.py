"""Scenario mode of the pitch videos (src/cenarios): deterministic texts, the emergency path above every
preference, and a reset that never touches var/home.db. No real house, MCP or LLM."""

import os
import time

import pytest

import src.mcp_client
from src.cenarios.__main__ import build_stage, scenario_env, twin_snippet
from src.config import get_settings

PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"


@pytest.fixture
def stage(tmp_path, monkeypatch):
    # build_stage refuses any real McpClient from then on: monkeypatch puts the real class back afterwards
    monkeypatch.setattr(src.mcp_client, "McpClient", src.mcp_client.McpClient)
    for k, v in scenario_env(tmp_path / "cenarios").items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    st = build_stage(get_settings(), delay_scale=0, think_s=0, batch_s=0.05)
    st.director.reset()
    yield st
    if st.bridge is not None:
        st.bridge.stop()
    get_settings.cache_clear()


def wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def texts(msgs):
    return [m["text"] for m in msgs]


def hhmmss(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def timeline(st):
    return [(hhmmss(x.ts), x.message) for x in sorted(
        [e for e in st.memory.recent_events(200) if e.type != "tick"] + st.memory.recent_decisions(200),
        key=lambda x: x.ts)]


def test_scenarios_1_to_5_in_sequence_say_exactly_the_recordings(stage):
    st, d, chat = stage, stage.director, stage.chat
    # 1 · rule created by talking ("janela" -> the only contact sensor, on the Sala door)
    d.scenario("1")
    out = chat.handle("Se abrir a janela com o aquecedor ligado, avisa-me.")
    assert texts(out)[-1] == (
        "Vou criar a regra TT_porta_aberta_aquecedor_ligado no MCP: quando a porta da Sala abrir e o aquecedor do "
        "Quarto estiver ligado, a luz do Quarto fica âmbar como aviso e aviso-te aqui no chat. Confirmas?")
    assert texts(chat.action("confirm", chat.confirm["id"])) == [
        "Confirmar", "Regra TT_porta_aberta_aquecedor_ligado criada. Cenário criado no MCP."]
    assert ("16:30:40", "Regra TT_porta_aberta_aquecedor_ligado: Cenário criado no MCP.") in timeline(st)
    assert [s["name"] for s in st.mcp.scenarios()] == ["TT_porta_aberta_aquecedor_ligado"]

    # 2 · autonomy limit, with the preference id of the recordings
    d.scenario("2", fresh=False)
    assert texts(chat.handle("Nunca acendas as luzes sozinha."))[-1] == \
        "Nunca vou agir em Luz: só registo e te informo. Confirmas?"
    out = chat.action("confirm", chat.confirm["id"])
    assert texts(out)[-1] == "Feito. Guardei: autonomia Luz: nunca faz (só informa)."
    assert out[-1]["pref_id"] == "23113697957b"

    # 3 · conflict decided with the user: lead-in, reject, door, rule notice (no amber light), approve
    d.scenario("3", fresh=False)
    assert wait(lambda: st.memories.decisions.pending())
    chat.action("decision", st.memories.decisions.pending()[-1]["id"], "rejected")
    d.door(True)
    assert wait(lambda: st.memories.decisions.pending())
    light_before = st.mcp.light()["color_rgb"]
    out = chat.action("decision", st.memories.decisions.pending()[-1]["id"], "approved")
    assert texts(out) == ["Aprovar", "Feito."]
    st.perception.step()
    assert st.mcp.plug()["power"] == "off" and st.mcp.plug()["instantPower"] == 0
    assert st.mcp.light()["color_rgb"] == light_before == "255,255,255"  # Luz «Nunca»: the rule only notifies
    msgs = texts(st.chat.store.after(0, 200))
    assert ("Regra TT_porta_aberta_aquecedor_ligado: a porta da Sala abrir e o aquecedor do Quarto estiver ligado. "
            "Aconteceu agora.") in msgs
    assert ("Proposta para aprovares: Porta da Sala aberta. Energia propõe desligar a tomada do Quarto: Temperatura "
            "do Quarto (22.8 °C) já acima do setpoint (21.0 °C); manter o aquecedor a 7 W é desperdício. Coordenador: "
            "Porta da Sala aberta e aquecedor do Quarto ligado a 7 W com temperatura (22,8 °C) acima do setpoint "
            "(21,0 °C); aquecer com porta aberta é desperdício.") in msgs
    tl = timeline(st)
    for item in [("16:44:23", "Aquecedor (tomada) do Quarto ligado."),
                 ("16:44:36", "Aquecedor do Quarto ligado a 7 W com a temperatura já a 22,8 °C, acima do setpoint de "
                              "21,0 °C; sem janela nem porta aberta. Proponho desligar a tomada do Quarto."),
                 ("16:44:51", "Proposta da Energia: desligar a tomada do Quarto. À espera da tua aprovação no chat."),
                 ("16:45:42", "Porta da Sala aberta."),
                 ("16:46:34", "Proposta da Energia: desligar a tomada do Quarto. À espera da tua aprovação no chat."),
                 ("16:46:52", "Aprovado por ti: desligar a tomada do Quarto. Feito."),
                 ("16:46:58", "Aquecedor (tomada) do Quarto desligado.")]:
        assert item in tl

    # 4 · security above everything
    d.scenario("4", fresh=False)
    assert texts(chat.handle("Vou sair."))[-1] == \
        "Vou armar a casa (modo ausente). Confirmas? (Se ficares em casa: «arma em modo casa».)"
    assert texts(chat.action("confirm", chat.confirm["id"]))[-1] == "Segurança: Feito."
    st.perception.step()
    d.door(True)
    # one A2A batch per click, as in a demo (seconds apart): Segurança answers the door before the camera
    assert wait(lambda: any(x.message.startswith("Porta da Sala aberta com a casa em alarme")
                            for x in st.memory.recent_decisions(50)))
    d.camera()
    st.perception.step()
    assert st.mcp.light()["color_rgb"] == "255,0,0"
    assert wait(lambda: any("duas pessoas" in x.message for x in st.memory.recent_decisions(50)))
    assert texts(chat.handle("falso alarme, era eu"))[-1] == "Vou desarmar a casa e desligar o alarme. Confirmas?"
    assert texts(chat.action("confirm", chat.confirm["id"]))[-1] == "Segurança: Feito. Alarme desligado."
    st.perception.step()
    assert wait(lambda: any(x.message.startswith("Alarme ativo na Sala") for x in st.memory.recent_decisions(50)))
    assert st.mcp.light()["color_rgb"] == "255,255,255"
    tl = timeline(st)
    for item in [("17:20:23", "A casa está em alarme!"),
                 ("17:20:26", "Análise visual da Sala: 2 pessoas detetadas (98%)."),
                 ("17:20:27", "Intrusão na Sala (pessoa vista na câmara com a casa armada (ausente)): luz a vermelho "
                              "no máximo (regra fixa, sem LLM). Feito."),
                 ("17:21:30", "Desarmada por ti: luz reposta como estava antes da intrusão. Feito."),
                 ("17:21:31", "Segurança: armada (ausente) → desarmada.")]:
        assert item in tl

    # 5 · proactive suggestion: detected in code over the history, phrase from the fixture
    d.scenario("5", fresh=False)
    out = chat.handle("Tens alguma sugestão?")
    assert out[-1]["text"] == (
        "Sugestão (Conforto): A humidade no Quarto subiu nas 3 noites registadas (+10, +10, +11 pontos). Se tiveres "
        "um desumidificador, liga-o à tomada inteligente e eu cuido de o ativar; para já, queres que te avise quando "
        "passar de 70 %?")
    ev = st.insights.evidence(out[-1]["suggestion_id"])
    assert ev["resumo"] == "Dias observados: 16/11, 17/11, 18/11 · janela 22:00–07:00 · confiança 95 %"
    assert min(p[1] for p in ev["series"][0]["points"]) == 56.5
    assert texts(chat.action("suggestion", out[-1]["suggestion_id"], "accept"))[-1] == (
        "Vou criar a regra «se a humidade no Quarto passar de 70 %, avisa-me», para arejares a divisão a tempo. "
        "Confirmas?")
    assert texts(chat.action("confirm", chat.confirm["id"]))[-1] == \
        "Feito: regra TT_humi_acima_70. Cenário criado no MCP."
    assert ("17:42:57", "Regra TT_humi_acima_70: Cenário criado no MCP.") in timeline(st)


@pytest.mark.parametrize("never", [("luz",), ("luz", "seguranca"), ("energia", "conforto", "seguranca", "luz")])
def test_emergency_turns_the_light_red_whatever_the_autonomy(stage, never):
    """Scenario 4: no preference switches off the fixed intrusion rule (Luz and Segurança «Nunca» included)."""
    from src.knowledge import Knowledge
    st = stage
    st.director.scenario("4")
    kn = Knowledge(st.memories, st.coordinator, chat=st.chat, insights=st.insights)
    for cat in never:
        kn.set_autonomy(cat, "nunca")
    st.coordinator.set_security("AWAY", confirmed=True)
    st.perception.step()
    st.director.door(True)
    st.director.camera()
    light = st.mcp.light()
    assert (light["power"], light["color_rgb"], light["brightness"]) == ("on", "255,0,0", 100)
    fixed = [x for x in st.memory.recent_decisions(20) if x.message.startswith("Intrusão na Sala")]
    assert fixed and fixed[-1].status == "executada" and fixed[-1].agent == "Segurança"


def test_scenario_6_starts_with_three_changes_and_two_answers(stage):
    from src.knowledge import Knowledge
    st = stage
    st.director.scenario("6")
    kn = Knowledge(st.memories, st.coordinator, chat=st.chat, insights=st.insights)
    v = kn.view(st.memory.state)
    assert [(a["nome"], a["nivel"]) for a in v["autonomia"]] == [
        ("Energia", "perguntar"), ("Conforto", "perguntar"), ("Segurança", "perguntar"), ("Luz", "nunca")]
    assert len(v["historico"]) == 3 and v["decisoes_respondidas"] == 2
    assert [r["descricao"].split(":")[0] for r in v["regras"]] == ["regra TT_porta_aberta_aquecedor_ligado"]
    muted = v["silenciadas"][0]
    assert (muted["descricao"], muted["ambito"], muted["origem"], muted["data"], muted["frase"]) == (
        "não sugerir: criar a regra «se a humidade no Quarto passar de 70 %, avisa-me», para arejares a divisão a "
        "tempo", "casa", "dita por ti", "19/11/2026 17:06", "Não voltes a sugerir isto")
    for cat, level in (("energia", "nunca"), ("conforto", "nunca"), ("seguranca", "nunca"), ("luz", "auto")):
        kn.set_autonomy(cat, level)
    assert len(kn.view(st.memory.state)["historico"]) == 7


def test_reset_clears_only_the_scenario_files(stage, tmp_path):
    st = stage
    home_db = tmp_path / "home.db"  # where var/home.db would be, next to var/cenarios/
    home_db.write_bytes(b"dados reais")
    assert st.settings.history_db == tmp_path / "cenarios" / "sim.db"
    st.director.scenario("3")
    assert wait(lambda: st.memories.decisions.pending())
    st.director.load_humidity()
    st.director.reset()
    assert st.memories.prefs.history(n=10) == [] and st.memories.decisions.recent(10) == []
    assert st.chat.store.after(0) == [] and st.mcp.scenarios() == []
    assert st.memories.history.summary() == []
    assert home_db.read_bytes() == b"dados reais"
    assert st.mcp.plug()["power"] == "on" and st.mcp.plug()["instantPower"] == 7.0


def test_no_real_mcp_client_in_scenario_mode(stage):
    with pytest.raises(RuntimeError):
        src.mcp_client.McpClient(allow_write=True)
    assert all(tool in {"get_home", "get_home_security_alarm_history", "post_new_scenario"}
               for tool, _ in stage.mcp.calls)


def test_twin_page_unchanged_without_the_flag():
    from src.api import UI_DIR, create_app
    from src.memory import SharedMemory
    from fastapi.testclient import TestClient
    os.environ.setdefault("TT_NO_REAL_WRITES", "1")
    plain = TestClient(create_app(SharedMemory(), get_settings())).get("/twin").text
    assert plain == (UI_DIR / "twin" / "index.html").read_text(encoding="utf-8")
    assert "realizador" not in plain
    assert "realizador.js" in twin_snippet({"sugestoes_rapidas": ["Tens alguma sugestão?"]})
