#!/usr/bin/env bash
# bead-description-gate.sh — Validate bead descriptions on creation
# Trigger: PreToolUse on Bash (matching bd create) | Timeout: 3000ms
#
# Checks that bd create commands include meaningful descriptions with
# test specs and acceptance criteria. Injects a warning if missing.
# Does NOT block — this is a nudge, not a gate.

set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
print(cmd)
" 2>/dev/null) || exit 0

COMMAND="$PARSED"
[ -z "$COMMAND" ] && exit 0

# Only act on bd create commands
echo "$COMMAND" | grep -q '^bd create' || exit 0

# Skip epics — they don't need test specs
echo "$COMMAND" | grep -qiE 'epic' && exit 0

# Check for description flag
if ! echo "$COMMAND" | grep -qE '\-\-description'; then
  python3 -c "
import json
print(json.dumps({
    'additionalContext': 'SABLE bead quality: This bd create has no --description flag. Every bead needs a description that passes the Fresh Agent Test: file paths, function names, what to change, test file path, and acceptance criteria.'
}))
"
  exit 0
fi

# Extract description content (between quotes after --description)
DESC=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = re.search(r'--description[= ]\"([^\"]+)\"', cmd) or re.search(r\"--description[= ]'([^']+)'\", cmd)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

[ -z "$DESC" ] && exit 0

# Check for key quality signals
MISSING=""

# Test spec: should mention a test file or testing approach
if ! echo "$DESC" | grep -qiE '(test|\.test\.|\.spec\.|__tests__|pytest|vitest|TDD|red.green|\[no-test\])'; then
  MISSING="test spec (which test file, what assertions)"
fi

# File paths: should reference specific files
if ! echo "$DESC" | grep -qiE '(\.(ts|tsx|py|js|jsx)|frontend/|src/|lib/|components/)'; then
  [ -n "$MISSING" ] && MISSING="$MISSING, " || true
  MISSING="${MISSING}file paths (exact files to create/modify)"
fi

[ -z "$MISSING" ] && exit 0

python3 -c "
import json
print(json.dumps({
    'additionalContext': 'SABLE bead quality: Description is missing: $MISSING. Good beads include file paths, function names, test file references, and acceptance criteria so agents can act immediately without re-exploring.'
}))
"
