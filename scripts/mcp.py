"""Call any Connected Home MCP tool from the command line.

Usage:
    python scripts/mcp.py                                  # list tools
    python scripts/mcp.py <tool> --help                    # show the tool's parameters
    python scripts/mcp.py <tool> [key=value ...]           # call the tool
    python scripts/mcp.py <tool> --json '{"key": "value"}' # call with a JSON object of arguments

Values in key=value are parsed as JSON when possible (numbers, true/false, lists, objects),
otherwise passed as strings. Example:
    python scripts/mcp.py get_device_details device_id=123
    python scripts/mcp.py update_device_capabilities query="luz da sala" value=true

Requires a previous `python scripts/connected_home_auth.py login`.
"""

import json
import sys

from connected_home_auth import mcp_request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class Session:
    def __init__(self):
        self.sid = None
        self.next_id = 1
        res = self.rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "connected-home-cli", "version": "1.0"},
        })
        mcp_request({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.sid)

    def rpc(self, method, params):
        res, self.sid = mcp_request(
            {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}, self.sid)
        self.next_id += 1
        if not res:
            raise SystemExit(f"{method}: resposta vazia do servidor")
        if "error" in res:
            raise SystemExit(f"{method} falhou: {json.dumps(res['error'], ensure_ascii=False)}")
        return res["result"]

    def tools(self):
        tools, cursor = [], None
        while True:
            result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools += result.get("tools", [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools


def summary(desc):
    lines = [l.strip() for l in (desc or "").splitlines()
             if l.strip() and l.strip().rstrip(":").lower() not in ("description", "descriptions")]
    return lines[0] if lines else ""


def parse_args(argv):
    args = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            if i + 1 >= len(argv):
                raise SystemExit("--json precisa de um objeto JSON")
            args.update(json.loads(argv[i + 1]))
            i += 2
            continue
        if "=" not in a:
            raise SystemExit(f"Argumento invalido '{a}'. Usa key=value ou --json '{{...}}'")
        key, value = a.split("=", 1)
        try:
            args[key] = json.loads(value)
        except ValueError:
            args[key] = value
        i += 1
    return args


def print_tool_help(tool):
    print(f"{tool['name']}\n")
    print((tool.get("description") or "").strip(), "\n")
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if not props:
        print("Sem parametros.")
        return
    print("Parametros:")
    for name, p in props.items():
        kind = p.get("type", "any")
        if "enum" in p:
            kind += " " + "|".join(map(str, p["enum"]))
        flag = " (obrigatorio)" if name in required else ""
        desc = (p.get("description") or "").strip().replace("\n", " ")
        print(f"  {name}: {kind}{flag}" + (f" - {desc}" if desc else ""))


def print_result(result):
    for item in result.get("content", []):
        if item.get("type") == "text":
            text = item["text"]
            try:
                print(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
            except ValueError:
                print(text)
        else:
            print(json.dumps(item, indent=2, ensure_ascii=False))
    if "structuredContent" in result and not result.get("content"):
        print(json.dumps(result["structuredContent"], indent=2, ensure_ascii=False))
    if result.get("isError"):
        sys.exit(1)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    session = Session()
    tools = {t["name"]: t for t in session.tools()}
    if not argv:
        print(f"{len(tools)} tools (python scripts/mcp.py <tool> --help para ver parametros):\n")
        for t in tools.values():
            print(f"- {t['name']}: {summary(t.get('description'))}")
        return
    name, rest = argv[0], argv[1:]
    if name not in tools:
        raise SystemExit(f"Tool '{name}' nao existe. Disponiveis: {', '.join(tools)}")
    if "--help" in rest or "-h" in rest:
        print_tool_help(tools[name])
        return
    print_result(session.rpc("tools/call", {"name": name, "arguments": parse_args(rest)}))


if __name__ == "__main__":
    main()
