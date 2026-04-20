#!/usr/bin/env bash
# inbox-injection.sh — PostToolUse:Bash, automatic inbox push notification
# Trigger: PostToolUse:Bash | Timeout: 5000ms
#
# After every Bash tool call in a manager's main session, query for-<self> beads.
# If new (unannounced) beads exist, inject a notification via additionalContext.
#
# Critical safety mechanisms:
#  - Skip if agent_id is present (subagent context — workers must not see manager inbox)
#  - Skip if CLAUDE_AGENT_NAME unset / CLAUDE_AGENT_ROLE != manager
#  - Dedup via session-scoped file at /tmp/inbox-seen-${SESSION_ID}
#
# Companion: inbox-injection-precompact.sh clears the dedup file on PreCompact.

set -euo pipefail

[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
sid = d.get('session_id', '')
agent_id = d.get('agent_id', '')
print(f'{sid}\n{agent_id}')
" 2>/dev/null) || exit 0

SESSION_ID=$(echo "$PARSED" | sed -n '1p')
AGENT_ID=$(echo "$PARSED" | sed -n '2p')

# Subagent context — skip
[ -n "$AGENT_ID" ] && exit 0
[ -z "$SESSION_ID" ] && exit 0

INBOX_LABEL="for-${CLAUDE_AGENT_NAME}"
SEEN_FILE="/tmp/inbox-seen-${SESSION_ID}"

# Query inbox (open + ready). Use --json for parseable output.
INBOX_JSON=$(bd ready -l "$INBOX_LABEL" --json 2>/dev/null || echo "[]")

# Extract bead IDs and titles
NEW_ITEMS=$(echo "$INBOX_JSON" | python3 -c "
import json, sys, os
seen_file = os.environ.get('SEEN_FILE', '')
seen = set()
if os.path.exists(seen_file):
    with open(seen_file) as f:
        seen = set(line.strip() for line in f if line.strip())

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if not isinstance(data, list):
    sys.exit(0)

new = []
for item in data:
    bid = item.get('id', '')
    title = item.get('title', '')
    pri = item.get('priority', '')
    if bid and bid not in seen:
        new.append((bid, title, pri))

if not new:
    sys.exit(0)

# Append new IDs to seen file
with open(seen_file, 'a') as f:
    for bid, _, _ in new:
        f.write(bid + '\n')

# Format notification
lines = []
for bid, title, pri in new:
    pri_marker = f'[P{pri}] ' if pri != '' else ''
    lines.append(f'  - {pri_marker}{bid}: {title}')
print('\n'.join(lines))
" SEEN_FILE="$SEEN_FILE")

[ -z "$NEW_ITEMS" ] && exit 0

python3 -c "
import json, os
items = os.environ.get('NEW_ITEMS', '')
name = os.environ.get('CLAUDE_AGENT_NAME', '').upper()
print(json.dumps({
    'additionalContext': f'INBOX ({name}) — new addressed beads:\n{items}\n\nRun \`bd show <id>\` to read. Run \`/inbox\` to see all current items.'
}))
" NEW_ITEMS="$NEW_ITEMS"
