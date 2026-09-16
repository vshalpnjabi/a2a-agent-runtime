#!/usr/bin/env python3
"""
A2A network directory — the centralized agent network on the Mac mini.

This is a DIRECTORY, not a message broker. It knows who is on the network and
where their server lives; it never stores, relays, or streams messages.

Endpoints:
  GET  /v1/agents                       public discovery (no keys)
  GET  /v1/agents/{id}                  single agent card
  POST /v1/agents/register              public: request to join -> PENDING + approval email
  GET  /v1/agents/approve/{reg_id}?token=   one-click approve (email link)
  GET  /v1/agents/deny/{reg_id}?token=      one-click deny (email link)
  POST /v1/agents/{id}/deregister       master key or the agent's own network key
  POST /v1/agents/{id}/heartbeat        agent's own network key: refresh last_seen/server_url
  GET  /health
  GET  /                               tiny network status page

Key model (one key per agent, no send key):
  - master key: Vishal only, lives on the Mini (A2A_MASTER_KEY_FILE).
  - network_key: issued per agent at approval. The agent uses it to
    authenticate to other agents' servers when sending, and to manage its own
    registration (heartbeat/deregister). Agent servers on this machine validate
    senders against network_keys.json (0600).

An agent's discovery card:
  {
    "agent_id": ..., "name": ..., "description": ...,
    "server_url": "http://100.76.81.125:8771",   # where its A2A server lives
    "inbox_path": "/v1/inbox",                   # POST here to deliver a message
    "stream_path": "/v1/stream",
    "send_path": "/v1/send",
    "capabilities": {...},
    "registered_at": ..., "last_seen": ...
  }
  Crew agents use path-based routing, e.g. server_url http://...:8772 with
  inbox_path /v1/crew/{agent_id}/inbox.

Env:
  A2A_PORT (default 8765), A2A_BIND (default 0.0.0.0)
  A2A_DATA_DIR (default ~/a2a-net/directory)
  A2A_MASTER_KEY_FILE (default ~/a2a/.api_key)
  A2A_PUBLIC_BASE (default http://100.76.81.125:8765)  # used in approval links
  A2A_SMTP_FILE (default <data_dir>/.smtp.json, fallback ~/a2a/.smtp.json)
"""

import json
import os
import re
import secrets
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.expanduser(os.environ.get("A2A_DATA_DIR", "~/a2a-net/directory"))
PORT = int(os.environ.get("A2A_PORT", "8765"))
BIND = os.environ.get("A2A_BIND", "0.0.0.0")
MASTER_KEY_FILE = os.path.expanduser(os.environ.get("A2A_MASTER_KEY_FILE", "~/a2a/.api_key"))
PUBLIC_BASE = os.environ.get("A2A_PUBLIC_BASE", "http://100.76.81.125:8765").rstrip("/")
SMTP_FILE = os.path.expanduser(os.environ.get(
    "A2A_SMTP_FILE", os.path.join(BASE, ".smtp.json")))
SMTP_FALLBACK = os.path.expanduser("~/a2a/.smtp.json")

AGENTS_FILE = os.path.join(BASE, "agents.json")
PENDING_FILE = os.path.join(BASE, "pending_registrations.json")
KEYS_FILE = os.path.join(BASE, "network_keys.json")

os.makedirs(BASE, exist_ok=True)


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj, mode=0o644):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.chmod(tmp, mode)
    os.rename(tmp, path)


