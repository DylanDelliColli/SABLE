#!/usr/bin/env bash
# pre-dispatch-preempt.sh — Block dispatch if P0 coord bead in inbox
# Trigger: PreToolUse:Agent | Timeout: 3000ms
#
# Selective preemption: when a priority=0 bead exists in the manager's inbox
# (`for-<self>`), the next dispatch is denied until the bead is resolved or
# explicitly deferred via `bd defer <id>`.
#
# Existing dispatched workers are unaffected — only the next dispatch.
#
# Escape valve: `bd defer <id> --reason="..."` removes the bead from the active
# inbox query, so dispatch resumes. Use this when stepping away.

set -euo pipefail

[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0

NESTED_AGENT_ID=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('agent_id', ''))
" 2>/dev/null) || exit 0

# Subagent context — let workers dispatch their own children freely
[ -n "$NESTED_AGENT_ID" ] && exit 0

INBOX_LABEL="for-${CLAUDE_AGENT_NAME}"

# Query inbox for P0 items
P0_BEADS=$(bd ready -l "$INBOX_LABEL" --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if not isinstance(data, list):
        sys.exit(0)
    for item in data:
        pri = item.get('priority', None)
        bid = item.get('id', '')
        title = item.get('title', '')
        if pri == 0 and bid:
            print(f'{bid}: {title}')
except Exception:
    pass
" 2>/dev/null)

[ -z "$P0_BEADS" ] && exit 0

# P0 in inbox — deny dispatch
python3 -c "
import json, os
beads = os.environ.get('P0_BEADS', '')
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': f'PREEMPTION: priority-0 coord bead(s) in your inbox blocking next dispatch:\n{beads}\n\nResolve with bd close, or defer with bd defer <id> --reason=\"...\" to unblock dispatch (use when AFK or when explicitly setting aside).'
    }
}))
" P0_BEADS="$P0_BEADS"
