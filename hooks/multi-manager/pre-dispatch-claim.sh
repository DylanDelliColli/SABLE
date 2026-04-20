#!/usr/bin/env bash
# pre-dispatch-claim.sh — Pre-write WIP file claims to bead notes at dispatch time
# Trigger: PreToolUse:Agent | Timeout: 5000ms
#
# Reads the bead description (extracts bead ID from dispatch prompt), parses
# referenced file paths, and writes them to bead notes as WIP-CLAIMS:<paths>.
#
# Closes the dispatch-time race condition where worker B dispatches before
# worker A has started editing — claims now exist at dispatch.
#
# Companion: edit-write-claim-reconciler.sh appends emergent claims as workers edit.
#
# Skips if: no bead ID inferrable, no file paths in description, subagent context.

set -euo pipefail

[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
tool_input = d.get('tool_input', {})
prompt = tool_input.get('prompt', '')
agent_id = d.get('agent_id', '')
print(agent_id)
print('---PROMPT---')
print(prompt)
" 2>/dev/null) || exit 0

NESTED_AGENT_ID=$(echo "$PARSED" | sed -n '1p')
PROMPT=$(echo "$PARSED" | sed -n '3,$p')

[ -n "$NESTED_AGENT_ID" ] && exit 0
[ -z "$PROMPT" ] && exit 0

# Extract bead IDs from prompt — common formats: "bd-123", "bead bd-abc-1", "claim bd-x"
BEAD_IDS=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
ids = set(re.findall(r'\b((?:bd|sable|epic|task|bug|feat)-[a-zA-Z0-9_-]+)\b', text))
for i in sorted(ids):
    print(i)
" 2>/dev/null)

[ -z "$BEAD_IDS" ] && exit 0

# For each bead, read its description, extract file paths, write claims
for BEAD_ID in $BEAD_IDS; do
  DESC=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and data:
        print(data[0].get('description', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

  [ -z "$DESC" ] && continue

  # Extract file paths — match common code file patterns
  FILES=$(echo "$DESC" | python3 -c "
import sys, re
text = sys.stdin.read()
# Match file paths with code extensions
paths = set()
for m in re.finditer(r'(?:^|[\s\(\[\"\\'])((?:[\w\-./]+/)?[\w\-./]+\.(?:ts|tsx|js|jsx|py|rs|go|java|rb|md|yaml|yml|toml|json|sh|sql|css|scss|html))(?=[\s\)\]\"\\',:;]|$)', text, re.MULTILINE)
    paths.add(m.group(1))
print(','.join(sorted(paths)))
" 2>/dev/null || echo "")

  [ -z "$FILES" ] && continue

  # Read current notes; only append claim if not already present
  CURRENT_NOTES=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and data:
        print(data[0].get('notes', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

  if echo "$CURRENT_NOTES" | grep -q "^WIP-CLAIMS:"; then
    continue  # claims already established
  fi

  CLAIM_LINE="WIP-CLAIMS: $FILES"
  if [ -n "$CURRENT_NOTES" ]; then
    NEW_NOTES="${CURRENT_NOTES}
${CLAIM_LINE}"
  else
    NEW_NOTES="$CLAIM_LINE"
  fi

  bd update "$BEAD_ID" --notes "$NEW_NOTES" >/dev/null 2>&1 || true
done

exit 0
