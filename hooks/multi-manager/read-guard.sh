#!/usr/bin/env bash
# read-guard.sh — Deny cross-inbox queries
# Trigger: PreToolUse:Bash | Timeout: 3000ms
#
# Denies `bd ready -l for-<other>` or `bd list -l for-<other>` queries where
# <other> != $CLAUDE_AGENT_NAME. Hard mechanical guard against role drift.
#
# Allows queries against the agent's own inbox.
# Allows umbrella `coord` queries.
# Skips for non-manager sessions (env var unset).

set -euo pipefail

[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
print(cmd)
" 2>/dev/null) || exit 0

COMMAND="$PARSED"
[ -z "$COMMAND" ] && exit 0

# Only act on bd ready / bd list with -l for-<name>
echo "$COMMAND" | grep -qE '^bd (ready|list)' || exit 0
echo "$COMMAND" | grep -qE '\-l[= ]for-' || exit 0

# Extract the label being queried
QUERIED_LABEL=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = re.search(r'-l[= ](for-[a-zA-Z0-9_-]+)', cmd)
print(m.group(1) if m else '')
")

[ -z "$QUERIED_LABEL" ] && exit 0

OWN_LABEL="for-${CLAUDE_AGENT_NAME}"

# Allow own inbox
[ "$QUERIED_LABEL" = "$OWN_LABEL" ] && exit 0
# Allow umbrella coord label (managers should be able to inspect coordination traffic generally)
[ "$QUERIED_LABEL" = "for-coord" ] && exit 0

# Foreign inbox query — deny
python3 -c "
import json, os
own = os.environ.get('OWN_LABEL', '')
queried = os.environ.get('QUERIED_LABEL', '')
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': f'Read guard: you are {own.replace(\"for-\", \"\")}; cannot query {queried}. Use {own} for your own inbox or /inbox slash command.'
    }
}))
" OWN_LABEL="$OWN_LABEL" QUERIED_LABEL="$QUERIED_LABEL"
