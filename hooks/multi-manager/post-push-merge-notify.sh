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

# SABLE-b06t: no-op push guard. 'Everything up-to-date' (exit 0, nothing new
# landed) doesn't match any failure keyword, so a keyword-only heuristic
# notifies chuck for content that was never actually pushed by THIS command
# (observed live: tarzan's tmux-only push, origin/tmux-only unchanged at
# 95ce920). The positive ls-remote confirmation below (after BRANCH is known)
# can't catch this case on its own — remote tip == local HEAD looks identical
# whether this push is the one that landed it or a resend of already-landed
# content — so this text check stays as the no-op-specific leg.
STDOUT_STDERR=$(echo "$PARSED" | sed -n '/---STDOUT---/,$p')
if echo "$STDOUT_STDERR" | grep -qiE 'everything[[:space:]]+up-to-date'; then
  exit 0
fi

# market-brief-package-2u25: resolve PER REPO (repo-local git config / .sable
# file wins over session env, shared with pre-push-rebase-test.sh via
# lib-identity.sh) so a foreign SABLE_BASE_BRANCH/SABLE_INTEGRATION_BRANCH
# inherited from another repo's session cannot misfire the guards below.
# Resolved BEFORE BASE_BRANCH (SABLE-pzfk) since the diff-base default now
# depends on it.
INTEGRATION_BRANCH=$(sable_resolve_integration_branch "$CWD")

# Validate the base ref and fall back gracefully (SABLE-61n: an invalid
# SABLE_BASE_BRANCH caused git to exit 128 under set -euo pipefail, silently
# killing the hook before the bd create was reached). Default to the resolved
# integration branch when it is published, not a hardcoded origin/main
# (SABLE-pzfk): on a repo whose integration branch isn't main (tmux-only
# today), the old unconditional origin/main default reported the ENTIRE
# integration-branch-vs-main history as the pushed diff — inflating the file
# list (chuck's PR-ready messages showed an alphabetical docs prefix
# regardless of the real diff) and feeding the wrong file set into the
# overlap analysis below (spurious OVERLAP-WARNINGs against files nobody
# actually touched). Mirrors the SABLE-4amz fix in pre-push-rebase-test.sh
# (commit b77034e): only switch the default when origin/<INT> actually
# exists, else fall back to origin/main as before.
DEFAULT_BASE_BRANCH="origin/main"
if [ -n "$INTEGRATION_BRANCH" ] \
   && git -C "$CWD" rev-parse --verify --quiet "origin/$INTEGRATION_BRANCH" >/dev/null 2>&1; then
  DEFAULT_BASE_BRANCH="origin/$INTEGRATION_BRANCH"
fi
BASE_BRANCH=$(sable_validate_base_ref "$CWD" "${SABLE_BASE_BRANCH:-$DEFAULT_BASE_BRANCH}")

# Determine current branch and modified files
BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
[ -z "$BRANCH" ] && exit 0
[ "$BRANCH" = "HEAD" ] && exit 0

# market-brief-package-2u25: pushing the repo's OWN integration branch is not
# "PR ready for review" — it already IS the integration line (a topology
# promotion decided elsewhere, not routine merge-queue work; chuck triaged an
# earlier misfire of this exact shape as a false-positive).
[ "$BRANCH" = "$INTEGRATION_BRANCH" ] && exit 0

# SABLE-b06t: positive push confirmation, replacing the vacuous failure-phrase
# grep for the exact scenario it was blind to. That heuristic only caught
# failures matching its exact phrase list; it missed the wk-prodspawn
# incident (chuck reviewed a PR-ready message for a branch that never reached
# origin — verified absent after two `git fetch --prune` runs — because the
# worker's push had failed in some way whose captured output didn't trip any
# of the four tracked phrases). Require the branch's remote tip to match
# local HEAD: if the push never actually landed (rejected, auth/network
# failure, no upstream, or any other failure text this heuristic doesn't
# know about), ls-remote either returns nothing for the ref or returns a tip
# that doesn't match — either way, skip instead of notifying chuck to review
# work that isn't there.
REMOTE_TIP=$(git -C "$CWD" ls-remote --exit-code origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1 || echo "")
LOCAL_HEAD=$(git -C "$CWD" rev-parse HEAD 2>/dev/null || echo "")
[ -z "$REMOTE_TIP" ] && exit 0
[ -z "$LOCAL_HEAD" ] && exit 0
[ "$REMOTE_TIP" != "$LOCAL_HEAD" ] && exit 0

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

# Brief basename list, shared by the message-based notifications below (the
# worker-landing wake and the Chuck handoff).
FILES_BRIEF=$(echo "$FILES" | sed 's#.*/##' | head -8 | tr '\n' ' ')

