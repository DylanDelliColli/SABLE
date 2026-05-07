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

# Extract bead IDs from the close command. Strategy: shlex-tokenize the
# string, then keep only tokens that match the bead-ID shape
# (PREFIX-suffix or PREFIX-suffix.N, with uppercase prefix + lowercase
# alphanumeric suffix). This naturally excludes flags (--reason, --json),
# flag values (text after a flag), pipes (|), redirects (2>&1, > file),
# and command chains (&&, ||, ;) — none of those tokens look like a
# bead ID, so they don't inflate ID_COUNT.
#
# Replaces the previous sed pipeline (SABLE-1n2: missed --flag value
# forms) and the shlex+flag-walker variant (SABLE-sqz: missed pipe /
# redirect / chain tokens since they aren't flags but aren't IDs either).
BEAD_ARGS=$(BEAD_CMD="$COMMAND" python3 -c "
import os, re, shlex
cmd = re.sub(r'^bd close\s+', '', os.environ.get('BEAD_CMD', ''))
try:
    tokens = shlex.split(cmd)
except ValueError:
    tokens = []
ID_PATTERN = re.compile(r'^[A-Z][A-Z0-9]*-[a-z0-9]+(\.[0-9]+)?\$')
ids = [t for t in tokens if ID_PATTERN.match(t)]
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
