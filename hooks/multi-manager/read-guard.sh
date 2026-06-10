#!/usr/bin/env bash
# read-guard.sh — Deny cross-inbox queries
# Trigger: PreToolUse:Bash | Timeout: 3000ms
#
# Denies `bd ready -l for-<other>` or `bd list -l for-<other>` queries where
# <other> is not the acting agent. Hard mechanical guard against role drift.
#
# Identity is resolved via lib-identity.sh (SABLE-uz9.3): agent_type from the
# hook input for subagent contexts (v2 one-window managers), env vars for
# legacy terminal launches (Chuck holdout). This also closes the old
# contamination hole — a worker subagent inside a manager terminal used to
# inherit the manager's env identity and could read its inbox; now it resolves
# as the worker type and the guard simply doesn't apply manager privileges.
#
# Allows queries against the agent's own inbox.
# Allows umbrella `coord` queries.
# Skips for non-manager identities (workers, planning agents, anonymous).
# Skips for lincoln/cockpit (cross-inbox read is their job — agents.yaml
# cross_inbox_read: true; they may NOT modify foreign inboxes, enforced via
# role prompt).

set -euo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

# shellcheck source=lib-identity.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib-identity.sh"
sable_resolve_identity "$HOOK_INPUT"

[ "$SABLE_ID_IS_MANAGER" -eq 1 ] || exit 0
[ "$SABLE_ID_NAME" = "lincoln" ] && exit 0
[ "$SABLE_ID_NAME" = "cockpit" ] && exit 0
# Seward: TEMPORARY strategist overlay (SABLE-nps) — cross-inbox read for
# status synthesis; remove this line when Seward retires (see SABLE-uz9.8).
[ "$SABLE_ID_NAME" = "seward" ] && exit 0

COMMAND=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('tool_input', {}).get('command', ''))
" 2>/dev/null) || exit 0

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

OWN_LABEL="for-${SABLE_ID_NAME}"

# Allow own inbox
[ "$QUERIED_LABEL" = "$OWN_LABEL" ] && exit 0
# Allow umbrella coord label (managers should be able to inspect coordination traffic generally)
[ "$QUERIED_LABEL" = "for-coord" ] && exit 0

# Foreign inbox query — deny
OWN_LABEL="$OWN_LABEL" QUERIED_LABEL="$QUERIED_LABEL" python3 -c "
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
"
