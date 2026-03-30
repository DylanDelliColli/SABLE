#!/usr/bin/env bash
# tdd-evidence.sh — Silent logger for test command evidence
# Fires on every PreToolUse:Bash, writes only when it detects test commands.
# Partner to tdd-gate.sh which checks the evidence file on bd close.

set -euo pipefail

# Read stdin and parse with python3 (jq not available)
PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
sid = d.get('session_id', '')
print(f'{sid}\n{cmd}')
" 2>/dev/null) || exit 0

SESSION_ID=$(echo "$PARSED" | sed -n '1p')
COMMAND=$(echo "$PARSED" | sed -n '2p')

[ -z "$COMMAND" ] && exit 0
[ -z "$SESSION_ID" ] && exit 0

# Match test runner commands
if echo "$COMMAND" | grep -qE '(vitest|pytest|npm test|npx vitest|python -m pytest)'; then
  EVIDENCE_FILE="/tmp/tdd-evidence-${SESSION_ID}"
  echo "$(date -Iseconds) $COMMAND" >> "$EVIDENCE_FILE"
fi

exit 0
