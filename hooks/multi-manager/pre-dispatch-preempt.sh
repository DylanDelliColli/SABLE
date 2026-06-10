#!/usr/bin/env bash
# pre-dispatch-preempt.sh — Block dispatch if P0 coord bead in the lane's inbox
# Trigger: PreToolUse:Agent | Timeout: 3000ms
#
# Selective preemption: when a priority=0 bead exists in the dispatch lane's
# inbox (`for-<lane>`), the next dispatch is denied until the bead is resolved
# or explicitly deferred (`bd defer <id>` — record the reason first via
# bd update --notes; defer has no --reason flag).
#
# Existing dispatched workers are unaffected — only the next dispatch.
#
# Lane resolution via lib-identity.sh sable_resolve_dispatch_lane
# (SABLE-uz9.3/uz9.4 option A): legacy manager terminals govern their own
# lane; the v2 one-window main session governs the lane named by the
# "Dispatching-for: <manager>" prompt line (default cockpit) and only while
# the cockpit is in execution mode.

set -euo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

# shellcheck source=lib-identity.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib-identity.sh"
sable_resolve_dispatch_lane "$HOOK_INPUT"

[ "$SABLE_DISPATCH_ACTIVE" -eq 1 ] || exit 0

INBOX_LABEL="for-${SABLE_DISPATCH_LANE}"

# Query the lane's inbox for P0 items
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
P0_BEADS="$P0_BEADS" LANE="$SABLE_DISPATCH_LANE" python3 -c "
import json, os
beads = os.environ.get('P0_BEADS', '')
lane = os.environ.get('LANE', '')
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': f'PREEMPTION ({lane}): priority-0 coord bead(s) in the lane inbox blocking next dispatch:\n{beads}\n\nResolve with bd close, or set aside with: bd update <id> --notes \"deferred: <reason>\" (notes overwrites — fetch and append) then bd defer <id>. Use when AFK or explicitly deferring.'
    }
}))
"
