"""OAuth helper for the Connected Home MCP server (Claude Code).

The hackathon Keycloak only accepts redirects to
http://mcp-redirect.futurelabsinnovations.com:<port>/callback (resolves to 127.0.0.1),
while Claude Code's built-in OAuth always redirects to localhost. This script does
the Authorization Code + PKCE flow itself and is wired into .mcp.json as a
headersHelper, so Claude Code receives a fresh bearer token on every connect.

Usage:
    python scripts/connected_home_auth.py login     # once: opens the browser, sign in with the team account
    python scripts/connected_home_auth.py headers   # used by Claude Code (prints JSON headers)
    python scripts/connected_home_auth.py tools     # list the server's MCP tools (-v for full JSON schemas)
    python scripts/connected_home_auth.py logout    # forget the stored tokens

Config (environment variables or the repo's .env):
    CONNECTED_HOME_API_KEY        API key given by the organisation (sent as X-API-Key if set)
    CONNECTED_HOME_MCP_URL        default https://cognitive-home.futurelabsinnovations.com
    CONNECTED_HOME_CALLBACK_PORT  default 5434 (change if the port is busy)
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = Path.home() / ".connected-home" / "tokens.json"
CLIENT_ID = "mcp-client"
REDIRECT_HOST = "mcp-redirect.futurelabsinnovations.com"


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
MCP_URL = os.environ.get("CONNECTED_HOME_MCP_URL", "https://cognitive-home.futurelabsinnovations.com").rstrip("/")
API_KEY = os.environ.get("CONNECTED_HOME_API_KEY", "")
PORT = int(os.environ.get("CONNECTED_HOME_CALLBACK_PORT", "5434"))
REDIRECT_URI = f"http://{REDIRECT_HOST}:{PORT}/callback"


def get_json(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    # Cloudflare (error 1010) blocks the default Python-urllib User-Agent.
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json", "User-Agent": "connected-home-mcp/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{url} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def oidc_config():
    meta = get_json(f"{MCP_URL}/.well-known/oauth-protected-resource")
    issuer = meta["authorization_servers"][0]
    return get_json(f"{issuer}/.well-known/openid-configuration")


def save_tokens(tok):
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 300))
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tok), encoding="utf-8")


def login():
    conf = oidc_config()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    auth_url = conf["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "openid",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in q.items()})
            ok = "code" in result and result.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Login concluido. Podes fechar esta janela." if ok else f"Erro no login: {result}"
            self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"A abrir o browser para login (se nao abrir, visita):\n{auth_url}\n", flush=True)
    webbrowser.open(auth_url)
    server.timeout = 10
    deadline = time.time() + 300
    while "code" not in result and "error" not in result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if result.get("state") != state or "code" not in result:
        raise SystemExit(f"Login falhou: {result or 'timeout'}")
    tok = get_json(conf["token_endpoint"], {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": result["code"],
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })
    save_tokens(tok)
    print(f"Login OK. Tokens guardados em {TOKEN_FILE}")


def access_token():
    if not TOKEN_FILE.exists():
        raise SystemExit("Sem tokens. Corre: python scripts/connected_home_auth.py login")
    tok = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if tok["expires_at"] - time.time() > 60:
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise SystemExit("Token expirado. Corre: python scripts/connected_home_auth.py login")
    new = get_json(oidc_config()["token_endpoint"], {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": tok["refresh_token"],
    })
    new.setdefault("refresh_token", tok["refresh_token"])
    save_tokens(new)
    return new["access_token"]


def headers():
    out = {"Authorization": f"Bearer {access_token()}", "X-Client-Type": "core"}
    if API_KEY:
        out["X-API-Key"] = API_KEY
    print(json.dumps(out))


def mcp_request(payload, session_id=None):
    """POST a JSON-RPC message to the MCP server (Streamable HTTP). Returns (json_or_None, session_id)."""
    hdrs = {
        "Authorization": f"Bearer {access_token()}",
        "X-Client-Type": "core",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "connected-home-mcp/1.0",
    }
    if API_KEY:
        hdrs["X-API-Key"] = API_KEY
    if session_id:
        hdrs["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            sid = resp.headers.get("Mcp-Session-Id") or session_id
            body = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{MCP_URL} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    if not body.strip():
        return None, sid
    if "text/event-stream" in ctype:
        # Take the last SSE "data:" payload that is a JSON-RPC response.
        msg = None
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if "result" in data or "error" in data:
                    msg = data
        return msg, sid
    return json.loads(body), sid


def list_tools(verbose=False):
    init, sid = mcp_request({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "connected-home-cli", "version": "1.0"},
        },
    })
    if init and "error" in init:
        raise SystemExit(f"initialize falhou: {init['error']}")
    mcp_request({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    tools, cursor, req_id = [], None, 2
    while True:
        params = {"cursor": cursor} if cursor else {}
        res, sid = mcp_request({"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": params}, sid)
        if not res or "error" in res:
            raise SystemExit(f"tools/list falhou: {res and res.get('error')}")
        tools += res["result"].get("tools", [])
        cursor = res["result"].get("nextCursor")
        req_id += 1
        if not cursor:
            break
    if verbose:
        print(json.dumps(tools, indent=2, ensure_ascii=False))
        return
    print(f"{len(tools)} tools:\n")
    for t in tools:
        desc = [l.strip() for l in (t.get("description") or "").splitlines()
                if l.strip() and l.strip().rstrip(":").lower() not in ("description", "descriptions")]
        print(f"- {t['name']}" + (f": {desc[0]}" if desc else ""))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "headers"
    if cmd == "login":
        login()
    elif cmd == "headers":
        headers()
    elif cmd == "tools":
        list_tools(verbose="-v" in sys.argv[2:])
    elif cmd == "logout":
        TOKEN_FILE.unlink(missing_ok=True)
        print("Tokens removidos.")
    else:
        raise SystemExit(__doc__)
