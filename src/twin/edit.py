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

from ..perception.state import HomeState, de, em
from .layout import DeviceLayout, Layout, LayoutError, Opening, RoomLayout, load_layout

ROLE_WORDS = {"sensor de temperatura": "temperature", "luz": "light", "aquecedor": "plug", "câmara": "camera",
              "camara": "camera", "detetor de fumo": "smoke", "sensor de contacto": "contact", "central": "hub"}
NOUN_PT = {"temperature": "o sensor de temperatura", "light": "a luz", "plug": "o aquecedor", "camera": "a câmara",
           "smoke": "o detetor de fumo", "contact": "o sensor da janela", "hub": "a central"}
REL_PT = {"direita": "à direita", "esquerda": "à esquerda", "frente": "à frente", "tras": "atrás",
          "ao_lado": "ao lado", "junto_janela": "junto à janela", "junto_porta": "junto à porta",
          "centro": "no centro", "canto": "no canto"}
WALL_GAP = 0.12


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
                             "temperatura está junto à janela».")]
        try:
            before, after, what = self.apply(req, state)
            row = self._save(after, before, what, req.model_dump(), text)
        except (LayoutProblem, LayoutError) as e:
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
