#!/usr/bin/env python3
"""
A2A async message sender — shared client for all agents.

Sends a message via POST /v1/agents/{target}/message.
Returns immediately with the message_id. Does NOT wait for a reply
(that's what the stream is for).

Usage:
  python3 a2a_send.py --to vi --text "Hello"
  python3 a2a_send.py --to grok --text "Hello" --key-file ~/.a2a/agent_key
  python3 a2a_send.py --to vi --file /path/to/message.txt

  # As a library:
  from a2a_send import send_message
  msg_id = send_message(to="vi", text="Hello")

Config (env or args):
  A2A_SERVER_URL     — default http://100.76.81.125:8765
  A2A_SEND_KEY_FILE  — default ~/.a2a/agent_key (your agent key)
  A2A_PROXY          — optional HTTP proxy (e.g. for tailnet from sandbox)
"""

import argparse
import json
import os
import sys
import urllib.request

DEFAULT_SERVER_URL = "http://100.76.81.125:8765"
DEFAULT_KEY_FILE = os.path.expanduser("~/.a2a/agent_key")


def _build_opener(url):
    """Route tailnet IPs through the :3130 CONNECT proxy if needed."""
    proxy = os.environ.get("A2A_PROXY")
    if proxy and ("100." in url or "192.168." in url):
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def send_message(to, text, server_url=None, key_file=None):
    """
    Send an async message to another agent.
    Returns message_id. Raises on failure.
    """
    server_url = server_url or os.environ.get("A2A_SERVER_URL", DEFAULT_SERVER_URL)
    key_file = key_file or os.environ.get("A2A_SEND_KEY_FILE", DEFAULT_KEY_FILE)

    with open(key_file) as f:
        agent_key = f.read().strip()

    url = f"{server_url}/v1/agents/{to}/message"
    payload = {"text": text}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {agent_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    opener = _build_opener(server_url)
    with opener.open(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())
        if not result.get("ok", True) and result.get("error"):
            raise RuntimeError(f"Send failed: {result['error']}")
        return result.get("message_id", result.get("id", "sent"))


def main():
    parser = argparse.ArgumentParser(description="A2A async message sender")
    parser.add_argument("--to", required=True, help="Recipient agent ID")
    parser.add_argument("--text", help="Message text")
    parser.add_argument("--file", help="Read message text from file")
    parser.add_argument("--server-url", help="Override server URL")
    parser.add_argument("--key-file", help="Override agent key file path")
    parser.add_argument("--quiet", action="store_true", help="Only output message_id")
    args = parser.parse_args()

    text = args.text
    if args.file:
        with open(args.file) as f:
            text = f.read()
    if not text:
        print("Error: --text or --file required", file=sys.stderr)
        sys.exit(1)

    try:
        msg_id = send_message(
            to=args.to,
            text=text,
            server_url=args.server_url,
            key_file=args.key_file,
        )
        if args.quiet:
            print(msg_id)
        else:
            print(f"Sent to {args.to}: {msg_id}")
    except Exception as e:
        print(f"Send failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
