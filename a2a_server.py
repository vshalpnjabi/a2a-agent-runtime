#!/usr/bin/env python3
"""
A2A agent server — one agent's (or one crew's) own inbox, outbox, and stream.

Modes:
  --mode agent --agent-id vi
      Single agent. Endpoints:
        POST /v1/inbox            deliver a message (any registered agent's network key)
        GET  /v1/inbox?since=n    owner reads inbox (owner network key or master)
        GET  /v1/stream?since=n   owner SSE stream of new messages
        POST /v1/send              owner sends {to, text}: resolved via the directory,
                                  delivered straight to the recipient's server, logged
        GET  /v1/outbox?since=n   owner reads sent log
        GET  /v1/health

  --mode crew
      One server, many agents, path-based routing:
        /v1/crew/{agent_id}/inbox   (POST deliver / GET read)
        /v1/crew/{agent_id}/stream  (GET SSE)
        /v1/crew/{agent_id}/send    (POST owner send)
        /v1/crew/{agent_id}/outbox  (GET read)
      An {agent_id} is served here only if the directory lists it with a
      server_url + inbox_path matching this server (cached 60s). Anything else
      is a 404 — the crew server never impersonates another server's agents.

Auth (one key per agent, no send key):
  - Incoming delivery (POST inbox): bearer must be the master key or ANY
    registered agent's network_key (read from the directory's network_keys.json,
    0600, same machine).
  - Owner endpoints: bearer must be the master key or that agent's own
    network_key.

Message shape on the stream and inbox reads (unchanged from the hub era, so
existing spool/watch/hook chains keep working):
  {"task_id": ..., "from": ..., "received_at": ..., "text": ...}

Env:
  A2A_MODE {agent,crew}, A2A_AGENT_ID (agent mode), A2A_PORT, A2A_BIND
  A2A_DATA_DIR (inbox/outbox jsonl live here)
  A2A_DIRECTORY_URL (default http://127.0.0.1:8765)
  A2A_KEYS_FILE (default ~/a2a-net/directory/network_keys.json)
  A2A_MASTER_KEY_FILE (default ~/a2a/.api_key)
  A2A_PUBLIC_URL (this server's public base, e.g. http://100.76.81.125:8772;
                  used to match directory records in crew mode)
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

MODE = os.environ.get("A2A_MODE", "agent")
AGENT_ID = os.environ.get("A2A_AGENT_ID", "")
PORT = int(os.environ.get("A2A_PORT", "8771"))
BIND = os.environ.get("A2A_BIND", "0.0.0.0")
DATA_DIR = os.path.expanduser(os.environ.get("A2A_DATA_DIR", "~/a2a-net/servers/agent"))
DIRECTORY_URL = os.environ.get("A2A_DIRECTORY_URL", "http://127.0.0.1:8765").rstrip("/")
KEYS_FILE = os.path.expanduser(os.environ.get("A2A_KEYS_FILE", "~/a2a-net/directory/network_keys.json"))
MASTER_KEY_FILE = os.path.expanduser(os.environ.get("A2A_MASTER_KEY_FILE", "~/a2a/.api_key"))
PUBLIC_URL = os.environ.get("A2A_PUBLIC_URL", f"http://127.0.0.1:{PORT}").rstrip("/")

os.makedirs(DATA_DIR, exist_ok=True)

# Directory discovery cache (crew mode membership checks).
_dir_cache = {"ts": 0, "agents": {}}
_DIR_CACHE_TTL = 60


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_master_key():
    try:
        with open(MASTER_KEY_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def load_keys():
    try:
        with open(KEYS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def agent_home(agent_id):
    """Data dir for one agent (agent mode: DATA_DIR itself; crew mode: per-agent subdir)."""
    if MODE == "crew":
        d = os.path.join(DATA_DIR, "crew", agent_id)
    else:
        d = DATA_DIR
    os.makedirs(d, exist_ok=True)
    return d


def inbox_path(agent_id):
    return os.path.join(agent_home(agent_id), "inbox.jsonl")


def outbox_path(agent_id):
    return os.path.join(agent_home(agent_id), "outbox.jsonl")


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def directory_agents():
    """GET /v1/agents from the directory, cached 60s."""
    now = time.time()
    if now - _dir_cache["ts"] < _DIR_CACHE_TTL and _dir_cache["agents"]:
        return _dir_cache["agents"]
    try:
        with urllib.request.urlopen(DIRECTORY_URL + "/v1/agents", timeout=10) as resp:
            data = json.loads(resp.read().decode())
        agents = {a["agent_id"]: a for a in data.get("agents", [])}
        _dir_cache.update(ts=now, agents=agents)
        return agents
    except Exception as e:
        # Serve stale rather than fail closed on a blip; empty only if never fetched.
        print(f"directory lookup failed: {e}", flush=True)
        return _dir_cache["agents"]


def hosted_here(agent_id):
    """True if this server is the registered home of agent_id (crew mode)."""
    if MODE != "crew":
        return agent_id == AGENT_ID
    agents = directory_agents()
    a = agents.get(agent_id)
    if not a:
        return False
    server_url = (a.get("server_url") or "").rstrip("/")
    inbox_path_ = a.get("inbox_path") or ""
    return (server_url == PUBLIC_URL
            and inbox_path_.startswith("/v1/crew/")
            and inbox_path_.split("/")[3] == agent_id)


def deliver_message(agent_id, sender_id, text):
    """Append to an agent's inbox. Returns the message record."""
    text = (text or "").strip()
    if not text:
        raise ApiError(400, "text is required")
    msg = {
        "task_id": uuid.uuid4().hex,
        "from": sender_id or "unknown",
        "received_at": now_iso(),
        "text": text,
    }
    append_jsonl(inbox_path(agent_id), msg)
    return msg


