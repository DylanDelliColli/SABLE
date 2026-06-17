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

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

# Identity via lib-identity.sh (SABLE-uz9.3 / SABLE-aok): fires for any manager
# identity (legacy env terminals, the Lincoln main session in execution mode,
# and manager subagents — attribution uses the RESOLVED name SABLE_ID_NAME, never
# the raw CLAUDE_AGENT_NAME env, which belongs to the parent session in v3);
# workers and anonymous sessions stand down.
# shellcheck source=lib-identity.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib-identity.sh"
sable_resolve_identity "$HOOK_INPUT"
[ "$SABLE_ID_IS_MANAGER" -eq 1 ] || exit 0
# Don't notify on chuck's own pushes
[ "$SABLE_ID_NAME" = "chuck" ] && exit 0

PARSED=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
cmd = d.get('tool_input', {}).get('command', '')
cwd = d.get('cwd', '')
resp = d.get('tool_response', {})
stdout = resp.get('stdout', '') if isinstance(resp, dict) else ''
stderr = resp.get('stderr', '') if isinstance(resp, dict) else ''
print(f'{cwd}\n{cmd}')
print('---STDOUT---')
print(stdout)
print('---STDERR---')
print(stderr)
" 2>/dev/null) || exit 0

CWD=$(echo "$PARSED" | sed -n '1p')
COMMAND=$(echo "$PARSED" | sed -n '2p')

# Only act on successful git push (SABLE-jpr: use shared matcher so
# 'git -C <path> push' and other flag-interleaved forms are matched correctly)
sable_is_git_push "$COMMAND" || exit 0

# Resolve the effective repo dir from the push command's `git -C <path>`
# target, falling back to the shell cwd. A manager pushing a worktree via
# `git -C <worktree> push` runs from the main checkout, so the pushed branch
# and its diff live in the -C target, not the shell cwd (SABLE-041).
CWD=$(sable_resolve_push_repo_dir "$CWD" "$COMMAND")
[ -z "$CWD" ] && exit 0

# Quick success heuristic: rejected/error/fatal in stderr means failure
STDOUT_STDERR=$(echo "$PARSED" | sed -n '/---STDOUT---/,$p')
if echo "$STDOUT_STDERR" | grep -qiE '(rejected|! \[remote rejected\]|error: failed to push|fatal:)'; then
  exit 0
fi

# Validate the base ref and fall back gracefully (SABLE-61n: an invalid
# SABLE_BASE_BRANCH caused git to exit 128 under set -euo pipefail, silently
# killing the hook before the bd create was reached)
BASE_BRANCH=$(sable_validate_base_ref "$CWD" "${SABLE_BASE_BRANCH:-origin/main}")

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

OVERLAPS=$(bd list --status=in_progress --json 2>/dev/null | FILES_CSV="$FILES_CSV" python3 -c "
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
" 2>/dev/null)

# Build description
DESC_LINES=""
DESC_LINES="${DESC_LINES}PR ready for review."
[ -n "$PR_URL" ] && DESC_LINES="${DESC_LINES}
PR URL: $PR_URL"
DESC_LINES="${DESC_LINES}
Branch: $BRANCH
Submitted by: $SABLE_ID_NAME

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
- Conflict resolution applied (mechanical fixes inline; semantic conflicts deferred to author via for-${SABLE_ID_NAME} bead)
- PR merged or held with reason"

TITLE="Review PR from ${SABLE_ID_NAME}: ${BRANCH}"

bd create \
  --title "$TITLE" \
  --type=task \
  --priority=2 \
  --labels=for-chuck,coord \
  --description "$DESC_LINES" >/dev/null 2>&1 || true

exit 0
