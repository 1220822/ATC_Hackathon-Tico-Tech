"""Dump the real capabilities of the Connected Home account to docs/capabilities/.

Runs READ-ONLY tools only (enforced by READ_ONLY below) and saves each response as JSON:
    python scripts/dump_capabilities.py

Reuses the MCP session and auth from scripts/mcp.py / scripts/connected_home_auth.py.
Values that look like secrets (tokens, passwords, RTSP URLs with credentials) are redacted
before writing, because docs/capabilities/ is committed.
"""

import json
import re
import sys
from pathlib import Path

from mcp import Session

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "capabilities"

READ_ONLY = {
    "get_home", "get_home_devices", "get_home_rooms", "get_home_status_brief",
    "get_home_detail_report", "get_device_details", "get_device_recommendations",
    "get_device_timers", "get_home_scenarios", "get_scenario",
    "get_home_security_status", "get_home_security_alarm_history",
}

SECRET_KEYS = re.compile(r"(pass(word)?|secret|token|api[_-]?key|credential|auth)", re.I)
URL_CREDS = re.compile(r"(\w+://)[^/@\s:]+:[^/@\s]+@")


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if SECRET_KEYS.search(k) and isinstance(v, str) and v else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return URL_CREDS.sub(r"\1<redacted>@", obj)
    return obj


def call(session, name, **args):
    if name not in READ_ONLY:
        raise RuntimeError(f"{name} is not a read-only tool; refusing to call it")
    result = session.rpc("tools/call", {"name": name, "arguments": args})
    texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
    parsed = []
    for t in texts:
        try:
            parsed.append(json.loads(t))
        except ValueError:
            parsed.append(t)
    data = parsed[0] if len(parsed) == 1 else parsed
    if not texts and "structuredContent" in result:
        data = result["structuredContent"]
    return {"isError": bool(result.get("isError")), "data": data}


def save(name, payload):
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(redact(payload), indent=2, ensure_ascii=False), encoding="utf-8")
    flag = " (isError)" if payload.get("isError") else ""
    print(f"  -> {path.relative_to(ROOT)}{flag}")


def find_list(data, *keys):
    """Return the first list found at data[key] (or data itself if it is a list)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def ident(item, *keys):
    for k in keys:
        if isinstance(item, dict) and item.get(k) not in (None, ""):
            return str(item[k])
    return None


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:80]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    session = Session()

    for name in ("get_home", "get_home_rooms", "get_home_status_brief", "get_home_detail_report",
                 "get_home_security_status", "get_device_recommendations"):
        print(name)
        save(name, call(session, name))

    print("get_home_security_alarm_history")
    save("get_home_security_alarm_history", call(session, "get_home_security_alarm_history", page=0, size=50))

    print("get_home_devices")
    devices = call(session, "get_home_devices")
    save("get_home_devices", devices)
    for dev in find_list(devices["data"], "devices", "items", "data", "content"):
        did = ident(dev, "deviceId", "id", "_id", "uuid")
        if not did:
            continue
        label = safe_name(f"{ident(dev, 'name', 'deviceName') or 'device'}_{did}")
        print(f"get_device_details / get_device_timers: {label}")
        save(f"device_{label}", call(session, "get_device_details", deviceId=did, techInfo=True))
        save(f"timers_{label}", call(session, "get_device_timers", deviceId=did))

    print("get_home_scenarios")
    scenarios = call(session, "get_home_scenarios")
    save("get_home_scenarios", scenarios)
    for sc in find_list(scenarios["data"], "scenarios", "items", "data", "content"):
        sid = ident(sc, "scenarioId", "id", "_id", "uuid")
        if sid:
            label = safe_name(f"{ident(sc, 'name') or 'scenario'}_{sid}")
            print(f"get_scenario: {label}")
            save(f"scenario_{label}", call(session, "get_scenario", scenarioId=sid))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
