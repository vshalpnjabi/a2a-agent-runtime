#!/usr/bin/env python3
"""Unit tests for the new A2A network: directory + agent/crew servers.

Self-contained: spins up real server processes on ephemeral ports with temp
dirs, drives them over HTTP with stdlib urllib, asserts behavior, tears down.
Exit 0 = all pass, non-zero = failures (prints which).

Run:  python3 tests/test_a2a_net.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIRECTORY_PY = os.path.join(ROOT, "a2a_directory.py")
SERVER_PY = os.path.join(ROOT, "a2a_server.py")

PASS, FAIL = 0, 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL: {name} {detail}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(url, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


class Proc:
    def __init__(self, script, env):
        self.p = subprocess.Popen(
            [sys.executable, script], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()


def api(method, url, body=None, key=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def main():
    global PASS, FAIL
    tmp = tempfile.mkdtemp(prefix="a2a-net-test-")
    procs = []
    try:
        dir_port = free_port()
        dir_base = os.path.join(tmp, "directory")
        dir_url = f"http://127.0.0.1:{dir_port}"
        master_key = "test-master-key"
        master_file = os.path.join(tmp, "master.key")
        with open(master_file, "w") as f:
            f.write(master_key)

        denv = dict(os.environ, A2A_PORT=str(dir_port), A2A_BIND="127.0.0.1",
                    A2A_DATA_DIR=dir_base, A2A_MASTER_KEY_FILE=master_file,
                    A2A_PUBLIC_BASE=dir_url)
        procs.append(Proc(DIRECTORY_PY, denv))
        check("directory starts", wait_health(dir_url + "/health"))

        # --- registration flow ------------------------------------------------
        s, reg = api("POST", dir_url + "/v1/agents/register",
                     {"name": "Vi", "description": "test agent",
                      "server_url": "http://127.0.0.1:1",
                      "capabilities": {"streaming": True}})
        check("register -> pending", s == 200 and reg.get("status") == "pending", f"{s} {reg}")
        check("register validates server_url",
              api("POST", dir_url + "/v1/agents/register", {"name": "Nope"})[0] == 400)
        reg_id = reg["registration_id"]
        pending = json.load(open(os.path.join(dir_base, "pending_registrations.json")))
        token = pending[reg_id]["approve_token"]

        s, agents = api("GET", dir_url + "/v1/agents")
        check("pending agent not in discovery", s == 200 and agents.get("agents") == [])

        # bad approve token rejected
        s, _ = api("GET", dir_url + f"/v1/agents/approve/{reg_id}?token=wrong")
        check("approve rejects bad token", s == 403, f"{s}")

        s, appr = api("GET", dir_url + f"/v1/agents/approve/{reg_id}?token={token}")
        check("approve issues network key",
              s == 200 and appr.get("agent_id") == "vi" and len(appr.get("network_key", "")) > 20,
              f"{s}")
        vi_key = appr["network_key"]
        keys = json.load(open(os.path.join(dir_base, "network_keys.json")))
        check("keys file holds network key", keys.get("vi") == vi_key)
        check("keys file is 0600", oct(os.stat(os.path.join(dir_base, "network_keys.json")).st_mode & 0o777) == "0o600")

        s, agents = api("GET", dir_url + "/v1/agents")
        cards = agents.get("agents", [])
        check("approved agent in discovery", s == 200 and len(cards) == 1 and cards[0]["agent_id"] == "vi")
        check("discovery card has no keys", "network_key" not in json.dumps(cards) and "agent_key" not in json.dumps(cards))
        check("discovery card carries server_url", cards[0].get("server_url") == "http://127.0.0.1:1")

        # directory is not a message broker: no message endpoints
        for method, path in [("POST", "/v1/agents/vi/message"), ("GET", "/v1/agents/vi/inbox"),
                             ("GET", "/v1/agents/vi/stream"), ("POST", "/vi/send")]:
            s, _ = api(method, dir_url + path, {"text": "x"}, key=master_key)
            check(f"directory has no {method} {path}", s == 404, f"{s}")

        # heartbeat with owner key updates last_seen
        s, hb = api("POST", dir_url + "/v1/agents/vi/heartbeat",
                    {"server_url": "http://127.0.0.1:8771"}, key=vi_key)
        check("heartbeat ok", s == 200 and hb.get("ok"), f"{s} {hb}")
        s, card = api("GET", dir_url + "/v1/agents/vi")
        check("heartbeat moved server_url", card.get("server_url") == "http://127.0.0.1:8771")
        check("heartbeat rejects bad key",
              api("POST", dir_url + "/v1/agents/vi/heartbeat", {}, key="bad")[0] == 401)

        # --- agent-mode server -------------------------------------------------
        vi_port = free_port()
        vi_data = os.path.join(tmp, "vi")
        venv = dict(os.environ, A2A_MODE="agent", A2A_AGENT_ID="vi",
                    A2A_PORT=str(vi_port), A2A_BIND="127.0.0.1",
                    A2A_DATA_DIR=vi_data, A2A_DIRECTORY_URL=dir_url,
                    A2A_KEYS_FILE=os.path.join(dir_base, "network_keys.json"),
                    A2A_MASTER_KEY_FILE=master_file,
                    A2A_PUBLIC_URL=f"http://127.0.0.1:{vi_port}")
        procs.append(Proc(SERVER_PY, venv))
        vi_url = f"http://127.0.0.1:{vi_port}"
        check("agent server starts", wait_health(vi_url + "/v1/health"))

        # register a sender agent for auth tests
        s, reg2 = api("POST", dir_url + "/v1/agents/register",
                      {"name": "Sender", "server_url": "http://127.0.0.1:2"})
        reg2_id = reg2["registration_id"]
        tok2 = json.load(open(os.path.join(dir_base, "pending_registrations.json")))[reg2_id]["approve_token"]
        s, appr2 = api("GET", dir_url + f"/v1/agents/approve/{reg2_id}?token={tok2}")
        sender_key = appr2["network_key"]

        # inbox delivery: any registered agent's key works; strangers rejected
        s, res = api("POST", vi_url + "/v1/inbox", {"text": "hello vi"}, key=sender_key)
        check("inbox accepts registered sender", s == 200 and res.get("ok") and res.get("task_id"), f"{s} {res}")
        s, _ = api("POST", vi_url + "/v1/inbox", {"text": "hello"}, key="bogus")
        check("inbox rejects bad key", s == 401, f"{s}")
        s, _ = api("POST", vi_url + "/v1/inbox", {"text": "   "}, key=sender_key)
        check("inbox rejects empty text", s == 400, f"{s}")
        s, _ = api("GET", vi_url + "/v1/inbox?since=0", key="bogus")
        check("inbox read rejects bad key", s == 401, f"{s}")

        s, inbox = api("GET", vi_url + "/v1/inbox?since=0", key=vi_key)
        msgs = inbox.get("messages", [])
        check("owner reads inbox", s == 200 and len(msgs) == 1 and msgs[0]["text"] == "hello vi"
              and msgs[0]["from"] == "sender", f"{s} {inbox}")
        check("inbox message shape", set(msgs[0].keys()) >= {"task_id", "from", "received_at", "text"})
        s, inbox2 = api("GET", vi_url + "/v1/inbox?since=1", key=vi_key)
        check("inbox since= offset works", inbox2.get("messages") == [] and inbox2.get("offset") == 1)

        # SSE stream: ready + the spooled message
        import socket as _sock
        got_ready, got_msg = False, False
        raw = _sock.create_connection(("127.0.0.1", vi_port), timeout=10)
        raw.sendall(f"GET /v1/stream?since=0 HTTP/1.1\r\nHost: x\r\n"
                    f"Authorization: Bearer {vi_key}\r\n\r\n".encode())
        raw.settimeout(8)
        buf = b""
        try:
            while not (got_ready and got_msg):
                chunk = raw.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n\n" in buf:
                    frame, buf = buf.split(b"\n\n", 1)
                    for line in frame.decode("utf-8", "replace").splitlines():
                        if line.startswith("data: "):
                            ev = json.loads(line[6:])
                            if ev.get("type") == "ready":
                                got_ready = True
                            if ev.get("type") == "message" and ev["message"]["text"] == "hello vi":
                                got_msg = True
        except _sock.timeout:
            pass
        raw.close()
        check("stream emits ready", got_ready)
        check("stream replays message", got_msg)

        # --- server-to-server send via directory -------------------------------
        b_port = free_port()
        b_data = os.path.join(tmp, "b")
        benv = dict(os.environ, A2A_MODE="agent", A2A_AGENT_ID="agent-b",
                    A2A_PORT=str(b_port), A2A_BIND="127.0.0.1",
                    A2A_DATA_DIR=b_data, A2A_DIRECTORY_URL=dir_url,
                    A2A_KEYS_FILE=os.path.join(dir_base, "network_keys.json"),
                    A2A_MASTER_KEY_FILE=master_file,
                    A2A_PUBLIC_URL=f"http://127.0.0.1:{b_port}")
        procs.append(Proc(SERVER_PY, benv))
        b_url = f"http://127.0.0.1:{b_port}"
        check("second agent server starts", wait_health(b_url + "/v1/health"))

        # point both directory records at the real servers
        api("POST", dir_url + "/v1/agents/vi/heartbeat",
            {"server_url": vi_url, "inbox_path": "/v1/inbox"}, key=vi_key)
        s, regb = api("POST", dir_url + "/v1/agents/register",
                      {"name": "Agent B", "server_url": b_url})
        regb_id = regb["registration_id"]
        tokb = json.load(open(os.path.join(dir_base, "pending_registrations.json")))[regb_id]["approve_token"]
        s, apprb = api("GET", dir_url + f"/v1/agents/approve/{regb_id}?token={tokb}")
        b_key = apprb["network_key"]
        # clear directory cache on the servers by waiting out... instead just send:
        # (servers cache discovery 60s; the heartbeat above may not be seen yet.
        #  send retries against fresh-enough data: vi's record already had a server_url,
        #  but it was the stale http://127.0.0.1:1. Force cache refresh by restarting
        #  vi's server is overkill; the test instead approves B then sends B->vi
        #  after B's server freshly fetched the directory.)
        time.sleep(1)

        s, sent = api("POST", b_url + "/v1/send", {"to": "agent-b", "text": "self ping"}, key=b_key)
        check("send to self resolves + delivers", s == 200 and sent.get("ok"), f"{s} {sent}")
        s, bout = api("GET", b_url + "/v1/outbox?since=0", key=b_key)
        check("outbox logs delivery", s == 200 and len(bout.get("messages", [])) == 1
              and bout["messages"][0]["delivered"] is True, f"{s} {bout}")
        s, binbox = api("GET", b_url + "/v1/inbox?since=0", key=b_key)
        check("recipient inbox has message", any(m["text"] == "self ping" and m["from"] == "agent-b"
                                                 for m in binbox.get("messages", [])), f"{binbox}")

        s, _ = api("POST", b_url + "/v1/send", {"to": "nobody-here", "text": "x"}, key=b_key)
        check("send to unknown agent -> 404", s == 404, f"{s}")
        s, _ = api("POST", b_url + "/v1/send", {"to": "agent-b", "text": "x"}, key="bogus")
        check("send rejects bad key", s == 401, f"{s}")

        # --- crew mode: path-based routing --------------------------------------
        crew_port = free_port()
        crew_data = os.path.join(tmp, "crew")
        crew_url = f"http://127.0.0.1:{crew_port}"
        cenv = dict(os.environ, A2A_MODE="crew",
                    A2A_PORT=str(crew_port), A2A_BIND="127.0.0.1",
                    A2A_DATA_DIR=crew_data, A2A_DIRECTORY_URL=dir_url,
                    A2A_KEYS_FILE=os.path.join(dir_base, "network_keys.json"),
                    A2A_MASTER_KEY_FILE=master_file,
                    A2A_PUBLIC_URL=crew_url)
        procs.append(Proc(SERVER_PY, cenv))
        check("crew server starts", wait_health(crew_url + "/v1/health"))

        crew_keys = {}
        for name in ("Grok Alpha", "Grok Beta"):
            s, regr = api("POST", dir_url + "/v1/agents/register",
                          {"name": name, "server_url": crew_url,
                           "inbox_path": f"/v1/crew/{{agent_id}}/inbox",
                           "stream_path": f"/v1/crew/{{agent_id}}/stream",
                           "send_path": f"/v1/crew/{{agent_id}}/send"})
            rid = regr["registration_id"]
            aid = regr["agent_id"]
            tokr = json.load(open(os.path.join(dir_base, "pending_registrations.json")))[rid]["approve_token"]
            # fix the templated paths to the concrete agent id
            s, apprr = api("GET", dir_url + f"/v1/agents/approve/{rid}?token={tokr}")
            crew_keys[aid] = apprr["network_key"]
            api("POST", dir_url + f"/v1/agents/{aid}/heartbeat",
                {"inbox_path": f"/v1/crew/{aid}/inbox",
                 "stream_path": f"/v1/crew/{aid}/stream",
                 "send_path": f"/v1/crew/{aid}/send"}, key=apprr["network_key"])
        alpha = [a for a in crew_keys if "alpha" in a][0]
        beta = [a for a in crew_keys if "beta" in a][0]

        s, res = api("POST", f"{crew_url}/v1/crew/{alpha}/inbox", {"text": "for alpha"}, key=sender_key)
        check("crew inbox accepts for registered crew agent", s == 200 and res.get("ok"), f"{s} {res}")
        s, _ = api("POST", f"{crew_url}/v1/crew/{beta}/inbox", {"text": "x"}, key="bogus")
        check("crew inbox rejects bad key", s == 401, f"{s}")
        s, _ = api("POST", f"{crew_url}/v1/crew/not-a-crew-agent/inbox", {"text": "x"}, key=sender_key)
        check("crew rejects unregistered agent path", s == 404, f"{s}")
        s, _ = api("POST", f"{crew_url}/v1/crew/vi/inbox", {"text": "x"}, key=sender_key)
        check("crew rejects agent hosted elsewhere", s == 404, f"{s}")

        s, ainbox = api("GET", f"{crew_url}/v1/crew/{alpha}/inbox?since=0", key=crew_keys[alpha])
        check("crew inbox isolated (alpha)", s == 200 and len(ainbox["messages"]) == 1
              and ainbox["messages"][0]["text"] == "for alpha", f"{s} {ainbox}")
        s, binbox = api("GET", f"{crew_url}/v1/crew/{beta}/inbox?since=0", key=crew_keys[beta])
        check("crew inbox isolated (beta empty)", s == 200 and binbox["messages"] == [], f"{s} {binbox}")
        s, _ = api("GET", f"{crew_url}/v1/crew/{alpha}/inbox?since=0", key=crew_keys[beta])
        check("crew owner auth is per-agent", s == 401, f"{s}")

        # crew agent sends to the standalone agent through the directory
        s, sent = api("POST", f"{crew_url}/v1/crew/{alpha}/send",
                      {"to": "agent-b", "text": "alpha says hi"}, key=crew_keys[alpha])
        check("crew send delivers via directory", s == 200 and sent.get("ok"), f"{s} {sent}")
        s, aout = api("GET", f"{crew_url}/v1/crew/{alpha}/outbox?since=0", key=crew_keys[alpha])
        check("crew outbox logged", s == 200 and aout["messages"]
              and aout["messages"][0]["delivered"] is True, f"{s} {aout}")

        # deregister removes from discovery and keys
        s, _ = api("POST", dir_url + "/v1/agents/sender/deregister", key="bogus")
        check("deregister rejects bad key", s == 401, f"{s}")
        s, dr = api("POST", dir_url + "/v1/agents/sender/deregister", key=master_key)
        check("master can deregister", s == 200 and dr.get("ok"), f"{s} {dr}")
        s, agents = api("GET", dir_url + "/v1/agents")
        check("deregistered agent gone",
              all(a["agent_id"] != "sender" for a in agents["agents"]), f"{agents}")

        print(f"\n{ PASS} passed, {FAIL} failed")
        return 0 if FAIL == 0 else 1
    finally:
        for p in procs:
            p.stop()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