def load_master_key():
    try:
        with open(MASTER_KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def load_agents():
    return read_json(AGENTS_FILE, default={}) or {}


def save_agents(agents):
    write_json(AGENTS_FILE, agents)


def load_pending():
    return read_json(PENDING_FILE, default={}) or {}


def save_pending(pending):
    write_json(PENDING_FILE, pending)


def load_keys():
    return read_json(KEYS_FILE, default={}) or {}


def save_keys(keys):
    write_json(KEYS_FILE, keys, mode=0o600)


def send_approval_email(reg_id, req):
    """Email Vishal the one-click approve/deny links. Master key stays on the Mini."""
    smtp_path = SMTP_FILE if os.path.exists(SMTP_FILE) else SMTP_FALLBACK
    if not os.path.exists(smtp_path):
        print(f"SMTP not configured ({smtp_path} missing), skipping email", flush=True)
        return False
    try:
        with open(smtp_path) as f:
            cfg = json.load(f)
        to_email = cfg.get("to", "")
        from_email = cfg.get("from", "")
        subject = f"A2A network: new agent registration - {req['name']}"
        body = f"""New agent wants to join your A2A network.

Name: {req['name']}
Agent ID: {req['agent_id']}
Description: {req['description']}
Server URL: {req['server_url']}
Inbox path: {req['inbox_path']}
Requested: {req['requested_at']}
Registration ID: {reg_id}

APPROVE: {PUBLIC_BASE}/v1/agents/approve/{reg_id}?token={req['approve_token']}

DENY: {PUBLIC_BASE}/v1/agents/deny/{reg_id}?token={req['deny_token']}

(One-click links. Master key stays on the Mini. The directory stores no messages.)
"""
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email
        with smtplib.SMTP(cfg.get("host", "smtp.gmail.com"), cfg.get("port", 587)) as server:
            server.starttls()
            server.login(cfg.get("username", ""), cfg.get("password", ""))
            server.send_message(msg)
        print(f"Approval email sent for {req['name']} ({reg_id})", flush=True)
        return True
    except Exception as e:
        print(f"Failed to send approval email: {e}", flush=True)
        return False


def agent_card(agent_id, a):
    """Public discovery card. Never includes keys."""
    return {
        "agent_id": agent_id,
        "name": a.get("name", ""),
        "description": a.get("description", ""),
        "server_url": a.get("server_url", ""),
        "inbox_path": a.get("inbox_path", "/v1/inbox"),
        "stream_path": a.get("stream_path", "/v1/stream"),
        "send_path": a.get("send_path", "/v1/send"),
        "capabilities": a.get("capabilities", {}),
        "registered_at": a.get("registered_at"),
        "last_seen": a.get("last_seen"),
    }


def register_agent(body):
    name = (body.get("name") or "").strip()
    server_url = (body.get("server_url") or "").strip().rstrip("/")
    if not name:
        raise ApiError(400, "name is required")
    if not server_url:
        raise ApiError(400, "server_url is required (where this agent's A2A server lives)")
    agents = load_agents()
    pending = load_pending()
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"
    agent_id = slug
    if agent_id in agents or agent_id in pending:
        agent_id = f"{slug}-{secrets.token_hex(3)}"
    reg_id = secrets.token_urlsafe(16)
    pending[reg_id] = {
        "agent_id": agent_id,
        "name": name,
        "description": body.get("description", ""),
        "server_url": server_url,
        "inbox_path": body.get("inbox_path", "/v1/inbox"),
        "stream_path": body.get("stream_path", "/v1/stream"),
        "send_path": body.get("send_path", "/v1/send"),
        "capabilities": body.get("capabilities", {}),
        "requested_at": now_iso(),
        "status": "pending",
        "approve_token": secrets.token_urlsafe(32),
        "deny_token": secrets.token_urlsafe(32),
    }
    save_pending(pending)
    send_approval_email(reg_id, pending[reg_id])
    return {"registration_id": reg_id, "agent_id": agent_id, "status": "pending",
            "message": "Registration pending Vishal's approval. Your network key is issued on approval."}


def approve_registration(reg_id):
    pending = load_pending()
    agents = load_agents()
    keys = load_keys()
    if reg_id not in pending:
        raise ApiError(404, "registration not found")
    req = pending[reg_id]
    if req["status"] != "pending":
        raise ApiError(409, f"already {req['status']}")
    agent_id = req["agent_id"]
    network_key = secrets.token_urlsafe(32)
    agents[agent_id] = {
        "name": req["name"],
        "description": req["description"],
        "server_url": req["server_url"],
        "inbox_path": req["inbox_path"],
        "stream_path": req["stream_path"],
        "send_path": req["send_path"],
        "capabilities": req["capabilities"],
        "registered_at": now_iso(),
        "last_seen": now_iso(),
    }
    keys[agent_id] = network_key
    save_agents(agents)
    save_keys(keys)
    # Save the key where the agent's operator can pick it up (Mini-local).
    agent_dir = os.path.join(BASE, "agents", agent_id)
    os.makedirs(agent_dir, exist_ok=True)
    with open(os.path.join(agent_dir, ".keys"), "w") as f:
        json.dump({"network_key": network_key}, f, indent=2)
    os.chmod(os.path.join(agent_dir, ".keys"), 0o600)
    req["status"] = "approved"
    req["approved_at"] = now_iso()
    save_pending(pending)
    return {"agent_id": agent_id, "network_key": network_key}


def deny_registration(reg_id):
    pending = load_pending()
    if reg_id not in pending:
        raise ApiError(404, "registration not found")
    pending[reg_id]["status"] = "denied"
    pending[reg_id]["denied_at"] = now_iso()
    save_pending(pending)
    return {"status": "denied"}


def deregister_agent(agent_id):
    agents = load_agents()
    if agent_id not in agents:
        raise ApiError(404, f"unknown agent: {agent_id}")
    del agents[agent_id]
    save_agents(agents)
    keys = load_keys()
    keys.pop(agent_id, None)
    save_keys(keys)
    return {"ok": True, "agent_id": agent_id}


def heartbeat(agent_id, body):
    agents = load_agents()
    if agent_id not in agents:
        raise ApiError(404, f"unknown agent: {agent_id}")
    a = agents[agent_id]
    if body.get("server_url"):
        a["server_url"] = body["server_url"].rstrip("/")
    for p in ("inbox_path", "stream_path", "send_path"):
        if body.get(p):
            a[p] = body[p]
    a["last_seen"] = now_iso()
    save_agents(agents)
    return {"ok": True, "agent_id": agent_id, "last_seen": a["last_seen"]}


STATUS_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>A2A network</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:2em auto;padding:0 1em}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.4em .6em;text-align:left}
code{background:#f4f4f4;padding:.1em .3em}</style></head><body>
<h1>A2A network directory</h1>
<p>This is a directory, not a message broker. Agents register here, discover each
other here, and send messages directly to each other's servers.</p>
<h2>Registered agents</h2>
<table><tr><th>agent</th><th>server</th><th>last seen</th></tr>{rows}</table>
<h2>Joining</h2>
<p><code>POST /v1/agents/register</code> with
<code>{{"name": ..., "description": ..., "server_url": ...}}</code>.
Vishal approves via email; your network key is issued on approval.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (now_iso(), fmt % args))
        sys.stdout.flush()

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}

    def _bearer(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def _is_master(self):
        key = load_master_key()
        return bool(key) and self._bearer() == key

    def _is_owner(self, agent_id):
        if self._is_master():
            return True
        keys = load_keys()
        return bool(keys.get(agent_id)) and self._bearer() == keys[agent_id]

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            qs = parse_qs(parsed.query)
            if not parts:
                agents = load_agents()
                rows = "".join(
                    f"<tr><td><code>{aid}</code></td><td><code>{a.get('server_url','')}</code></td>"
                    f"<td>{a.get('last_seen','')}</td></tr>"
                    for aid, a in sorted(agents.items()))
                body = STATUS_PAGE.format(rows=rows or "<tr><td colspan=3>(none)</td></tr>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parts == ["health"]:
                self._send_json({"ok": True, "service": "a2a-directory", "ts": now_iso()})
                return
            if parts[:2] == ["v1", "agents"]:
                rest = parts[2:]
                if not rest:
                    agents = load_agents()
                    self._send_json({"agents": [agent_card(aid, a) for aid, a in sorted(agents.items())]})
                    return
                if rest[0] == "approve" and len(rest) == 2:
                    token = (qs.get("token") or [""])[0]
                    pending = load_pending()
                    req = pending.get(rest[1])
                    if not req or req.get("approve_token") != token:
                        self._send_json({"error": "invalid token"}, 403)
                        return
                    result = approve_registration(rest[1])
                    # Show the key once, in the browser only. Never logged.
                    self._send_json({"ok": True, "agent_id": result["agent_id"],
                                     "network_key": result["network_key"],
                                     "note": "Copy this key now. It is also saved on the Mini."})
                    return
                if rest[0] == "deny" and len(rest) == 2:
                    token = (qs.get("token") or [""])[0]
                    pending = load_pending()
                    req = pending.get(rest[1])
                    if not req or req.get("deny_token") != token:
                        self._send_json({"error": "invalid token"}, 403)
                        return
                    self._send_json(deny_registration(rest[1]))
                    return
                if len(rest) == 1:
                    agents = load_agents()
                    if rest[0] not in agents:
                        self._send_json({"error": f"unknown agent: {rest[0]}"}, 404)
                        return
                    self._send_json(agent_card(rest[0], agents[rest[0]]))
                    return
            self._send_json({"error": "not found"}, 404)
        except ApiError as e:
            self._send_json({"error": e.message}, e.code)
        except Exception as e:
            self._send_json({"error": f"internal: {e}"}, 500)

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            body = self._read_body()
            if parts[:2] == ["v1", "agents"]:
                rest = parts[2:]
                if rest == ["register"]:
                    self._send_json(register_agent(body))
                    return
                if len(rest) == 2 and rest[1] == "deregister":
                    if not self._is_owner(rest[0]):
                        self._send_json({"error": "unauthorized"}, 401)
                        return
                    self._send_json(deregister_agent(rest[0]))
                    return
                if len(rest) == 2 and rest[1] == "heartbeat":
                    if not self._is_owner(rest[0]):
                        self._send_json({"error": "unauthorized"}, 401)
                        return
                    self._send_json(heartbeat(rest[0], body))
                    return
            self._send_json({"error": "not found"}, 404)
        except ApiError as e:
            self._send_json({"error": e.message}, e.code)
        except Exception as e:
            self._send_json({"error": f"internal: {e}"}, 500)


def main():
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"a2a-directory on {BIND}:{PORT} (data: {BASE})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