def read_inbox(agent_id, since):
    msgs = read_jsonl(inbox_path(agent_id))
    total = len(msgs)
    return {"messages": msgs[since:], "offset": total}


def read_outbox(agent_id, since):
    msgs = read_jsonl(outbox_path(agent_id))
    total = len(msgs)
    return {"messages": msgs[since:], "offset": total}


def send_to_agent(owner_id, owner_key, to, text):
    """Owner send: resolve `to` via the directory, deliver directly, log to outbox."""
    text = (text or "").strip()
    if not text:
        raise ApiError(400, "text is required")
    agents = directory_agents()
    target = agents.get(to)
    message_id = uuid.uuid4().hex
    record = {
        "message_id": message_id,
        "from": owner_id,
        "to": to,
        "text": text,
        "sent_at": now_iso(),
        "delivered": False,
    }
    if not target or not target.get("server_url"):
        record["detail"] = f"unknown agent: {to}"
        append_jsonl(outbox_path(owner_id), record)
        raise ApiError(404, f"unknown agent: {to}")
    url = target["server_url"].rstrip("/") + (target.get("inbox_path") or "/v1/inbox")
    payload = json.dumps({"from": owner_id, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Authorization": f"Bearer {owner_key}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        record["delivered"] = bool(result.get("ok"))
        record["detail"] = f"delivered to {url}"
    except Exception as e:
        record["detail"] = f"delivery failed: {e}"
    append_jsonl(outbox_path(owner_id), record)
    if not record["delivered"]:
        raise ApiError(502, f"could not deliver to {to}: {record['detail']}")
    return {"ok": True, "message_id": message_id, "to": to}


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

    def _owner_ok(self, agent_id):
        if self._is_master():
            return True
        keys = load_keys()
        k = keys.get(agent_id)
        return bool(k) and self._bearer() == k

    def _sender_ok(self):
        """Master, or any registered agent's network key. Returns (ok, sender_id)."""
        if self._is_master():
            return True, None
        token = self._bearer()
        if not token:
            return False, None
        for aid, k in load_keys().items():
            if k and token == k:
                return True, aid
        return False, None

    def _resolve(self, parts):
        """Map URL parts -> (agent_id, action). Raises ApiError."""
        if MODE == "agent":
            if len(parts) != 2 or parts[0] != "v1":
                raise ApiError(404, "not found")
            if not hosted_here(AGENT_ID):
                raise ApiError(500, "server misconfigured: agent mismatch")
            return AGENT_ID, parts[1]
        # crew mode: /v1/crew/{agent_id}/{inbox|stream|send|outbox}
        if len(parts) != 4 or parts[0] != "v1" or parts[1] != "crew":
            raise ApiError(404, "not found")
        agent_id = parts[2]
        if not hosted_here(agent_id):
            raise ApiError(404, f"unknown agent: {agent_id}")
        return agent_id, parts[3]

    def _serve_stream(self, agent_id):
        query = urlparse(self.path).query
        since = 0
        for kv in query.split("&"):
            if kv.startswith("since="):
                try:
                    since = int(kv.split("=", 1)[1])
                except ValueError:
                    since = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(obj):
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            self.wfile.flush()

        emit({"type": "ready", "offset": since, "server_ts": now_iso()})
        try:
            heartbeat = 0
            while True:
                data = read_inbox(agent_id, since)
                for m in data["messages"]:
                    emit({"type": "message", "message": m, "offset": data["offset"]})
                if data["messages"]:
                    since = data["offset"]
                heartbeat += 1
                if heartbeat >= 60:  # ~30s
                    emit({"type": "ping", "ts": now_iso()})
                    heartbeat = 0
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["v1", "health"] or parts == ["health"]:
                self._send_json({"ok": True, "service": f"a2a-server({MODE})",
                                 "agent_id": AGENT_ID or None, "ts": now_iso()})
                return
            agent_id, action = self._resolve(parts)
            if not self._owner_ok(agent_id):
                self._send_json({"error": "unauthorized"}, 401)
                return
            if action == "stream":
                self._serve_stream(agent_id)
                return
            query = parsed.query
            since = 0
            for kv in query.split("&"):
                if kv.startswith("since="):
                    try:
                        since = int(kv.split("=", 1)[1])
                    except ValueError:
                        since = 0
            if action == "inbox":
                self._send_json(read_inbox(agent_id, since))
            elif action == "outbox":
                self._send_json(read_outbox(agent_id, since))
            else:
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
            agent_id, action = self._resolve(parts)
            if action == "inbox":
                ok, sender_id = self._sender_ok()
                if not ok:
                    self._send_json({"error": "unauthorized"}, 401)
                    return
                sender = sender_id or body.get("from") or "unknown"
                msg = deliver_message(agent_id, sender, body.get("text"))
                self._send_json({"ok": True, "task_id": msg["task_id"], "agent_id": agent_id})
                return
            if not self._owner_ok(agent_id):
                self._send_json({"error": "unauthorized"}, 401)
                return
            if action == "send":
                result = send_to_agent(agent_id, self._bearer(), body.get("to"), body.get("text"))
                self._send_json(result)
                return
            self._send_json({"error": "not found"}, 404)
        except ApiError as e:
            self._send_json({"error": e.message}, e.code)
        except Exception as e:
            self._send_json({"error": f"internal: {e}"}, 500)


def main():
    global MODE, AGENT_ID, PORT
    ap = argparse.ArgumentParser(description="A2A agent server (agent or crew mode)")
    ap.add_argument("--mode", choices=["agent", "crew"], default=MODE)
    ap.add_argument("--agent-id", default=AGENT_ID)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    MODE, AGENT_ID, PORT = args.mode, args.agent_id, args.port
    if MODE == "agent" and not AGENT_ID:
        print("agent mode requires --agent-id", file=sys.stderr)
        sys.exit(2)
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"a2a-server mode={MODE} agent={AGENT_ID or '-'} on {BIND}:{PORT} "
          f"(data: {DATA_DIR}, directory: {DIRECTORY_URL})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
