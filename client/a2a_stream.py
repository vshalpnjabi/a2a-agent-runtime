#!/usr/bin/env python3
"""
Unified A2A SSE stream client — used by BOTH Vi and Grok (and any future peers).

One code path, one streaming dialect. Config-driven:
  python3 a2a_stream.py --config <name>.json

Config schema:
{
  "stream_url": "http://127.0.0.1:8765/vi/inbox/stream",
  "api_key_file": "/path/to/.api_key",
  "offset_file": "/path/to/.offset",
  "peer_filter": "grok",           // optional: only deliver messages where 'to' matches
  "hook": "/path/to/on_message.sh", // invoked with message JSON on stdin
  "reconnect_interval": 60,         // seconds before proactive reconnect
  "ping_timeout": 75,               // seconds without ping/event before stale reconnect
  "backoff_max": 30
}

Zero tokens when idle. The hook is the ONLY thing that runs on message.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

# Tailnet IPs must go through the :3130 CONNECT proxy (not the default :3128)
# See ~/TOOLS.md for details.
_TAILNET_PROXY = "http://hatch-egress-proxy:3130"

def _build_opener(url):
    """Build a urllib opener that routes tailnet IPs through the :3130 proxy."""
    if "100." in url or "192.168." in url or ".local" in url:
        proxy = urllib.request.ProxyHandler({
            "http": _TAILNET_PROXY,
            "https": _TAILNET_PROXY,
        })
        return urllib.request.build_opener(proxy)
    return urllib.request.build_opener()

def load_config(path):
    with open(path) as f:
        config = json.load(f)
    # Expand env vars ($VAR, ${VAR}) and ~ in all string values.
    # Host-specific paths live in env, so one config works on any host.
    def expand(v):
        if isinstance(v, str):
            return os.path.expandvars(os.path.expanduser(v))
        if isinstance(v, dict):
            return {k: expand(x) for k, x in v.items()}
        if isinstance(v, list):
            return [expand(x) for x in v]
        return v
    return expand(config)

def get_key(key_file):
    with open(key_file) as f:
        return f.read().strip()

def get_offset(offset_file):
    try:
        with open(offset_file) as f:
            return int(f.read().strip())
    except:
        return 0

def save_offset(offset_file, n):
    # Atomic write
    tmp = offset_file + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(n))
    os.rename(tmp, offset_file)

def run_hook(hook_path, message):
    """Invoke hook with message JSON on stdin. Hook output goes to our stdout."""
    try:
        # Don't capture output — hook's TASK_ID/FROM/TEXT must reach the wrapper
        proc = subprocess.run(
            [hook_path],
            input=json.dumps(message).encode(),
            timeout=30
        )
        return proc.returncode == 0
    except Exception as e:
        print(f"Hook failed: {e}", file=sys.stderr)
        return False

def stream_forever(config):
    stream_url = config["stream_url"]
    key = get_key(config["api_key_file"])
    offset_file = config["offset_file"]
    peer_filter = config.get("peer_filter")
    hook = config.get("hook")
    reconnect_interval = config.get("reconnect_interval", 60)
    ping_timeout = config.get("ping_timeout", 75)
    backoff_max = config.get("backoff_max", 30)
    exit_on_message = config.get("exit_on_message", False)

    offset = get_offset(offset_file)
    backoff = 1

    while True:
        try:
            url = f"{stream_url}?since={offset}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            last_activity = time.time()
            connect_time = time.time()

            opener = _build_opener(stream_url)
            with opener.open(req, timeout=70) as resp:
                buffer = ""
                backoff = 1  # reset on successful connect

                for chunk in resp:
                    now = time.time()
                    # Proactive reconnect to avoid stale connections
                    if now - connect_time > reconnect_interval:
                        break
                    # Staleness: no ping/event for too long
                    if now - last_activity > ping_timeout:
                        print("Stale stream, reconnecting", file=sys.stderr)
                        break

                    buffer += chunk.decode("utf-8", errors="ignore")
                    while "\n\n" in buffer:
                        event, buffer = buffer.split("\n\n", 1)
                        for line in event.split("\n"):
                            if line.startswith("data: "):
                                try:
                                    data = json.loads(line[6:])
                                    etype = data.get("type")
                                    if etype == "ready":
                                        offset = max(offset, data.get("offset", offset))
                                        save_offset(offset_file, offset)
                                        last_activity = now
                                    elif etype == "ping":
                                        last_activity = now
                                    elif etype == "message":
                                        msg = data.get("message", {})
                                        new_offset = data.get("offset", offset + 1)
                                        # Peer filter: only deliver if 'to' matches (or no filter)
                                        to = msg.get("to", "")
                                        if not peer_filter or not to or to.lower() == peer_filter.lower():
                                            # Save offset FIRST, before hook/exit, so we never replay
                                            offset = max(offset, new_offset)
                                            save_offset(offset_file, offset)
                                            if hook:
                                                hook_ok = run_hook(hook, msg)
                                                if not hook_ok:
                                                    print("Hook failed, offset already saved", file=sys.stderr)
                                            if exit_on_message:
                                                # For agents woken by process exit (e.g. Vi):
                                                # hook already output the message, exit now
                                                sys.exit(0)
                                        else:
                                            offset = max(offset, new_offset)
                                            save_offset(offset_file, offset)
                                        last_activity = now
                                except json.JSONDecodeError:
                                    pass
        except Exception as e:
            print(f"Stream error: {e}", file=sys.stderr)

        time.sleep(backoff)
        backoff = min(backoff * 2, backoff_max)

def main():
    parser = argparse.ArgumentParser(description="Unified A2A SSE stream client")
    parser.add_argument("--config", required=True, help="Path to JSON config file")
    args = parser.parse_args()
    config = load_config(args.config)
    stream_forever(config)

if __name__ == "__main__":
    main()
