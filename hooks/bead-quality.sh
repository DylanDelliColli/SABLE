#!/usr/bin/env bash
# bead-quality.sh — PostToolUse nudge after bd create
# Checks that newly created beads have required sections.
# Bugs need: ## Steps to Reproduce, ## Acceptance Criteria
# Tasks/features need: ## Acceptance Criteria

set -euo pipefail

# Parse stdin JSON
PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
stdout = d.get('tool_result', {}).get('stdout', '')
sid = d.get('session_id', '')
print(f'{sid}\n{cmd}\n{stdout}')
" 2>/dev/null) || exit 0

COMMAND=$(echo "$PARSED" | sed -n '2p')
STDOUT=$(echo "$PARSED" | sed -n '3p')

[ -z "$COMMAND" ] && exit 0

# Only act on bd create commands
echo "$COMMAND" | grep -q '^bd create' || exit 0

# Extract the bead ID from stdout (format: "Created issue: <id> — ...")
BEAD_ID=$(echo "$STDOUT" | grep -oP 'Created issue: \K[a-zA-Z0-9_-]+' || echo "")
[ -z "$BEAD_ID" ] && exit 0

# Detect type from the command
TYPE="task"
if echo "$COMMAND" | grep -q '\-\-type=bug'; then
  TYPE="bug"
fi

# Get the bead description
DESC=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if isinstance(data, list) and len(data) > 0:
    print(data[0].get('description', '') or '')
" 2>/dev/null || echo "")

# Check required sections
MISSING=""
if [ "$TYPE" = "bug" ]; then
  echo "$DESC" | grep -q '## Steps to Reproduce' || MISSING="## Steps to Reproduce"
fi
echo "$DESC" | grep -qi '## Acceptance Criteria' || {
  [ -n "$MISSING" ] && MISSING="$MISSING, ## Acceptance Criteria" || MISSING="## Acceptance Criteria"
}

# If nothing missing, exit silently
[ -z "$MISSING" ] && exit 0

# Inject reminder as additional context
python3 -c "
import json
msg = 'Bead quality: $BEAD_ID is missing required sections: $MISSING. Run bd update $BEAD_ID --description \"...\" to add them now. Bugs need ## Steps to Reproduce and ## Acceptance Criteria. Tasks need ## Acceptance Criteria.'
print(json.dumps({'additionalContext': msg}))
"
