#!/bin/bash
# A2A wake check: run by Hatch cron every 30 seconds.
# Checks the message spool. If empty, exits silently (zero tokens).
# If messages are pending, outputs them as a handoff to the agent.
#
# Setup as a Hatch cron:
#   Name: a2a-wake-check
#   Schedule: every 30 seconds (or */30 * * * * * — check your Hatch cron syntax)
#   Command: bash /path/to/a2a-agent-runtime/hatch/wake_check.sh
#
# The cron worker should be configured to only wake the main agent
# when this script produces output.

SPOOL_DIR="${A2A_SPOOL_DIR:-$HOME/.a2a/spool}"

if [ ! -d "$SPOOL_DIR" ]; then
    exit 0
fi

# Get pending spool files (sorted by timestamp)
FILES=$(ls -1 "$SPOOL_DIR"/*.json 2>/dev/null | sort)
if [ -z "$FILES" ]; then
    # No messages — stay silent, zero tokens
    exit 0
fi

# Messages pending — output them for the handoff
echo "A2A_MESSAGES_PENDING"
echo "==================="
for f in $FILES; do
    TASK_ID=$(python3 -c "import json; print(json.load(open('$f')).get('task_id', ''))" 2>/dev/null)
    FROM=$(python3 -c "import json; print(json.load(open('$f')).get('from', ''))" 2>/dev/null)
    TEXT=$(python3 -c "import json; print(json.load(open('$f')).get('text', ''))" 2>/dev/null)
    echo ""
    echo "TASK_ID=$TASK_ID"
    echo "FROM=$FROM"
    echo "TEXT=$TEXT"
    echo "---"
    # Remove from spool after outputting (at-least-once delivery;
    # the agent should handle duplicates gracefully)
    rm -f "$f"
done
