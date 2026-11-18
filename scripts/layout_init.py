"""Generate an initial 3D layout (config/layout.json) from the real get_home (read-only).

Rooms side by side with a fixed size; each device at the typical place of its type (light and
smoke detector on the ceiling, contact sensor on a window, plug low on a wall, camera in a corner
looking at the door, ...). Adjust the file by hand afterwards.

    python scripts/layout_init.py                    # reads get_home from the MCP (read tool only)
    python scripts/layout_init.py --from-dump        # uses docs/capabilities/get_home.json instead
    python scripts/layout_init.py --out other.json   # default: LAYOUT_FILE (config/layout.json)
    python scripts/layout_init.py --force            # overwrite an existing file
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ROOT, get_settings  # noqa: E402
from src.home_config import load_home_config  # noqa: E402
from src.twin.layout import auto_layout, rooms_from_get_home  # noqa: E402

DUMP = ROOT / "docs" / "capabilities" / "get_home.json"


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from-dump", action="store_true", help=f"usar {DUMP.relative_to(ROOT)}")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--force", action="store_true", help="sobrescrever o ficheiro se já existir")
    a = p.parse_args()

    settings = get_settings()
    out = a.out or settings.layout_file
    if out.exists() and not a.force:
        raise SystemExit(f"{out} já existe; usa --force para o sobrescrever.")

    if a.from_dump:
        home = json.loads(DUMP.read_text(encoding="utf-8"))["data"]
    else:
        from src.mcp_client import McpClient
        home = McpClient(allow_write=False).call("get_home")

    sim_ids = set(load_home_config(settings.home_config_file).sim_room_ids)
    rooms = [r for r in rooms_from_get_home(home) if r["id"] not in sim_ids]
    if not rooms:
        raise SystemExit("O get_home não tem divisões nem dispositivos.")
    layout = auto_layout(rooms)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(layout.model_dump(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    n_dev = sum(len(r["devices"]) for r in rooms)
    print(f"Layout escrito em {out}: {len(rooms)} divisões, {n_dev} dispositivos.")
    for r in rooms:
        print(f"  {r['name']}: " + ", ".join(d["name"] for d in r["devices"]))


if __name__ == "__main__":
    main()
