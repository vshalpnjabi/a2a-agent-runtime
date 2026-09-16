#!/usr/bin/env python3
"""
Unified A2A async send interface — used by Vi, Grok crew agents, and any peers.

POSTs {to, text} to the agent's OWN server /v1/send (or /v1/crew/{id}/send).
The server resolves the recipient through the network directory and delivers
directly to the recipient's server. Returns immediately with the message_id.
Does NOT wait for a reply (that's what the stream is for).

Usage:
  python3 a2a_send.py --to grok-alpha --text "Hello"
  python3 a2a_send.py --to grok-alpha --file /path/to/message.txt

  # As a library:
  from a2a_send import send_message
  msg_id = send_message(to="grok-alpha", text="Hello")

Config (env or args):
  A2A_SEND_URL      default http://100.76.81.125:8771/v1/send (Vi's own server)
  A2A_SEND_KEY_FILE default ~/.a2a_network_key (the agent's network key)
  A2A_PROXY         optional http proxy (tailnet from sandbox needs :3130)
"""

import argparse
import json
import os
import sys
import urllib.request

DEFAULT_URL = "http://100.76.81.125:8771/v1/send"
DEFAULT_KEY_FILE = os.path.expanduser("~/.a2a_network_key")


def _build_opener(url):
    proxy = os.environ.get("A2A_PROXY")
    if not proxy and ("100." in url or "192.168." in url or ".local" in url):
        # Tailnet from the sandbox must go through the :3130 CONNECT proxy.
        proxy = "http://hatch-egress-proxy:3130"
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def send_message(to, text, context_id=None, url=None, key_file=None):
    """
    Send an async message via the agent's own server. Returns message_id.
    Raises on failure.
    """
    url = url or os.environ.get("A2A_SEND_URL", DEFAULT_URL)
    key_file = key_file or os.environ.get("A2A_SEND_KEY_FILE", DEFAULT_KEY_FILE)

    with open(key_file) as f:
        key = f.read().strip()

    payload = {"to": to, "text": text}
    if context_id:
        payload["context_id"] = context_id

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST"
    )

    with _build_opener(url).open(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
        if not result.get("ok"):
            raise RuntimeError(f"Send failed: {result}")
        return result.get("message_id")


def main():
    parser = argparse.ArgumentParser(description="Unified A2A async send (via own server)")
    parser.add_argument("--to", required=True, help="Recipient agent id")
    parser.add_argument("--text", help="Message text")
    parser.add_argument("--file", help="Read message text from file")
    parser.add_argument("--context-id", help="Optional context ID")
    parser.add_argument("--url", help="Override send URL")
    parser.add_argument("--key-file", help="Override key file")
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
            context_id=args.context_id,
            url=args.url,
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
