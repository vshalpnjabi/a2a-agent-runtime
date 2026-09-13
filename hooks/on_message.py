#!/usr/bin/env python3
"""
A2A message hook: invoked by a2a_stream.py with message JSON on stdin.
Writes the message to the spool directory for the waker to pick up.
Zero tokens — pure file I/O.

Spool format: {spool_dir}/{timestamp}_{task_id}.json
"""
import json
import os
import sys
import time

def main():
    try:
        msg = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # No valid message, nothing to do
        return 0

    spool_dir = os.environ.get(
        "A2A_SPOOL_DIR",
        os.path.expanduser("~/.a2a/spool")
    )
    os.makedirs(spool_dir, exist_ok=True)

    task_id = msg.get("task_id", "unknown")
    timestamp = int(time.time() * 1000)

    # Parse sender for the handoff
    text = msg.get("text", "")
    sender = msg.get("from", "unknown")
    if text.startswith("[From ") and "]" in text:
        tag_end = text.find("]")
        sender = text[6:tag_end].strip()
        text = text[tag_end + 1:].strip()

    spool_file = os.path.join(spool_dir, f"{timestamp}_{task_id}.json")
    with open(spool_file, "w") as f:
        json.dump({
            "task_id": task_id,
            "from": sender,
            "text": text,
            "received_at": msg.get("received_at", ""),
            "spooled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, f, indent=2)

    # Also output for the stream log (debugging)
    print(f"SPOOLED {task_id} from {sender}", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
