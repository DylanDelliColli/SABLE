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
# Skips: worker/bare subagent context, dispatches with no inferrable bead IDs.

set -euo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

# Identity/lane gating via lib-identity.sh (SABLE-uz9.3 / SABLE-4it): governance
# runs for manager-typed subagents (native worker dispatch), legacy manager
# terminals, and the Lincoln main session in execution mode; worker/bare-id
# subagent contexts stand down inside sable_resolve_dispatch_lane. Lane comes
# from identity — the "Dispatching-for:" relay parse is deleted.
# shellcheck source=lib-identity.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib-identity.sh"
sable_resolve_dispatch_lane "$HOOK_INPUT"
[ "$SABLE_DISPATCH_ACTIVE" -eq 1 ] || exit 0

PROMPT=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print((d.get('tool_input', {}) or {}).get('prompt', ''))
" 2>/dev/null) || exit 0

[ -z "$PROMPT" ] && exit 0

# Extract bead IDs from this dispatch
DISPATCH_IDS=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
ids = set(re.findall(r'\b((?:bd|sable|epic|task|bug|feat)-[a-zA-Z0-9_-]+)\b', text, re.IGNORECASE))
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
    # Claims already established for this bead (SABLE-szd: dedicated
    # metadata field, NOT notes — bd update --notes overwrites the whole
    # field, so notes can no longer be trusted as a claims source).
    metadata = data[0].get('metadata', {}) or {}
    wip_claims = metadata.get('wip_claims', '') or ''
    for p in wip_claims.split(','):
        p = p.strip()
        if p:
            print(p)
    # Files mentioned in description (claims may not exist yet at
    # pre-dispatch time — this hook and pre-dispatch-claim.sh fire on the
    # same trigger with no ordering guarantee).
    desc = data[0].get('description', '') or ''
    for m in re.finditer(r'(?:^|[\s\(\[\"\\'])((?:[\w\-./]+/)?[\w\-./]+\.(?:ts|tsx|js|jsx|py|rs|go|java|rb|md|yaml|yml|toml|json|sh|sql|css|scss|html))(?=[\s\)\]\"\\',:;]|$)', desc, re.MULTILINE):
        print(m.group(1))
except Exception:
    pass
" 2>/dev/null
done | sort -u)

[ -z "$DISPATCH_FILES" ] && exit 0

# Find all in-progress beads (status=in_progress) not in dispatch set
IN_PROGRESS=$(bd list --status=in_progress --json 2>/dev/null || echo "[]")

OVERLAPS=$(echo "$IN_PROGRESS" | DISPATCH_IDS="$DISPATCH_IDS" DISPATCH_FILES="$DISPATCH_FILES" python3 -c "
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
    metadata = item.get('metadata', {}) or {}
    wip_claims = metadata.get('wip_claims', '') or ''
    files = set(p.strip() for p in wip_claims.split(',') if p.strip())
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
" 2>/dev/null)

[ -z "$OVERLAPS" ] && exit 0

# Inject overlap warning — advisory, dispatch still proceeds
OVERLAPS="$OVERLAPS" python3 -c "
import json, os
overlaps = os.environ.get('OVERLAPS', '')
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': f'OVERLAP DETECTED — proposed dispatch shares files with active in-progress work:\n{overlaps}\n\nDispatch will proceed. If intentional collaboration is needed, file a coord bead. Chuck will see this overlap context on PR submission and can sequence merges accordingly.'
    }
}))
"
