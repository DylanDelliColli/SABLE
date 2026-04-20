#!/usr/bin/env bash
# post-push-merge-notify.sh — File for-chuck bead with overlap analysis after push
# Trigger: PostToolUse:Bash matching `git push` | Timeout: 10000ms
#
# After a successful git push, file a coord bead addressed to chuck (the merge
# integrator) with: PR URL (if detectable), files modified, overlap context with
# any in-progress beads' WIP-CLAIMS.
#
# Chuck uses this to sequence merges intelligently — hold a PR if it overlaps
# an in-flight PR, merge if independent.

set -euo pipefail

[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0
# Don't notify on chuck's own pushes
[ "$CLAUDE_AGENT_NAME" = "chuck" ] && exit 0

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
agent_id = d.get('agent_id', '')
cwd = d.get('cwd', '')
resp = d.get('tool_response', {})
stdout = resp.get('stdout', '') if isinstance(resp, dict) else ''
stderr = resp.get('stderr', '') if isinstance(resp, dict) else ''
print(f'{agent_id}\n{cwd}\n{cmd}')
print('---STDOUT---')
print(stdout)
print('---STDERR---')
print(stderr)
" 2>/dev/null) || exit 0

NESTED_AGENT_ID=$(echo "$PARSED" | sed -n '1p')
CWD=$(echo "$PARSED" | sed -n '2p')
COMMAND=$(echo "$PARSED" | sed -n '3p')

[ -n "$NESTED_AGENT_ID" ] && exit 0

# Only act on successful git push
echo "$COMMAND" | grep -qE '\bgit\s+push\b' || exit 0
[ -z "$CWD" ] && exit 0

# Quick success heuristic: rejected/error/fatal in stderr means failure
STDOUT_STDERR=$(echo "$PARSED" | sed -n '/---STDOUT---/,$p')
if echo "$STDOUT_STDERR" | grep -qiE '(rejected|! \[remote rejected\]|error: failed to push|fatal:)'; then
  exit 0
fi

BASE_BRANCH="${SABLE_BASE_BRANCH:-origin/main}"

# Determine current branch and modified files
BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
[ -z "$BRANCH" ] && exit 0
[ "$BRANCH" = "HEAD" ] && exit 0

FILES=$(git -C "$CWD" diff "$BASE_BRANCH"...HEAD --name-only 2>/dev/null | head -50)
[ -z "$FILES" ] && exit 0

# Try to detect PR URL via gh (best-effort, optional)
PR_URL=$(gh pr view --json url -q .url 2>/dev/null || echo "")

# Find overlaps with in-progress beads' WIP-CLAIMS
FILES_CSV=$(echo "$FILES" | tr '\n' ',' | sed 's/,$//')

OVERLAPS=$(bd list --status=in_progress --json 2>/dev/null | python3 -c "
import json, sys, os, re

pushed = set(os.environ.get('FILES_CSV', '').split(','))
pushed.discard('')

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(data, list):
    sys.exit(0)

overlaps = []
for item in data:
    notes = item.get('notes', '') or ''
    if 'WIP-CLAIMS:' not in notes:
        continue
    files = set()
    for m in re.finditer(r'WIP-CLAIMS:\s*([^\n]+)', notes):
        for p in m.group(1).split(','):
            p = p.strip()
            if p:
                files.add(p)
    o = files & pushed
    if o:
        overlaps.append({
            'bead': item.get('id', ''),
            'title': item.get('title', ''),
            'assignee': item.get('assignee', '') or 'unassigned',
            'files': sorted(o),
        })

if not overlaps:
    sys.exit(0)

lines = []
for o in overlaps:
    lines.append(f\"  - {o['bead']} ({o['assignee']}): {', '.join(o['files'])}\")
print('\n'.join(lines))
" FILES_CSV="$FILES_CSV" 2>/dev/null)

# Build description
DESC_LINES=""
DESC_LINES="${DESC_LINES}PR ready for review."
[ -n "$PR_URL" ] && DESC_LINES="${DESC_LINES}
PR URL: $PR_URL"
DESC_LINES="${DESC_LINES}
Branch: $BRANCH
Submitted by: $CLAUDE_AGENT_NAME

## Files Modified
$(echo "$FILES" | sed 's/^/  - /')"

if [ -n "$OVERLAPS" ]; then
  DESC_LINES="${DESC_LINES}

## Overlap Warning
The following in-progress beads share files with this PR. Sequence merges accordingly:
$OVERLAPS"
fi

DESC_LINES="${DESC_LINES}

## Acceptance Criteria
- CI green
- Conflict resolution applied (mechanical fixes inline; semantic conflicts deferred to author via for-${CLAUDE_AGENT_NAME} bead)
- PR merged or held with reason"

TITLE="Review PR from ${CLAUDE_AGENT_NAME}: ${BRANCH}"

bd create \
  --title "$TITLE" \
  --type=task \
  --priority=2 \
  --labels=for-chuck,coord \
  --description "$DESC_LINES" >/dev/null 2>&1 || true

exit 0