# --- Wake the dispatching manager on a worker landing (SABLE-nmmh) ---------
# Managers now run an EVENT-DRIVEN loop: they END their turn when nothing is
# actionable (optimus.md / tarzan.md), so a worker landing must ACTIVELY wake
# the dispatching manager to review the outcome — otherwise the review waits
# behind the manager's next unrelated wake (the poll-driven latency this bead
# removes). The worker pane carries the lane manager's name in CLAUDE_AGENT_NAME
# (set by worker_env_args at spawn) and this hook runs in that env, so
# SABLE_ID_NAME already resolves to the dispatching manager — message it
# directly; no spawn-helper change needed. Fire ONLY for a real worker landing:
# gate on this pane's @sable_role=worker tag so a manager's OWN emergency push
# (@sable_role=<role>) does not self-notify. Message-only (no durable bead): a
# missed wake degrades to the manager's residual background safety-net sweep, and
# Chuck merges regardless. Disable with SABLE_WORKER_LAND_NOTIFY=0.
if [ "${SABLE_WORKER_LAND_NOTIFY:-1}" = "1" ] \
   && [ -n "${TMUX_PANE:-}" ] && [ -n "${SABLE_ID_NAME:-}" ] \
   && command -v tmux >/dev/null 2>&1 \
   && command -v sable-msg >/dev/null 2>&1; then
  PANE_ROLE=$(tmux display-message -p -t "$TMUX_PANE" '#{@sable_role}' 2>/dev/null || echo "")
  if [ "$PANE_ROLE" = "worker" ]; then
    LAND_MSG="Worker landed: branch ${BRANCH} (${FILES_BRIEF}) pushed & bead closed. Review the outcome — closed bead + for-chuck PR — and REVISE by re-spawning into the same worktree if wrong."
    [ -n "$OVERLAPS" ] && LAND_MSG="${LAND_MSG} OVERLAP-WARNING: shares files with in-flight work."
    sable-msg "$SABLE_ID_NAME" "$LAND_MSG" --from worker >/dev/null 2>&1 || true
  fi
fi

# Is a Chuck pane live on the tmux server (SABLE-wvk9)? A necessary-condition
# probe that decides whether the message-first handoff is even worth attempting:
# an absent pane means Chuck was never launched (or is down), so the durable
# for-chuck bead is the only handoff that survives. Server-wide (-a) is
# deliberate — this is only a gate; sable-msg itself does the authoritative
# session-scoped delivery and will still fail-to-bead if the sole chuck pane
# belongs to another repo's fleet. Fail-OPEN (treat as present) when tmux is
# unavailable or the listing errors, so a probe hiccup never suppresses a send
# that would otherwise land.
sable_chuck_pane_present() {
  command -v tmux >/dev/null 2>&1 || return 0
  local roles
  roles=$(tmux list-panes -a -F '#{@sable_role}' 2>/dev/null) || return 0
  printf '%s\n' "$roles" | grep -qx chuck
}

# --- Message-first handoff with durable fallback (SABLE-bldh.15 / SABLE-wvk9) --
# In the tmux warm-pane topology the worker->merge handoff is a direct message to
# Chuck: event-driven, no polled bead. But the message send is the handoff ONLY
# when Chuck actually receives it — it must never suppress the durable for-chuck
# bead unless delivery is POSITIVELY confirmed to a reachable Chuck pane.
# SABLE-wvk9: a wk-desc-gate-paths push left BOTH paths silent (no message
# reached Chuck AND no fallback bead was filed), so the merge stranded until a
# manual stranded-recovery sweep. Two independent hardenings:
#   1. Probe for a live Chuck pane (@sable_role=chuck) BEFORE trusting the
#      message path. No pane => Chuck was never spawned at push time (the exact
#      incident) or is down: skip the futile send and go straight to the bead.
#   2. Only `exit 0` on a CONFIRMED send (sable-msg exit 0). Every other outcome
#      (unreachable / undelivered / messaging disabled) falls through to the
#      durable for-chuck bead and prints a context line, so the fallback is never
#      silent again. Disable the message leg with SABLE_MERGE_NOTIFY_VIA_MSG=0.
FALLBACK_REASON=""
if [ "${SABLE_MERGE_NOTIFY_VIA_MSG:-1}" != "1" ]; then
  FALLBACK_REASON="message handoff disabled (SABLE_MERGE_NOTIFY_VIA_MSG=0)"
elif ! command -v sable-msg >/dev/null 2>&1; then
  FALLBACK_REASON="sable-msg not on PATH"
elif ! sable_chuck_pane_present; then
  FALLBACK_REASON="no reachable chuck pane (not spawned yet, or down)"
else
  MSG="PR ready from ${SABLE_ID_NAME}: branch ${BRANCH} (${FILES_BRIEF}). Review and merge into the integration branch, then report."
  [ -n "$OVERLAPS" ] && MSG="${MSG} OVERLAP-WARNING: shares files with in-flight work — sequence carefully."
  if sable-msg chuck "$MSG" --from "$SABLE_ID_NAME" >/dev/null 2>&1; then
    exit 0
  fi
  FALLBACK_REASON="sable-msg could not confirm delivery to chuck"
fi

# Build description (durable for-chuck bead — fallback path)
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

# Context line so the durable fallback is never silent again (SABLE-wvk9). A
# PostToolUse hook's stdout surfaces to the agent whose Bash push triggered it,
# so this records — for the worker and any log — WHY the message path did not
# carry the handoff and that the durable for-chuck bead now covers the merge.
echo "post-push-merge-notify: ${FALLBACK_REASON:-message delivery not confirmed} — filed durable for-chuck fallback bead (label for-chuck) for branch ${BRANCH}; chuck merges it from the pool."

exit 0
