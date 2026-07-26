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

# SABLE-jfg6.1 (contract D1): durable entry trace at TRUE line 1, before the
# stdin read AND before the git-push matcher / identity gate below. The legacy
# SABLE-tb1y "INVOKED" line (further down) sits AFTER the matcher, so its
# absence could not distinguish never-dispatched from dispatched-with-empty-
# stdin (research F1); this ENTRY line closes that gap. Additive only — the
# matcher, identity gate, and pp_trace disposition logging are all unchanged.
# shellcheck source=lib-hook-trace.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib-hook-trace.sh"
sable_trace_entry post-push-merge-notify

HOOK_INPUT=$(sable_trace_read_stdin) || HOOK_INPUT=""

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

# --- Invocation trace (SABLE-tb1y) -----------------------------------------
# The 2026-07-14 silent-strand class (a completed worker push that filed NEITHER
# a for-chuck bead NOR a manager-wake msg) was undiagnosable post-hoc: the only
# signal was the worker pane's stdout, and the pane was reaped before anyone
# looked. This append-only, pane-reap-surviving log records that the hook fired
# for this push AND the disposition it exited on, so the next occurrence is
# diagnosable instead of dark. Low-volume (one line per manager/worker push).
# Failsafe by construction: every tracing error is swallowed so it can never
# affect the handoff. Disable with SABLE_HOOK_TRACE=0; redirect with
# SABLE_HOOK_TRACE_LOG.
SABLE_HOOK_TRACE_LOG="${SABLE_HOOK_TRACE_LOG:-${HOME:-/tmp}/.claude/sable/logs/post-push-merge-notify.log}"
sable_pp_trace() {
  [ "${SABLE_HOOK_TRACE:-1}" = "0" ] && return 0
  local dir
  dir=$(dirname "$SABLE_HOOK_TRACE_LOG" 2>/dev/null) || return 0
  [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || return 0
  printf '%s pid=%s name=%s branch=%s | %s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '?')" \
    "$$" "${SABLE_ID_NAME:-?}" "${BRANCH:-?}" "$*" \
    >> "$SABLE_HOOK_TRACE_LOG" 2>/dev/null || true
}
sable_pp_trace "INVOKED cwd=${CWD} cmd=[${COMMAND}]"

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
  sable_pp_trace "EXIT no-op-push (everything up-to-date)"
  exit 0
fi

# market-brief-package-2u25: resolve PER REPO (repo-local git config / .sable
# file wins over session env, shared with pre-push-rebase-test.sh via
# lib-identity.sh) so a foreign SABLE_BASE_BRANCH/SABLE_INTEGRATION_BRANCH
# inherited from another repo's session cannot misfire the guards below.
# Resolved BEFORE BASE_BRANCH (SABLE-pzfk) since the diff-base default now
# depends on it.
INTEGRATION_BRANCH=$(sable_resolve_integration_branch "$CWD")

# SABLE-xuxx: use the SABLE-1238 authoritative resolver instead of
# re-deriving the same precedence by hand. The prior inline logic (SABLE-61n
# fallback + SABLE-pzfk integration-branch default + SABLE-cstk exists-check)
# still honored a LEAKED SABLE_BASE_BRANCH whenever it happened to exist in
# this repo (e.g. origin/main, which exists in nearly every repo) even when
# the repo's own published integration branch (origin/tmux-only) should be
# authoritative — inflating the diff/file list and OVERLAP-WARNING analysis
# below with the integration branch's own history vs main. sable_resolve_base_branch
# never lets a leaked SABLE_BASE_BRANCH override a PUBLISHED origin/<INT>; it
# only falls through to SABLE_BASE_BRANCH (then origin/main, then @{upstream})
# when the integration branch itself is unpublished. Same graceful-fallback
# guarantee as before: never aborts under set -euo pipefail.
BASE_BRANCH=$(sable_resolve_base_branch "$CWD")

# Determine current branch and modified files
BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  # A manager-authored push we cannot attribute to a branch (empty or detached
  # HEAD). Not the strand class this bead fixes, but SABLE-tb1y deliverable 3
  # says never silent-exit a manager push — say why and trace it.
  echo "post-push-merge-notify: skipping — cannot resolve a branch name in ${CWD} (rev-parse gave '${BRANCH:-<empty>}'); no PR handoff possible."
  sable_pp_trace "EXIT no-branch (rev-parse=${BRANCH:-empty})"
  exit 0
fi

# market-brief-package-2u25: pushing the repo's OWN integration branch is not
# "PR ready for review" — it already IS the integration line (a topology
# promotion decided elsewhere, not routine merge-queue work; chuck triaged an
# earlier misfire of this exact shape as a false-positive).
if [ "$BRANCH" = "$INTEGRATION_BRANCH" ]; then
  sable_pp_trace "EXIT integration-branch self-push (${BRANCH})"
  exit 0
fi

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
LOCAL_HEAD=$(git -C "$CWD" rev-parse HEAD 2>/dev/null || echo "")
if [ -z "$LOCAL_HEAD" ]; then
  echo "post-push-merge-notify: skipping — cannot resolve local HEAD in ${CWD} on branch ${BRANCH}; no handoff possible."
  sable_pp_trace "EXIT no-local-head"
  exit 0
fi

# SABLE-tb1y: settle the ls-remote confirmation under load. The single-shot
# check above (SABLE-b06t) silently `exit 0`'d whenever REMOTE_TIP != LOCAL_HEAD.
# That is correct for a push that never landed — but it ALSO fired for a push
# that DID land whose new tip origin had not yet reflected at the instant the
# hook read it. Under concurrent-push load that read raced the remote's ref
# update: on the 2026-07-14 optimus shift strandings clustered HARD in the late,
# high-load half of the shift (6/7 workers CLEAN early vs 4/4 STRANDED late) —
# the timing-race signature. A raced miss meant a DOUBLE silent failure: no
# for-chuck bead AND no manager-wake msg (both live BELOW this guard), so a
# completed merge went dark until a manual stranded-recovery sweep.
#
# Fix: poll ls-remote a bounded number of times, breaking the instant the remote
# tip matches local HEAD — giving origin a moment to settle. A genuinely
# unlanded push (rejected / auth / network) never matches and still skips, so the
# SABLE-b06t "don't notify chuck about work that isn't on origin" guarantee holds
# — but the skip is now LOUD (SABLE-tb1y deliverable 3: never silent-exit a
# manager-authored push). Tunable via SABLE_PUSH_CONFIRM_RETRIES (default 4 extra
# tries = 5 reads) and SABLE_PUSH_CONFIRM_SLEEP (default 0.3s; worst-case added
# latency ~1.2s, well under the 10s hook timeout).
#
# SABLE-27r3: each read is wrapped in `timeout` (default 3s, SABLE_LSREMOTE_TIMEOUT)
# because a stalled GitHub SSH connection (port 22) HANGS ls-remote rather than
# failing fast — confirmed three times on 2026-07-09, including one 2-minute
# ls-remote stall that blocked the whole PostToolUse hook (and therefore the
# pushing agent's tool call) for its entire duration. The `2>/dev/null || echo`
# fallback only ever caught FAILURES, never hangs. On a timeout, `timeout` kills
# git and returns 124; the pipeline (under set -euo pipefail) then fails and
# falls to the `|| echo ""` branch, so REMOTE_TIP stays empty and this attempt
# is treated exactly like an unreachable/unconfirmed remote — the existing
# skip-instead-of-notify degradation below is the correct behavior (worst case
# a real push goes un-notified and lands via chuck's stranded-recovery sweep).
# `-k 2` backstops with SIGKILL 2s after the initial SIGTERM: a process blocked
# in a network syscall (ssh) does not always die promptly on SIGTERM, and a
# hung child that outlives the timeout would keep this read's pipe open.
LSREMOTE_TIMEOUT=${SABLE_LSREMOTE_TIMEOUT:-3}
CONFIRM_TRIES=${SABLE_PUSH_CONFIRM_RETRIES:-4}
CONFIRM_SLEEP=${SABLE_PUSH_CONFIRM_SLEEP:-0.3}
REMOTE_TIP=""
CONFIRM_ATTEMPT=0
while : ; do
  REMOTE_TIP=$(timeout -k 2 "$LSREMOTE_TIMEOUT" git -C "$CWD" ls-remote --exit-code origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1 || echo "")
  [ -n "$REMOTE_TIP" ] && [ "$REMOTE_TIP" = "$LOCAL_HEAD" ] && break
  [ "$CONFIRM_ATTEMPT" -ge "$CONFIRM_TRIES" ] && break
  CONFIRM_ATTEMPT=$((CONFIRM_ATTEMPT + 1))
  sleep "$CONFIRM_SLEEP" 2>/dev/null || true
done

if [ -z "$REMOTE_TIP" ] || [ "$REMOTE_TIP" != "$LOCAL_HEAD" ]; then
  # Unconfirmed after the settle budget: either the push genuinely did not land
  # (skipping is correct — chuck must not review absent work) or origin lag
  # exceeded the retries. Either way, say so loudly + trace it so a stranded
  # merge is diagnosable instead of silent.
  echo "post-push-merge-notify: skipping — branch ${BRANCH} local HEAD ${LOCAL_HEAD} NOT confirmed on origin after $((CONFIRM_ATTEMPT + 1)) ls-remote attempt(s) (remote tip: ${REMOTE_TIP:-<none>}). If the push DID land, this was origin lag beyond the retry budget — file the for-chuck handoff manually or raise SABLE_PUSH_CONFIRM_RETRIES; if it did NOT land, no handoff is correct."
  sable_pp_trace "EXIT unconfirmed local=${LOCAL_HEAD} remote=${REMOTE_TIP:-none} attempts=$((CONFIRM_ATTEMPT + 1))"
  exit 0
fi
sable_pp_trace "CONFIRMED local=${LOCAL_HEAD} remote=${REMOTE_TIP} attempts=$((CONFIRM_ATTEMPT + 1))"

FILES=$(git -C "$CWD" diff "$BASE_BRANCH"...HEAD --name-only 2>/dev/null | head -50)
# SABLE-5hcg addendum: an empty diff used to silent-exit here, so a push whose
# diff vs the integration base came up empty (e.g. a misresolved BASE_BRANCH)
# left no trace anywhere. Loud skip line instead — nothing to hand off to
# chuck, but the reason is now visible to the pushing agent, not swallowed.
if [ -z "$FILES" ]; then
  echo "post-push-merge-notify: skipping — no file diff between ${BASE_BRANCH} and HEAD on branch ${BRANCH}; nothing to hand off to chuck. If unexpected, check SABLE_BASE_BRANCH / integration-branch resolution."
  sable_pp_trace "EXIT empty-diff base=${BASE_BRANCH}"
  exit 0
fi

# --- Push-time preview kick (SABLE-jd5fj.1) ---------------------------------
# The merge-preview ci-verify run is the slowest link in the worker->merge path,
# and until now it only STARTED once Chuck picked the handoff up — so its whole
# duration sat between the push and the merge. Fire `sable-merge-gate preview`
# here instead — it builds the merge-preview commit, pushes the ci-verify ref
# (the CI trigger) and returns WITHOUT waiting — so the verdict is already
# computing before Chuck wakes. Chuck's later `promote` adopts that same ref via
# the shared (base_sha, branch_sha) key, so this starts the run he waits on
# rather than a second one.
#
# Placement is the contract: every no-kick guard already stands above this line
# and stays authoritative — integration-branch self-push, 'Everything
# up-to-date' no-op, unconfirmed-on-origin push, and empty diff vs the base all
# exit before reaching here. So a kick happens exactly once per confirmed,
# non-empty, non-self push, which is exactly the set of pushes that become a
# Chuck merge.
#
# Fire-and-forget by construction: detached (setsid/nohup, own stdio) because the
# kick's fetch+push outlives this hook's 10s timeout and must not be reaped with
# the hook, and NON-BLOCKING because the handoff below must never wait on it. The
# kick is pure warm-up — it writes no bead evidence and reports no verdict, so
# losing one costs latency only (jd5fj.2's poll leg re-kicks a missed preview,
# and promote still builds its own preview when none was kicked). Disable with
# SABLE_PREVIEW_KICK=0; output lands in SABLE_PREVIEW_KICK_LOG.
if [ "${SABLE_PREVIEW_KICK:-1}" = "1" ]; then
  if command -v sable-merge-gate >/dev/null 2>&1; then
    PREVIEW_KICK_LOG="${SABLE_PREVIEW_KICK_LOG:-$(dirname "$SABLE_HOOK_TRACE_LOG")/preview-kick.log}"
    mkdir -p "$(dirname "$PREVIEW_KICK_LOG")" 2>/dev/null || true
    if command -v setsid >/dev/null 2>&1; then
      setsid sable-merge-gate preview --branch "$BRANCH" --repo "$CWD" \
        >> "$PREVIEW_KICK_LOG" 2>&1 < /dev/null &
    else
      nohup sable-merge-gate preview --branch "$BRANCH" --repo "$CWD" \
        >> "$PREVIEW_KICK_LOG" 2>&1 < /dev/null &
    fi
    PREVIEW_KICK_PID=$!
    disown "$PREVIEW_KICK_PID" 2>/dev/null || true
    sable_pp_trace "PREVIEW-KICK fired branch=${BRANCH} pid=${PREVIEW_KICK_PID}"
  else
    # Loud skip (SABLE-tb1y deliverable 3): never swallow a disposition on a
    # confirmed push. Costs latency only — Chuck's promote builds its own preview.
    echo "post-push-merge-notify: not kicking the merge preview — sable-merge-gate is not on PATH; Chuck's promote will build the preview at merge time (slower, not broken)."
    sable_pp_trace "PREVIEW-KICK skipped no-binary branch=${BRANCH}"
  fi
else
  sable_pp_trace "PREVIEW-KICK disabled (SABLE_PREVIEW_KICK=0) branch=${BRANCH}"
fi

# Try to detect PR URL via gh (best-effort, optional)
PR_URL=$(gh pr view --json url -q .url 2>/dev/null || echo "")

FILES_CSV=$(echo "$FILES" | tr '\n' ',' | sed 's/,$//')

# --- Overlap scan against UNCONTAINED BRANCH REFS (SABLE-pfbjw) ------------
# GROUND TRUTH, not declarations. The prior implementation intersected this
# push's files against `bd list --status=in_progress`'s wip_claims metadata,
# which is wrong in BOTH directions and both were measured live, not
# theorized: (A) FALSE POSITIVE — wip_claims is a claim, true only when
# written and never invalidated; SABLE-23upx's (wk-sable-screen) own
# wip_claims was byte-identical to its own pushed file list, and
# SABLE-be4lo.4 (wk-trains-fold)'s wip_claims contained
# bin/test_merge_gate_preview.py — the exact file its own push touched —
# while each bead was still in_progress at push time, so the branch matched
# itself. (B) FALSE NEGATIVE, the dangerous direction — `--status=in_progress`
# can never see the CLOSED-BUT-UNLANDED population, which in this fleet is
# the NORMAL end state (a worker's contract closes its bead before the branch
# is actually merged). Live case (optimus): skrdj pushed with NO warning
# while genuinely sharing .github/ci/shell-run-set.sh with qwthx, which was
# CLOSED and its branch still uncontained — invisible to an in_progress-only
# query. Silence was not clearance.
#
# So an "occupant" is now a fact about the git graph, not a claim about a
# bead: a remote wk-* branch counts iff its ref currently resolves AND its
# tip is NOT an ancestor of the integration branch (its work has not actually
# landed). This structurally also removes the self-match failure mode
# without a separate exclusion list — the pushing branch's own ref is
# skipped by name, and a bundled dispatch (multiple beads, one branch) is
# one git ref either way, so it can never appear as "another" occupant of
# itself.
#
# `git rev-parse --verify --quiet` everywhere, never a bare rev-parse: a bare
# rev-parse ECHOES an unresolvable ref back as if it were a real answer,
# which manufactured hundreds of false findings in an earlier ad hoc sweep
# (optimus). An empty tip is UNKNOWN and is skipped — never asserted
# "contained" (which would wrongly suppress a real warning) and never
# asserted "uncontained" (which would wrongly manufacture one). A reaped
# branch simply does not appear in `for-each-ref` at all, so a deliberately
# retired branch is correctly never an occupant without any extra check.
INTEGRATION_REF="origin/${INTEGRATION_BRANCH}"
INTEGRATION_TIP=$(git -C "$CWD" rev-parse --verify --quiet "$INTEGRATION_REF" 2>/dev/null || echo "")

OVERLAPS=""
if [ -z "$INTEGRATION_TIP" ]; then
  sable_pp_trace "OVERLAP-SCAN skipped unresolved-integration-ref ${INTEGRATION_REF}"
else
  CANDIDATE_REFS=$(git -C "$CWD" for-each-ref --format='%(refname:short)' 'refs/remotes/origin/wk-*' 2>/dev/null || echo "")
  OVERLAP_LINES=""
  while IFS= read -r CAND_REF; do
    [ -z "$CAND_REF" ] && continue
    CAND_SHORT="${CAND_REF#origin/}"
    [ "$CAND_SHORT" = "$BRANCH" ] && continue  # never compare the push to itself

    CAND_TIP=$(git -C "$CWD" rev-parse --verify --quiet "$CAND_REF" 2>/dev/null || echo "")
    [ -z "$CAND_TIP" ] && continue  # unresolvable — unknown, never asserted contained

    if git -C "$CWD" merge-base --is-ancestor "$CAND_TIP" "$INTEGRATION_TIP" 2>/dev/null; then
      continue  # CONTAINED — already landed, not an active occupant
    fi

    CAND_FILES=$(git -C "$CWD" diff "${INTEGRATION_REF}...${CAND_REF}" --name-only 2>/dev/null)
    [ -z "$CAND_FILES" ] && continue

    SHARED=$(FILES_CSV="$FILES_CSV" CAND_FILES="$CAND_FILES" python3 -c "
import os
pushed = set(f for f in os.environ.get('FILES_CSV', '').split(',') if f)
cand = set(f for f in os.environ.get('CAND_FILES', '').split(chr(10)) if f)
shared = sorted(pushed & cand)
if shared:
    print(','.join(shared))
" 2>/dev/null)
    [ -z "$SHARED" ] && continue

    # Label with any bead(s) declaring this branch — ANY status (the entire
    # point is that a CLOSED bead's branch can still be unlanded), falling
    # back to the bare branch name when bd is absent or nothing resolves.
    LABEL="$CAND_SHORT"
    BEAD_LABEL=$(bd list --status all --metadata-field "branch=$CAND_SHORT" --json --limit 0 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = []
if isinstance(data, list) and data:
    ids = [i.get('id', '') for i in data if isinstance(i, dict) and i.get('id')]
    if ids:
        print(','.join(ids))
" 2>/dev/null) || BEAD_LABEL=""
    [ -n "$BEAD_LABEL" ] && LABEL="${CAND_SHORT} (${BEAD_LABEL})"

    SHARED_BRIEF=$(printf '%s' "$SHARED" | sed 's/,/, /g')
    OVERLAP_LINES="${OVERLAP_LINES}${OVERLAP_LINES:+
}  - ${LABEL}: ${SHARED_BRIEF}"
  done <<< "$CANDIDATE_REFS"
  OVERLAPS="$OVERLAP_LINES"
fi

# SABLE-pfbjw: single-line, tmux-message-safe rendering of OVERLAPS (sable-msg
# types ONE line into the recipient's composer and submits it — an embedded
# newline would submit a partial message mid-composition). Empty whenever
# OVERLAPS is empty. Used below so the two live notifications name WHAT is
# shared and WITH WHAT, instead of the previous bare, unactionable
# "shares files with in-flight work" that cost a manual derivation to dismiss
# on every false positive.
OVERLAPS_BRIEF=$(printf '%s' "$OVERLAPS" | sed 's/^  - //' | tr '\n' ';' | sed 's/;$//' | sed 's/;/; /g')

# Brief basename list, shared by the message-based notifications below (the
# worker-landing wake and the Chuck handoff).
FILES_BRIEF=$(echo "$FILES" | sed 's#.*/##' | head -8 | tr '\n' ' ')

# SABLE-f916: both landing artifacts below (the live chuck message AND the
# durable for-chuck bead fallback) were byte-identical in framing to what a
# manager's deliberate, reviewed PR-ready sign-off would look like — Chuck had
# no mechanical way to tell "hook auto-detected a push" apart from "a manager
# actually reviewed this and accepts it." Incident 2026-07-15: an auto-notify
# for wk-bin-symlink-parity (SABLE-59t6.6) was queued+inspected as if
# PR-ready, but optimus had NOT accepted it (later rejected for false-green
# tests). This tag self-labels every auto-notify so it's grep-distinguishable
# from a real sign-off (which carries no such tag) — it does not change
# firing/registration behavior. Defined here (moved up from just above the
# Chuck-handoff block, SABLE-gx7p3) so the worker-landing wake below can also
# carry it — that message went out untagged and asserted closure it never
# observed.
AUTO_NOTIFY_TAG="[AUTO-NOTIFY: push detected by hook, NOT a manager sign-off]"

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
    # SABLE-gx7p3: the hook observed exactly one thing — a confirmed push. It
    # previously asserted "bead closed" and "for-chuck PR" unconditionally,
    # which is TRUE only when the branch's work bead is actually closed;
    # every other case (in_progress, unresolvable query) rendered the same
    # false terminal claim at the moment a lane manager is most likely to act
    # on it. Resolve the bead(s) here (only on the worker-landing path — a
    # manager's own push never reaches this branch) via the SAME structured
    # `branch` metadata sable-spawn-worker writes at dispatch time (mirrors
    # sable-reconcile-handoffs's find_work_bead_status resolver), and report
    # closure only when actually observed; otherwise say what is known (the
    # push, and the real status) and say explicitly this is not a sign-off.
    # A query failure leaves the set empty ("unknown") rather than defaulting
    # to a claim.
    #
    # CARDINALITY (live second instance, optimus/tarzan, SABLE-dhcyu bundle,
    # same evening as the original finding): a BUNDLED dispatch joins N>1
    # beads to the SAME branch metadata. Taking only the first matching
    # bead's status — this hook's own first-cut fix — reproduces the exact
    # defect one level down: if that first bead happens to be closed while a
    # sibling in the same bundle is still in_progress, the notify again
    # asserts a singular "bead closed" that is false about the unit of work.
    # So closure is asserted ONLY when EVERY bead sharing this branch's
    # metadata is closed; a partial bundle reports the set (closed count /
    # total, and which remain open) rather than a singular claim.
    BEAD_ID=""
    BEAD_TOTAL=0
    BEAD_CLOSED_COUNT=0
    BEAD_OPEN_LIST=""
    BEAD_ALL_IDS=""
    BEAD_QUERY=$(bd list --status all --metadata-field "branch=$BRANCH" --json --limit 0 2>/dev/null || echo "")
    if [ -n "$BEAD_QUERY" ]; then
      BEAD_ROWS=$(printf '%s' "$BEAD_QUERY" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = []
if isinstance(data, list):
    for item in data:
        if isinstance(item, dict) and item.get('status'):
            print(f\"{item.get('id', '')}\t{item.get('status', '')}\")
" 2>/dev/null) || BEAD_ROWS=""
      while IFS=$'\t' read -r bid bstatus; do
        [ -z "$bid" ] && continue
        BEAD_TOTAL=$((BEAD_TOTAL + 1))
        BEAD_ALL_IDS="${BEAD_ALL_IDS}${BEAD_ALL_IDS:+ }${bid}"
        if [ "$bstatus" = "closed" ]; then
          BEAD_CLOSED_COUNT=$((BEAD_CLOSED_COUNT + 1))
        else
          BEAD_OPEN_LIST="${BEAD_OPEN_LIST}${BEAD_OPEN_LIST:+, }${bid}(${bstatus})"
        fi
      done <<< "$BEAD_ROWS"
      BEAD_ID=$(printf '%s' "$BEAD_ALL_IDS" | cut -d' ' -f1)
    fi
    if [ "$BEAD_TOTAL" -gt 0 ] && [ "$BEAD_CLOSED_COUNT" -eq "$BEAD_TOTAL" ]; then
      if [ "$BEAD_TOTAL" -eq 1 ]; then
        LAND_MSG="${AUTO_NOTIFY_TAG} Worker landed: branch ${BRANCH} (${FILES_BRIEF}) pushed; bead ${BEAD_ID:-?} is CLOSED. Review the outcome — closed bead + for-chuck PR — and REVISE by re-spawning into the same worktree if wrong."
      else
        LAND_MSG="${AUTO_NOTIFY_TAG} Worker landed: branch ${BRANCH} (${FILES_BRIEF}) pushed; ALL ${BEAD_TOTAL} beads on this branch are CLOSED (${BEAD_ALL_IDS}). Review the outcome — closed beads + for-chuck PR — and REVISE by re-spawning into the same worktree if wrong."
      fi
    elif [ "$BEAD_TOTAL" -gt 0 ]; then
      LAND_MSG="${AUTO_NOTIFY_TAG} Worker pushed: branch ${BRANCH} (${FILES_BRIEF}). ${BEAD_CLOSED_COUNT}/${BEAD_TOTAL} bead(s) on this branch are closed — NOT all done (open: ${BEAD_OPEN_LIST}). This is NOT a completion signal; check \`bd show <id>\` for each and \`sable-worker-status\` before reviewing."
    else
      LAND_MSG="${AUTO_NOTIFY_TAG} Worker pushed: branch ${BRANCH} (${FILES_BRIEF}). No bead resolved via branch metadata — status unknown. The worker may still be running. This is NOT a completion signal; check \`bd show <bead>\` and \`sable-worker-status\` before reviewing."
    fi
    # SABLE-pfbjw: name WHAT is shared and WITH WHAT (OVERLAPS_BRIEF) instead
    # of the previous bare, unactionable "shares files with in-flight work" —
    # that phrasing gave the waking manager nothing to act on and cost a
    # manual derivation on every false positive to dismiss.
    [ -n "$OVERLAPS" ] && LAND_MSG="${LAND_MSG} OVERLAP-WARNING: shares files with in-flight work -- ${OVERLAPS_BRIEF}"
    if sable-msg "$SABLE_ID_NAME" "$LAND_MSG" --from worker >/dev/null 2>&1; then
      sable_pp_trace "WORKER-LAND-MSG sent -> ${SABLE_ID_NAME}"
    else
      sable_pp_trace "WORKER-LAND-MSG send FAILED -> ${SABLE_ID_NAME}"
    fi
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

# AUTO_NOTIFY_TAG (SABLE-f916) is defined earlier, above the worker-landing
# wake block, so that message can carry it too (SABLE-gx7p3).

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
  MSG="${AUTO_NOTIFY_TAG} PR ready from ${SABLE_ID_NAME}: branch ${BRANCH} (${FILES_BRIEF}). Review and merge into the integration branch, then report."
  # SABLE-pfbjw: name WHAT is shared and WITH WHAT (OVERLAPS_BRIEF) — see the
  # matching LAND_MSG comment above for why the bare phrase was unactionable.
  [ -n "$OVERLAPS" ] && MSG="${MSG} OVERLAP-WARNING: shares files with in-flight work -- ${OVERLAPS_BRIEF} -- sequence carefully."
  if sable-msg chuck "$MSG" --from "$SABLE_ID_NAME" >/dev/null 2>&1; then
    sable_pp_trace "HANDOFF chuck-msg confirmed"
    exit 0
  fi
  FALLBACK_REASON="sable-msg could not confirm delivery to chuck"
fi

# SABLE-riu: idempotency guard, mirroring bin/sable-reconcile-handoffs's
# predicate 3 / title_names_branch (SABLE-jfg6.3) so the push-based filer
# matches the pull-based floor. The message-first handoff above only
# `exit 0`s on a CONFIRMED send; a repeated fallback-path push of the SAME
# branch during a chuck-down/unreachable window (e.g. a worker re-pushing a
# fix) would otherwise file a new '[AUTO-NOTIFY] ... <branch>' bead every
# time, per SABLE-tb1y's optimus disposition (6 near-identical beads for one
# branch observed 2026-07-16). Skip the create if any open/in_progress
# for-chuck bead's title already names $BRANCH on a delimited-token boundary
# (so wk-foo does not false-match wk-foobar) — that earlier bead already
# covers this branch's handoff; update it by hand if the file list changed.
EXISTING_FOR_CHUCK=$(bd list --status open,in_progress --label for-chuck --json --limit 0 2>/dev/null || echo "")
if printf '%s' "$EXISTING_FOR_CHUCK" | BRANCH="$BRANCH" python3 -c "
import json, os, re, sys
branch = os.environ.get('BRANCH', '')
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if not isinstance(data, list):
    sys.exit(1)
pat = r'(?:^|[^\w./-])' + re.escape(branch) + r'(?:\$|[^\w./-])'
for item in data:
    if isinstance(item, dict) and re.search(pat, item.get('title') or ''):
        sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
  echo "post-push-merge-notify: skipping — an open/in_progress for-chuck bead already names branch ${BRANCH}; not filing a duplicate (${FALLBACK_REASON:-message delivery not confirmed})."
  sable_pp_trace "EXIT dup-for-chuck-bead branch=${BRANCH} reason=${FALLBACK_REASON:-unconfirmed}"
  exit 0
fi

# Build description (durable for-chuck bead — fallback path)
DESC_LINES=""
DESC_LINES="${DESC_LINES}${AUTO_NOTIFY_TAG} PR ready for review."
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

TITLE="[AUTO-NOTIFY] Review PR from ${SABLE_ID_NAME}: ${BRANCH}"

# SABLE-5hcg addendum: bd create used to end in a bare '|| true', so a failed
# bd invocation (dolt hiccup, transient lock, etc.) left the for-chuck channel
# dark even on THIS fallback path — the one thing standing between a pushed
# branch and a stranded merge. Capture the exit code and output, retry once
# (transient errors are common under fleet load), and if it still fails, say
# so loudly instead of swallowing it.
#
# SABLE-rq9k: --sandbox disables bd's Dolt auto-push. bd pushes to the shared
# Dolt remote on EVERY mutating write (create/update/close) by default; without
# this flag the for-chuck fallback bead was pushed to the remote as a pure hook
# side effect DURING a fleet-wide push hold (bead market-brief-package-1x8v,
# 2026-07-09). Dolt pushing is chuck-only by fleet convention, and a convention
# cannot bind a hook — so the hook must file this bead LOCAL-ONLY (still created
# and dolt-committed, never pushed); Chuck's batched pull+push carries it to the
# remote. --sandbox disables the push WITHOUT blocking the write (unlike
# --readonly, which would drop the handoff entirely). Applied to BOTH the initial
# create and the SABLE-5hcg retry below.
BD_CREATE_RC=0
BD_CREATE_OUT=$(bd create \
  --sandbox \
  --title "$TITLE" \
  --type=task \
  --priority=2 \
  --labels=for-chuck,coord \
  --description "$DESC_LINES" 2>&1) || BD_CREATE_RC=$?

if [ "$BD_CREATE_RC" -ne 0 ]; then
  BD_CREATE_RC=0
  BD_CREATE_OUT=$(bd create \
    --sandbox \
    --title "$TITLE" \
    --type=task \
    --priority=2 \
    --labels=for-chuck,coord \
    --description "$DESC_LINES" 2>&1) || BD_CREATE_RC=$?
fi

if [ "$BD_CREATE_RC" -ne 0 ]; then
  echo "post-push-merge-notify: FAILED to file durable for-chuck bead after retry (bd exit ${BD_CREATE_RC}) for branch ${BRANCH} — merge request may be stranded; file it manually with label for-chuck. bd output: ${BD_CREATE_OUT}"
  sable_pp_trace "HANDOFF for-chuck-bead FAILED rc=${BD_CREATE_RC} reason=${FALLBACK_REASON:-unconfirmed}"
else
  # Context line so the durable fallback is never silent again (SABLE-wvk9). A
  # PostToolUse hook's stdout surfaces to the agent whose Bash push triggered it,
  # so this records — for the worker and any log — WHY the message path did not
  # carry the handoff and that the durable for-chuck bead now covers the merge.
  echo "post-push-merge-notify: ${FALLBACK_REASON:-message delivery not confirmed} — filed durable for-chuck fallback bead (label for-chuck) for branch ${BRANCH}; chuck merges it from the pool."
  sable_pp_trace "HANDOFF for-chuck-bead filed reason=${FALLBACK_REASON:-unconfirmed}"
fi

exit 0
