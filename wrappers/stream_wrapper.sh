#!/bin/bash
# A2A stream wrapper: keeps a2a_stream.py alive forever.
# Restarts on crash. Zero tokens when idle.
#
# Usage:
#   bash stream_wrapper.sh /path/to/config.json
#
# Run it via nohup or systemd for durability:
#   nohup bash stream_wrapper.sh ~/.a2a/config.json > ~/.a2a/stream.log 2>&1 &

CONFIG="${1:-$HOME/.a2a/config.json}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLIENT="$SCRIPT_DIR/../client/a2a_stream.py"

if [ ! -f "$CONFIG" ]; then
    echo "Config not found: $CONFIG" >&2
    exit 1
fi

if [ ! -f "$CLIENT" ]; then
    echo "Client not found: $CLIENT" >&2
    exit 1
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Starting A2A stream wrapper" >&2

while true; do
    python3 "$CLIENT" --config "$CONFIG"
    CODE=$?
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) Stream exited (code $CODE), restarting in 5s" >&2
    sleep 5
done
