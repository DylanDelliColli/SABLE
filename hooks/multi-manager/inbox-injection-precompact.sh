#!/usr/bin/env bash
# inbox-injection-precompact.sh — Clear inbox dedup files before compaction
# Trigger: PreCompact | Timeout: 2000ms
#
# After compaction the agent loses memory of inbox notifications it received
# earlier in the conversation. Clear the session's dedup files so the next
# inbox-injection re-announces any unresolved items, re-orienting the
# post-compact agent.
#
# v2 (SABLE-uz9.3): dedup files are keyed /tmp/inbox-seen-<session>-<name>
# (one per manager identity sharing the session), so clear the whole glob for
# this session. No identity gate needed — clearing dedup state is harmless and
# the main session may legitimately be anonymous in the one-window topology.

set -uo pipefail

SESSION_ID=$(python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('session_id', ''))
" 2>/dev/null) || exit 0

[ -z "$SESSION_ID" ] && exit 0

rm -f "/tmp/inbox-seen-${SESSION_ID}" "/tmp/inbox-seen-${SESSION_ID}-"* 2>/dev/null

exit 0
