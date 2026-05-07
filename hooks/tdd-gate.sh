#!/usr/bin/env bash
# tdd-gate.sh — Block bd close without test evidence
# Checks the evidence file written by tdd-evidence.sh.
# Escape hatch: add [no-test] to bead notes (single-close only).

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

# Only act on bd close commands
echo "$COMMAND" | grep -q '^bd close' || exit 0

# Extract bead IDs by stripping the leading "bd close" and removing flags
# in any form: --flag=value, --flag="quoted", --flag value, --flag "quoted",
# --flag 'quoted', or bare --flag. The previous sed pipeline only handled
# the `=`-form variants; space-separated forms inflated ID_COUNT and
# silently bypassed the [no-test] escape hatch (SABLE-1n2).
BEAD_ARGS=$(BEAD_CMD="$COMMAND" python3 -c "
import os, re, shlex
cmd = re.sub(r'^bd close\s+', '', os.environ.get('BEAD_CMD', ''))
try:
    tokens = shlex.split(cmd)
except ValueError:
    tokens = []
ids = []
i = 0
while i < len(tokens):
    t = tokens[i]
    if t.startswith('--'):
        # --flag=value is a single token; consume one
        if '=' in t:
            i += 1
        # --flag value (separate tokens) — consume two if next isn't another flag
        elif i + 1 < len(tokens) and not tokens[i+1].startswith('--'):
            i += 2
        # bare --flag (boolean) — consume one
        else:
            i += 1
        continue
    ids.append(t)
    i += 1
print(' '.join(ids))
")
ID_COUNT=$(echo "$BEAD_ARGS" | wc -w)

# Single-bead close: check [no-test] escape hatch
if [ "$ID_COUNT" -eq 1 ]; then
  BEAD_ID="$BEAD_ARGS"
  # Check notes field for [no-test] marker via bd show --json
  NOTES=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if isinstance(data, list) and len(data) > 0:
    print(data[0].get('notes', '') or '')
" 2>/dev/null || echo "")
  if echo "$NOTES" | grep -q '\[no-test\]'; then
    exit 0  # Escape hatch: allow close without test evidence
  fi
fi

# Check for test evidence
EVIDENCE_FILE="/tmp/tdd-evidence-${SESSION_ID}"
if [ -s "$EVIDENCE_FILE" ]; then
  exit 0  # Tests were run this session — allow close
fi

# No evidence found — block the close
python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': 'TDD gate: No tests were run this session. Run your test suite first (npm test, pytest, etc.). For non-code beads: add [no-test] to bead notes and close individually.'
    }
}))
"
