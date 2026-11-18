"""Editing the 3D layout: natural language (chat), by hand (drag in /twin) and undo.

The versioned config/layout.json is the starting point; the user's layout lives in var/ (next to
home.db, gitignored) and every change is also a `layout` preference (before/after snapshots), so
"O que a casa sabe de ti" shows its history and Desfazer can restore the previous version.
Only WHERE things are drawn changes: rooms and devices still come from get_home, devices never
change room, and nothing is written to the MCP.
"""

import json
import re
from pathlib import Path

from pydantic import ValidationError

from ..perception.state import HomeState, de, em, fmt
from .layout import DeviceLayout, Layout, LayoutError, Opening, RoomLayout, load_layout

ROLE_WORDS = {"sensor de temperatura": "temperature", "luz": "light", "aquecedor": "plug", "câmara": "camera",
              "camara": "camera", "detetor de fumo": "smoke", "sensor de contacto": "contact", "central": "hub"}
NOUN_PT = {"temperature": "o sensor de temperatura", "light": "a luz", "plug": "o aquecedor", "camera": "a câmara",
           "smoke": "o detetor de fumo", "contact": "o sensor da janela", "hub": "a central"}
REL_PT = {"direita": "à direita", "esquerda": "à esquerda", "frente": "à frente", "tras": "atrás",
          "ao_lado": "ao lado", "junto_janela": "junto à janela", "junto_porta": "junto à porta",
          "centro": "no centro", "canto": "no canto"}
WALL_GAP = 0.12
SIDE_OF = {"tras": "north", "frente": "south", "esquerda": "west", "direita": "east"}
SIDE_PT = {"north": "de trás", "south": "da frente", "west": "da esquerda", "east": "da direita"}
OPENING_DEFAULTS = {"window": {"width": 1.2, "height": 1.2, "sill": 0.9}, "door": {"width": 0.9, "height": 2.1, "sill": 0.0}}
OPENING_GAP = 0.1  # between two openings, and from the corners


def _wall_len(room: dict, side: str) -> float:
    return room["width"] if side in ("north", "south") else room["depth"]


def _wall(room: dict, side: str) -> dict:
    for w in room["walls"]:
        if w["side"] == side:
            return w
    w = {"side": side, "openings": []}
    room["walls"].append(w)
    return w


def _fits(room: dict, side: str, offset: float, width: float, exclude: str | None = None) -> bool:
    length = _wall_len(room, side)
    if offset - width / 2 < OPENING_GAP - 1e-6 or offset + width / 2 > length - OPENING_GAP + 1e-6:
        return False
    for w in room["walls"]:
        if w["side"] != side:
            continue
        for o in w["openings"]:
            if o["id"] != exclude and offset - width / 2 < o["offset"] + o["width"] / 2 + OPENING_GAP \
                    and o["offset"] - o["width"] / 2 - OPENING_GAP < offset + width / 2:
                return False
    return True


def _place(room: dict, side: str, width: float, posicao: str | None, keep: float | None = None,
           exclude: str | None = None) -> float | None:
    """Offset (centre along the wall) closest to where it was asked for that does not collide."""
    length = _wall_len(room, side)
    lo, hi = width / 2 + OPENING_GAP, length - width / 2 - OPENING_GAP
    if hi < lo:
        return None
    want = {"inicio": lo + 0.2, "centro": length / 2, "fim": hi - 0.2}.get(posicao or "", keep if keep is not None else length / 2)
    want = min(max(want, lo), hi)
    step = 0.05
    for k in range(int(length / step) + 2):
        for cand in (want - k * step, want + k * step):
            if lo - 1e-6 <= cand <= hi + 1e-6 and _fits(room, side, round(cand, 2), width, exclude):
                return round(cand, 2)
    return None


class LayoutProblem(ValueError):
    """The change cannot be applied (message in PT-PT)."""


def user_layout_file(settings) -> Path:
    return Path(settings.history_db).parent / "layout_user.json"


