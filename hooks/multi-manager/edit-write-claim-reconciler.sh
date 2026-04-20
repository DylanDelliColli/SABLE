#!/usr/bin/env bash
# edit-write-claim-reconciler.sh — Append emergent file claims to bead notes
# Trigger: PreToolUse:Edit|Write | Timeout: 3000ms
#
# When a worker (subagent) modifies a file not declared in the bead description,
# append the file path to the bead's WIP-CLAIMS so other dispatches see it.
#
# Only fires inside subagent contexts (managers don't typically edit files directly
# in this pattern — they dispatch workers). Fast-exit if no agent_id.

set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
tool_input = d.get('tool_input', {})
file_path = tool_input.get('file_path', '')
agent_id = d.get('agent_id', '')
transcript = d.get('transcript_path', '')
print(f'{agent_id}\n{file_path}\n{transcript}')
" 2>/dev/null) || exit 0

AGENT_ID=$(echo "$PARSED" | sed -n '1p')
FILE_PATH=$(echo "$PARSED" | sed -n '2p')
TRANSCRIPT=$(echo "$PARSED" | sed -n '3p')

# Only act on subagent contexts
[ -z "$AGENT_ID" ] && exit 0
[ -z "$FILE_PATH" ] && exit 0
[ -z "$TRANSCRIPT" ] && exit 0

# Skip test files and generated files (less likely to be claim-worthy overlap surface)
echo "$FILE_PATH" | grep -qiE '(\.test\.|\.spec\.|__tests__|__pycache__|\.next/|node_modules/)' && exit 0

# Find the bead this subagent was dispatched for by scanning the transcript for
# a recent `bd update --claim` or "bead bd-X" reference
[ ! -f "$TRANSCRIPT" ] && exit 0

BEAD_ID=$(python3 -c "
import json, re, sys
path = sys.argv[1]
ids = []
try:
    with open(path) as f:
        for line in f:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            text = json.dumps(msg)
            for m in re.finditer(r'\b((?:bd|sable|epic|task|bug|feat)-[a-zA-Z0-9_-]+)\b', text):
                ids.append(m.group(1))
except Exception:
    sys.exit(0)
# Most recent ID is most likely to be the active bead
if ids:
    print(ids[-1])
" "$TRANSCRIPT" 2>/dev/null)

[ -z "$BEAD_ID" ] && exit 0

# Read current notes
CURRENT=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and data:
        print(data[0].get('notes', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

# If file already in claims, skip
echo "$CURRENT" | grep -qF "$FILE_PATH" && exit 0

# Append to claims
if echo "$CURRENT" | grep -q "^WIP-CLAIMS:"; then
  # Existing claim line — append file
  NEW_NOTES=$(echo "$CURRENT" | python3 -c "
import sys, os
file_path = os.environ.get('FILE_PATH', '')
text = sys.stdin.read()
lines = text.split('\n')
out = []
for line in lines:
    if line.startswith('WIP-CLAIMS:'):
        existing = line[len('WIP-CLAIMS:'):].strip()
        out.append(f'WIP-CLAIMS: {existing}, {file_path}')
    else:
        out.append(line)
print('\n'.join(out))
" FILE_PATH="$FILE_PATH")
else
  # No claim line yet — add one
  if [ -n "$CURRENT" ]; then
    NEW_NOTES="${CURRENT}
WIP-CLAIMS: ${FILE_PATH}"
  else
    NEW_NOTES="WIP-CLAIMS: ${FILE_PATH}"
  fi
fi

bd update "$BEAD_ID" --notes "$NEW_NOTES" >/dev/null 2>&1 || true

exit 0
