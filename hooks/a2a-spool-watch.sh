#!/usr/bin/env bash
# Watches Vi's A2A message spool for new messages.
# Pure shell, zero tokens when idle. The platform runs this every 5s.
# On new spool files, wakes a worker WITH the message text in the payload.
# The worker makes zero tool calls and hands TASK_ID/FROM/TEXT straight to Vi.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

SPOOL_DIR="${A2A_SPOOL_DIR:-$HOME/.a2a/spool}"
DRY_RUN="${HATCH_HOOK_DRY_RUN:-0}"

if [ ! -d "$SPOOL_DIR" ]; then
    silent "no spool dir" '{"reason":"no spool"}'
    exit 0
fi

# Find pending spool files
mapfile -t FILES < <(ls -1 "$SPOOL_DIR"/*.json 2>/dev/null | sort || true)
if [ ${#FILES[@]} -eq 0 ]; then
    silent "no pending messages" '{"reason":"empty spool"}'
    exit 0
fi

# Messages pending — build handoff text
HANDOFF=""
for f in "${FILES[@]}"; do
    TASK_ID=$(python3 -c "import json; print(json.load(open('$f')).get('task_id',''))" 2>/dev/null || echo "")
    FROM=$(python3 -c "import json; print(json.load(open('$f')).get('from',''))" 2>/dev/null || echo "")
    TEXT=$(python3 -c "import json; print(json.load(open('$f')).get('text',''))" 2>/dev/null || echo "")
    # "you" when the sender is Vishal
    if [ "$FROM" = "Vishal" ]; then FROM="you"; fi
    if [ -n "$HANDOFF" ]; then HANDOFF="$HANDOFF

"; fi
    HANDOFF="${HANDOFF}TASK_ID: $TASK_ID
FROM: $FROM
TEXT: $TEXT"
    # Remove from spool (dry runs don't touch state)
    if [ "$DRY_RUN" != "1" ]; then
        rm -f "$f"
    fi
done

payload=$(jq -n -c --arg h "$HANDOFF" '{handoff: $h}')
wake "new A2A messages" "$payload"
exit 0
