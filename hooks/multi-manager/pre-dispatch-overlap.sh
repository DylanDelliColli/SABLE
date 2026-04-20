#!/usr/bin/env bash
# pre-dispatch-overlap.sh — Annotate dispatch with overlap warnings
# Trigger: PreToolUse:Agent | Timeout: 5000ms
#
# Reads claims from all in-progress beads (other than the ones being dispatched
# now) and warns if the proposed dispatch's files overlap.
#
# Advisory only — does NOT block dispatch. The information is also surfaced to
# Chuck via the post-push hook so PR sequencing can account for it.
#
# Skips: subagent context, dispatches with no inferrable bead IDs.

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

# Extract bead IDs from this dispatch
DISPATCH_IDS=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
ids = set(re.findall(r'\b((?:bd|sable|epic|task|bug|feat)-[a-zA-Z0-9_-]+)\b', text))
for i in sorted(ids):
    print(i)
" 2>/dev/null)

[ -z "$DISPATCH_IDS" ] && exit 0

# Aggregate file claims from this dispatch's beads
DISPATCH_FILES=$(for BID in $DISPATCH_IDS; do
  bd show "$BID" --json 2>/dev/null | python3 -c "
import json, sys, re
try:
    data = json.load(sys.stdin)
    if not (isinstance(data, list) and data):
        sys.exit(0)
    notes = data[0].get('notes', '') or ''
    desc = data[0].get('description', '') or ''
    text = notes + '\n' + desc
    # Files from WIP-CLAIMS line
    for m in re.finditer(r'WIP-CLAIMS:\s*([^\n]+)', text):
        for p in m.group(1).split(','):
            p = p.strip()
            if p:
                print(p)
    # Files mentioned in description
    for m in re.finditer(r'(?:^|[\s\(\[\"\\'])((?:[\w\-./]+/)?[\w\-./]+\.(?:ts|tsx|js|jsx|py|rs|go|java|rb|md|yaml|yml|toml|json|sh|sql|css|scss|html))(?=[\s\)\]\"\\',:;]|$)', text, re.MULTILINE):
        print(m.group(1))
except Exception:
    pass
" 2>/dev/null
done | sort -u)

[ -z "$DISPATCH_FILES" ] && exit 0

# Find all in-progress beads (status=in_progress) not in dispatch set
IN_PROGRESS=$(bd list --status=in_progress --json 2>/dev/null || echo "[]")

OVERLAPS=$(echo "$IN_PROGRESS" | python3 -c "
import json, sys, os, re

dispatch_ids = set(os.environ.get('DISPATCH_IDS', '').split())
dispatch_files = set(os.environ.get('DISPATCH_FILES', '').split('\n'))
dispatch_files.discard('')

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if not isinstance(data, list):
    sys.exit(0)

overlaps = []
for item in data:
    bid = item.get('id', '')
    if bid in dispatch_ids:
        continue
    notes = item.get('notes', '') or ''
    desc = item.get('description', '') or ''
    text = notes + '\n' + desc
    files = set()
    for m in re.finditer(r'WIP-CLAIMS:\s*([^\n]+)', text):
        for p in m.group(1).split(','):
            p = p.strip()
            if p:
                files.add(p)
    overlap = files & dispatch_files
    if overlap:
        title = item.get('title', '')
        assignee = item.get('assignee', '') or 'unassigned'
        overlaps.append({
            'bead': bid,
            'title': title,
            'assignee': assignee,
            'files': sorted(overlap),
        })

if not overlaps:
    sys.exit(0)

lines = []
for o in overlaps:
    files_str = ', '.join(o['files'])
    lines.append(f\"  - {o['bead']} ({o['assignee']}, in-progress): {files_str}\")
print('\n'.join(lines))
" DISPATCH_IDS="$DISPATCH_IDS" DISPATCH_FILES="$DISPATCH_FILES" 2>/dev/null)

[ -z "$OVERLAPS" ] && exit 0

# Inject overlap warning — advisory, dispatch still proceeds
python3 -c "
import json, os
overlaps = os.environ.get('OVERLAPS', '')
print(json.dumps({
    'additionalContext': f'OVERLAP DETECTED — proposed dispatch shares files with active in-progress work:\n{overlaps}\n\nDispatch will proceed. If intentional collaboration is needed, file a coord bead. Chuck will see this overlap context on PR submission and can sequence merges accordingly.'
}))
" OVERLAPS="$OVERLAPS"
