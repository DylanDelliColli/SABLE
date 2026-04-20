#!/usr/bin/env bash
# inbox-injection-precompact.sh — Clear inbox dedup file before compaction
# Trigger: PreCompact | Timeout: 2000ms
#
# After compaction the agent loses memory of inbox notifications it received earlier
# in the conversation. Clear the dedup file so the next inbox-injection re-announces
# any unresolved items, re-orienting the post-compact agent.

set -euo pipefail

[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0

SESSION_ID=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('session_id', ''))
" 2>/dev/null) || exit 0

[ -z "$SESSION_ID" ] && exit 0

SEEN_FILE="/tmp/inbox-seen-${SESSION_ID}"
[ -f "$SEEN_FILE" ] && rm -f "$SEEN_FILE"

exit 0