def active_layout_file(settings) -> Path:
    user = user_layout_file(settings)
    return user if user.exists() else Path(settings.layout_file)


def _overlap(a: RoomLayout, b: RoomLayout) -> bool:
    return a.x < b.x + b.width - 1e-6 and b.x < a.x + a.width - 1e-6 and \
        a.z < b.z + b.depth - 1e-6 and b.z < a.z + a.depth - 1e-6


class LayoutEditor:
    def __init__(self, memory, settings, prefs):
        self.memory, self.s, self.prefs = memory, settings, prefs

    # -- files -----------------------------------------------------------------------------
    def current(self) -> Layout:
        return load_layout(active_layout_file(self.s))

    def _save(self, after: Layout, before: Layout, description: str, change: dict, original: str = "") -> dict:
        try:
            after = Layout.model_validate(after.model_dump())
        except ValidationError as e:
            raise LayoutProblem(f"o resultado não é válido ({e.errors()[0]['msg']})") from None
        path = user_layout_file(self.s)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(after.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return self.prefs.set("global", "layout", {"description": description[:300], "change": change,
                                                   "before": before.model_dump(), "after": after.model_dump()},
                              "explicit", original or description)

    # -- natural language --------------------------------------------------------------------
    def _device_for(self, alvo: str, state: HomeState, ref_room: str | None):
        role = ROLE_WORDS.get(alvo.casefold().strip())
        if role is None:
            for word, r in ROLE_WORDS.items():
                if word in alvo.casefold():
                    role = r
                    break
        if role is None:
            return None
        if ref_room and state.room(ref_room):
            d = state.by_role(role, state.room(ref_room).id)
            if d:
                return d
        return state.by_role(role)

    def apply(self, req, state: HomeState) -> tuple[Layout, Layout, str]:
        """LayoutRequest (alvo, relacao, referencia) -> (before, after, description)."""
        before = self.current()
        data = before.model_dump()
        target_room = state.room(req.alvo)
        if target_room is not None and req.relacao in ("direita", "esquerda", "frente", "tras", "ao_lado"):
            return before, self._move_room(data, target_room, req, state), \
                f"Pus {'a' if target_room.name.endswith('a') else 'o'} {target_room.name} {REL_PT[req.relacao]} " \
                f"{de(state.room(req.referencia).name) if req.referencia and state.room(req.referencia) else ''}".strip() + "."
        dev = self._device_for(req.alvo, state, req.referencia)
        if dev is None:
            raise LayoutProblem(f"não encontrei «{req.alvo}» na casa")
        after, what = self._move_device(data, dev, req.relacao, state)
        return before, after, what

    def _move_room(self, data: dict, room, req, state: HomeState) -> Layout:
        ref = state.room(req.referencia) if req.referencia else None
        if ref is None:
            others = [r for r in state.rooms.values() if r.id != room.id]
            if len(others) != 1:
                raise LayoutProblem("diz-me em relação a que divisão (ex.: «à direita da sala»)")
            ref = others[0]
        rooms = data["rooms"]
        if room.id not in rooms or ref.id not in rooms:
            raise LayoutProblem("essa divisão ainda não está no 3D")
        t, r = rooms[room.id], rooms[ref.id]
        rel = "direita" if req.relacao == "ao_lado" else req.relacao
        t["x"], t["z"] = {"direita": (r["x"] + r["width"], r["z"]), "esquerda": (r["x"] - t["width"], r["z"]),
                          "frente": (r["x"], r["z"] + r["depth"]), "tras": (r["x"], r["z"] - t["depth"])}[rel]
        moved = RoomLayout.model_validate(t)
        for oid, o in rooms.items():
            if oid != room.id and _overlap(moved, RoomLayout.model_validate(o)):
                raise LayoutProblem(f"{room.name} ficaria por cima {de(o.get('name') or oid)}")
        return Layout.model_validate(data)

    def _move_device(self, data: dict, dev, rel: str, state: HomeState) -> tuple[Layout, str]:
        room = data["rooms"].get(dev.room_id)
        if room is None:
            raise LayoutProblem(f"{dev.room_name} ainda não está no 3D")
        cur = data["devices"].get(dev.id) or DeviceLayout(room_id=dev.room_id, name=dev.name,
                                                          position=(room["width"] / 2, 1.0, room["depth"] / 2)).model_dump()
        cur["room_id"] = dev.room_id
        x, y, z = cur["position"]
        w, d = room["width"], room["depth"]
        extra = ""
        if rel in ("junto_janela", "junto_porta"):
            kind = "window" if rel == "junto_janela" else "door"
            found = [(wall["side"], o) for wall in room["walls"] for o in wall["openings"] if o["type"] == kind]
            if not found:
                if kind == "door":
                    raise LayoutProblem(f"{dev.room_name} não tem nenhuma porta no 3D")
                wall = next((wl for wl in room["walls"] if wl["side"] == "north"), None)
                if wall is None:
                    wall = {"side": "north", "openings": []}
                    room["walls"].append(wall)
                o = Opening(id=f"janela-{re.sub(r'[^a-z0-9]+', '-', dev.room_name.casefold())}", type="window",
                            offset=w / 2, width=1.2, height=1.2, sill=0.9).model_dump()
                if any(o["offset"] - o["width"] / 2 < op["offset"] + op["width"] / 2 and
                       op["offset"] - op["width"] / 2 < o["offset"] + o["width"] / 2 for op in wall["openings"]):
                    raise LayoutProblem(f"não há espaço para uma janela na parede de trás {de(dev.room_name)}")
                wall["openings"].append(o)
                found = [("north", o)]
                extra = f" Acrescentei uma janela na parede de trás {de(dev.room_name)}, porque o 3D não tinha nenhuma."
            side, o = found[0]
            length = w if side in ("north", "south") else d
            along = o["offset"] + (o["width"] / 2 + 0.35) * (1 if o["offset"] + o["width"] / 2 + 0.5 < length else -1)
            along = min(max(along, 0.2), length - 0.2)
            y = 1.4 if dev.role != "contact" else o.get("sill", 0) + o["height"] / 2
            x, z = {"north": (along, WALL_GAP), "south": (along, d - WALL_GAP),
                    "west": (WALL_GAP, along), "east": (w - WALL_GAP, along)}[side]
            cur["mount"] = "window" if dev.role == "contact" else "wall"
            if dev.role == "contact":
                cur["opening"] = o["id"]
            cur["rotation_y"] = {"north": 0.0, "south": 180.0, "west": 90.0, "east": -90.0}[side]
        elif rel == "centro":
            x, z = w / 2, d / 2
        elif rel == "canto":
            x, z = 0.3, 0.3
        elif rel == "direita":
            x = w - 0.3
        elif rel == "esquerda":
            x = 0.3
        elif rel == "frente":
            z = d - 0.3
        elif rel == "tras":
            z = 0.3
        else:
            raise LayoutProblem("não percebi onde fica")
        cur["position"] = [round(x, 2), round(y, 2), round(z, 2)]
        data["devices"][dev.id] = cur
        return Layout.model_validate(data), \
            f"Pus {NOUN_PT.get(dev.role, dev.name)} {REL_PT.get(rel, rel)} {em(dev.room_name)}.{extra}"

    # -- windows and doors of the floor plan ------------------------------------------------------
    def edit_opening(self, req, state: HomeState) -> tuple[Layout, Layout, str]:
        """OpeningRequest (adicionar / alterar / remover a janela ou porta) -> (before, after, description).
        Openings are drawn only (3D); the contact sensor that watches a window follows it."""
        before = self.current()
        data = before.model_dump()
        otype = "window" if req.tipo == "janela" else "door"
        noun = req.tipo  # janela | porta
        room_st = state.room(req.divisao) if req.divisao else None
        if room_st is None:
            pool = [r for r in state.rooms.values() if r.id in data["rooms"]]
            if req.acao != "adicionar":
                pool = [r for r in pool if any(o["type"] == otype for w in data["rooms"][r.id]["walls"] for o in w["openings"])]
            if len(pool) != 1:
                raise LayoutProblem(f"diz-me em que divisão (ex.: «{'acrescenta uma' if req.acao == 'adicionar' else 'a'} "
                                    f"{noun} {'no quarto' if req.acao == 'adicionar' else 'do quarto'}»)")
            room_st = pool[0]
        room = data["rooms"].get(room_st.id)
        if room is None:
            raise LayoutProblem(f"{room_st.name} ainda não está no 3D")
        where = de(room_st.name)
        d = OPENING_DEFAULTS[otype]

        if req.acao == "adicionar":
            width = req.largura_m or d["width"]
            sill = d["sill"] if otype == "door" else (req.peitoril_m if req.peitoril_m is not None else d["sill"])
            height = min(req.altura_m or d["height"], room["height"] - sill - 0.05)
            sides = [SIDE_OF[req.parede]] if req.parede else ["north", "east", "west", "south"]
            for side in sides:
                offset = _place(room, side, width, req.posicao)
                if offset is not None:
                    break
            else:
                raise LayoutProblem(f"não cabe uma {noun} de {fmt(width)} m na parede {SIDE_PT[sides[0]]} {where}"
                                    if req.parede else f"não há espaço livre nas paredes {where}")
            base = f"{noun}-{re.sub(r'[^a-z0-9]+', '-', room_st.name.casefold()).strip('-')}"
            ids = {o["id"] for r in data["rooms"].values() for w in r["walls"] for o in w["openings"]}
            oid, n = base, 2
            while oid in ids:
                oid, n = f"{base}-{n}", n + 1
            _wall(room, side)["openings"].append({"id": oid, "type": otype, "offset": offset, "width": width,
                                                  "height": round(height, 2), "sill": sill})
            what = f"Acrescentei uma {noun} de {fmt(width)} m na parede {SIDE_PT[side]} {where}."
            return before, self._validated(data), what

        found = [(w, o) for w in room["walls"] for o in w["openings"] if o["type"] == otype]
        if req.parede:
            found = [(w, o) for w, o in found if w["side"] == SIDE_OF[req.parede]] or found
        if not found:
            raise LayoutProblem(f"{room_st.name} não tem nenhuma {noun} no 3D"
                                + (f"; diz «acrescenta uma {noun} {em(room_st.name)}»" if req.acao == "alterar" else ""))
        wall, o = found[0]
        watchers = [dev_id for dev_id, dl in data["devices"].items() if dl.get("opening") == o["id"]]

        if req.acao == "remover":
            if watchers:
                names = ", ".join(data["devices"][i].get("name") or i for i in watchers)
                raise LayoutProblem(f"o sensor ({names}) está nessa {noun}; diz-me primeiro para onde a mudar")
            wall["openings"].remove(o)
            what = f"Removi a {noun} da parede {SIDE_PT[wall['side']]} {where}."
            return before, self._validated(data), what

        # alterar: size, wall, position along the wall
        width = req.largura_m or round(o["width"] + (req.delta_largura_m or 0), 2)
        height = req.altura_m or round(o["height"] + (req.delta_altura_m or 0), 2)
        sill = o["sill"] if otype == "door" else (req.peitoril_m if req.peitoril_m is not None else o["sill"])
        if width < 0.3 or height < 0.3:
            raise LayoutProblem(f"a {noun} ficaria demasiado pequena")
        if sill + height > room["height"] - 0.02:
            raise LayoutProblem(f"a {noun} ficaria mais alta do que a parede ({fmt(room['height'])} m)")
        new_side = SIDE_OF[req.para_parede] if req.para_parede else wall["side"]
        wall["openings"].remove(o)
        keep = o["offset"] if new_side == wall["side"] else None
        offset = _place(room, new_side, width, req.posicao, keep=keep, exclude=o["id"])
        if offset is None:
            raise LayoutProblem(f"uma {noun} de {fmt(width)} m não cabe na parede {SIDE_PT[new_side]} {where}")
        o.update(offset=offset, width=width, height=height, sill=sill)
        _wall(room, new_side)["openings"].append(o)
        for dev_id in watchers:  # the window's sensor moves with it
            self._stick_to_opening(data["devices"][dev_id], room, new_side, o)
        changes = []
        if new_side != wall["side"]:
            changes.append(f"passou para a parede {SIDE_PT[new_side]}")
        elif req.posicao:
            changes.append({"inicio": "mais para o início da parede", "centro": "ao centro da parede",
                            "fim": "mais para o fim da parede"}[req.posicao])
        if req.largura_m or req.delta_largura_m:
            changes.append(f"{fmt(width)} m de largura")
        if req.altura_m or req.delta_altura_m:
            changes.append(f"{fmt(height)} m de altura")
        if req.peitoril_m is not None and otype == "window":
            changes.append(f"peitoril a {fmt(sill)} m")
        what = f"Mudei a {noun} {where}: " + (", ".join(changes) or "sem alterações") + "."
        if watchers:
            what += " O sensor da janela foi com ela."
        return before, self._validated(data), what

    @staticmethod
    def _stick_to_opening(dev: dict, room: dict, side: str, o: dict) -> None:
        length = _wall_len(room, side)
        along = o["offset"] + o["width"] / 2 + 0.08
        if along > length - 0.1:
            along = o["offset"] - o["width"] / 2 - 0.08
        y = o.get("sill", 0) + o["height"] / 2
        x, z = {"north": (along, WALL_GAP), "south": (along, room["depth"] - WALL_GAP),
                "west": (WALL_GAP, along), "east": (room["width"] - WALL_GAP, along)}[side]
        dev["position"] = [round(x, 2), round(y, 2), round(z, 2)]
        dev["rotation_y"] = {"north": 0.0, "south": 180.0, "west": 90.0, "east": -90.0}[side]

    @staticmethod
    def _validated(data: dict) -> Layout:
        try:
            return Layout.model_validate(data)
        except ValidationError as e:
            raise LayoutProblem(f"o resultado não é válido ({e.errors()[0]['msg']})") from None

    # -- chat -----------------------------------------------------------------------------------
    def _ask_ok(self, chat, text: str, pref_id: str) -> dict:
        import time
        import uuid
        chat.confirm = {"id": uuid.uuid4().hex[:8], "kind": "layout_ok", "payload": {"pref_id": pref_id},
                        "text": text, "original": "", "ts": time.time()}
        buttons = [{"label": "Ficou bem", "action": "confirm", "id": chat.confirm["id"]},
                   {"label": "Não", "action": "cancel", "id": chat.confirm["id"]},
                   {"label": "Desfazer", "action": "layout_undo"}]
        return chat.say(f"{text} Ficou bem?", buttons=buttons, confirm_id=chat.confirm["id"], layout_changed=True)

    def from_chat(self, req, text: str, chat) -> list[dict]:
        state = self.memory.state
        if req is None:
            return [chat.say("Não percebi a mudança. Exemplos: «o quarto fica à direita da sala», «o sensor de "
                             "temperatura está junto à janela», «acrescenta uma janela na parede da direita do quarto».")]
        opening = getattr(req, "acao", None) in ("adicionar", "alterar", "remover")  # a window / door
        try:
            if opening:
                before, after, what = self.edit_opening(req, state)
            else:
                before, after, what = self.apply(req, state)
            row = self._save(after, before, what, req.model_dump(), text)
        except (LayoutProblem, LayoutError) as e:
            if opening:
                return [chat.say(f"Não consegui mudar a {req.tipo}: {e}. Podes dizer-me de outra forma "
                                 "(ex.: outra parede, ou uma largura mais pequena).")]
            return [chat.say(f"Não consegui mudar o 3D: {e}. Queres ajustar à mão?",
                             buttons=[{"label": "Ajustar à mão", "action": "layout_manual"}])]
        return [self._ask_ok(chat, what, row["id"])]

    def not_ok(self, chat) -> dict:
        return chat.say("Queres ajustar à mão no 3D ou descrever de outra forma?",
                        buttons=[{"label": "Ajustar à mão", "action": "layout_manual"},
                                 {"label": "Descrever de outra forma", "action": "layout_rephrase"},
                                 {"label": "Desfazer", "action": "layout_undo"}])

    def chat_action(self, action: str, chat) -> list[dict]:
        if action == "layout_manual":
            chat.confirm = None
            return [chat.say("Abri o modo de edição no 3D: arrasta as divisões e os dispositivos e carrega em "
                             "«Gravar». Também podes desfazer lá.")]
        if action == "layout_rephrase":
            chat.confirm = None
            return [chat.say("Diz-me de outra forma, por exemplo «o quarto fica atrás da sala» ou «a luz fica no "
                             "centro do quarto».")]
        if action == "layout_undo":
            chat.confirm = None
            what = self.undo()
            return [chat.say(f"Desfeito: {what}" if what else "Não há nenhuma alteração para desfazer.",
                             layout_changed=True)]
        return [chat.say("Não percebi esse botão.")]

    # -- manual + undo ---------------------------------------------------------------------------
    def manual(self, rooms: dict, devices: dict, chat=None) -> dict:
        """rooms: {id: {x, z}}; devices: {id: {position: [x, y, z] ABSOLUTE}} (from the drag mode)."""
        state = self.memory.state
        before = self.current()
        data = before.model_dump()
        for rid, pos in (rooms or {}).items():
            if rid not in data["rooms"]:
                raise LayoutProblem(f"divisão desconhecida: {rid}")
            data["rooms"][rid]["x"] = round(float(pos["x"]), 2)
            data["rooms"][rid]["z"] = round(float(pos["z"]), 2)
        for did, d in (devices or {}).items():
            dev = state.devices.get(did) if state else None
            if dev is None or dev.room_id not in data["rooms"]:
                raise LayoutProblem(f"dispositivo desconhecido: {did}")
            room = data["rooms"][dev.room_id]
            ax, ay, az = (float(v) for v in d["position"])
            rel = [round(min(max(ax - room["x"], 0.05), room["width"] - 0.05), 2), round(ay, 2),
                   round(min(max(az - room["z"], 0.05), room["depth"] - 0.05), 2)]
            cur = data["devices"].get(did) or {"room_id": dev.room_id, "name": dev.name, "mount": "wall"}
            cur["position"] = rel
            cur["room_id"] = dev.room_id
            data["devices"][did] = cur
        new = [RoomLayout.model_validate(r) for r in data["rooms"].values()]
        for i, a in enumerate(new):
            for b in new[i + 1:]:
                if _overlap(a, b):
                    raise LayoutProblem(f"{a.name} e {b.name} ficariam sobrepostas")
        n = len(rooms or {}) + len(devices or {})
        what = f"Ajuste à mão no 3D ({n} {'alteração' if n == 1 else 'alterações'})."
        row = self._save(Layout.model_validate(data), before, what, {"manual": {"rooms": rooms, "devices": devices}})
        if chat is not None:
            chat.say(f"Gravei o teu ajuste à mão: {n} {'alteração' if n == 1 else 'alterações'} no 3D.",
                     buttons=[{"label": "Desfazer", "action": "layout_undo"}], layout_changed=True)
        return row

    def undo(self) -> str | None:
        active = self.prefs.get_active("global", "layout")
        if not active:
            return None
        last = active[-1]
        before = Layout.model_validate(last["value"]["before"])
        current = self.current()
        desc = last["value"]["description"]
        self._save(before, current, f"Desfeito: {desc}"[:300], {"undo": last["id"]})
        return desc
