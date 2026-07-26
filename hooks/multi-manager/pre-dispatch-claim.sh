#!/usr/bin/env bash
# pre-dispatch-claim.sh — Pre-write WIP file claims to bead notes at dispatch time
# Trigger: PreToolUse:Agent | Timeout: 5000ms
#
# Reads the bead description (extracts bead ID from dispatch prompt), parses
# referenced file paths, and writes them to the bead's `wip_claims` metadata
# field as a comma-separated list (SABLE-szd: NOT notes — see below).
#
# SABLE-jd5fj.6: a planner-authored '## File footprint' description section
# (DECOMPOSITION-authored, dedicated wip_claims-metadata-pattern extension) is
# the authoritative claim source when present — see below — since it is the
# declared footprint pre-dispatch-overlap.sh's scheduling constraint enforces.
#
# Closes the dispatch-time race condition where worker B dispatches before
# worker A has started editing — claims now exist at dispatch.
#
# Companion: edit-write-claim-reconciler.sh appends emergent claims as workers edit.
#
# SABLE-d5iku: this hook ALSO carries the unmerged-blocker warning. See the
# block at the bottom of this file — the bead's rationale for putting it here
# is that this hook already runs on every worker dispatch and already reads
# bead state, so it is the cheapest place to surface a dependency that
# released on CLOSE while its blocker's BRANCH is still in the merge queue.
#
# Skips if: no bead ID inferrable, no file paths in description, worker/bare
# subagent context (manager-typed subagents ARE governed — they dispatch workers).

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

# Extract bead IDs from prompt — common formats: "bd-123", "bead bd-abc-1", "claim bd-x"
BEAD_IDS=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
ids = set(re.findall(r'\b((?:bd|sable|epic|task|bug|feat)-[a-zA-Z0-9]{2,6}(?:\.[0-9]+)*)\b(?!-[A-Za-z0-9])', text, re.IGNORECASE))
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

  # SABLE-jd5fj.6: footprint->wip_claims wiring. A planner-authored '## File
  # footprint' description section (authored at DECOMPOSITION — see this
  # bead's own description for the format) is the AUTHORITATIVE declared
  # footprint: parse it in preference to the generic file-extension regex,
  # since it may name extension-less scripts (e.g. bin/sable-spawn-worker)
  # the regex would miss, and it is the exact list pre-dispatch-overlap.sh's
  # scheduling constraint compares against. Falls back to the generic regex
  # for beads authored before the footprint-section convention.
  FILES=$(echo "$DESC" | python3 -c "
import sys, re
text = sys.stdin.read()
paths = set()
section = re.search(r'^##\s*File footprint\s*\n(.+?)(?=\n##\s|\Z)', text,
                     re.MULTILINE | re.DOTALL)
if section:
    for part in section.group(1).split(','):
        part = part.strip()
        if not part:
            continue
        paths.add(part.split()[0])
else:
    for m in re.finditer(r'(?:^|[\s\(\[\"\\'])((?:[\w\-./]+/)?[\w\-./]+\.(?:ts|tsx|js|jsx|py|rs|go|java|rb|md|yaml|yml|toml|json|sh|sql|css|scss|html))(?=[\s\)\]\"\\',:;]|$)', text, re.MULTILINE):
        paths.add(m.group(1))
print(','.join(sorted(paths)))
" 2>/dev/null || echo "")

  [ -z "$FILES" ] && continue

  # SABLE-szd: claims live in the dedicated `wip_claims` metadata field, NOT
  # bead notes. `bd update --notes` OVERWRITES the whole notes field rather
  # than appending — any later notes update from anywhere in a bead's life
  # (e.g. a manager's routine review-notes write) silently wiped this claim
  # line, breaking the overlap detection in pre-dispatch-overlap.sh and
  # post-push-merge-notify.sh for that bead. Metadata is a separate column
  # bd never touches on a --notes write, so it survives regardless of what
  # else updates notes afterward. Regression coverage:
  # hooks/test/test-pre-dispatch-claim.sh (SABLE-6la1, real bd).
  #
  # SABLE-6la1 verification note (2026-07-21, bd 1.0.5): use `--set-metadata
  # key=value` here, never the `--metadata '{json}'` blob form, as a matter of
  # intent — a blob write only ever names the key(s) you're touching, and a
  # careless caller could hand-write one that omits wip_claims. Empirically
  # (sandboxed probes against the installed bd 1.0.5), the blob form currently
  # MERGES per-key exactly like --set-metadata rather than replacing the whole
  # map, so an omitted key is not actually at risk today — but that merge
  # behavior is not documented/guaranteed by `bd update --help`, so don't rely
  # on it. If a future bd version changes `--metadata` back to a full-map
  # replace, any blob write here would silently clobber wip_claims. See
  # SABLE-gkofi (filed to reconcile this with SABLE-szd/SABLE-sm269, whose
  # descriptions assumed the blob form already replaces).
  CURRENT_CLAIMS=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and data:
        print((data[0].get('metadata', {}) or {}).get('wip_claims', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

  [ -n "$CURRENT_CLAIMS" ] && continue  # claims already established

  # SABLE-lfql: --sandbox disables bd's Dolt auto-push (SABLE-rq9k). bd pushes
  # to the shared Dolt remote on EVERY mutating write (create/update/close) by
  # default; without this flag, every dispatch pushed WIP-CLAIMS bookkeeping
  # to the remote as a pure hook side effect — the exact chuck-only-convention
  # violation behind the 2026-07-09 cross-fleet corruption incident.
  # --sandbox disables the push WITHOUT blocking the write (unlike --readonly,
  # which would drop it); Chuck's batched pull+push carries it later.
  bd update "$BEAD_ID" --sandbox --set-metadata "wip_claims=$FILES" >/dev/null 2>&1 || true
done

# --- Unmerged-blocker warning (SABLE-d5iku) ---------------------------------
# `bd ready` releases a dependent the instant its blocker's STATUS becomes
# closed. What a dependent sequenced behind a blocker for STRUCTURAL reasons
# needs is the blocker's CODE on the branch it forks from — and those two
# events are separated by the whole merge queue. So every `bd dep add` used for
# structural sequencing carries a false-release window in which the ready
# signal says go and the tree says not yet, and the false release looks
# EXACTLY like a correct one (live instance: SABLE-78kxu released by a closed
# SABLE-9boz4 whose wk-pin-refresh branch was still queued; a worker dispatched
# then would have built against the layout the dependency existed to replace).
#
# ADVISORY, NOT A CONSTRAINT — unlike pre-dispatch-overlap.sh, this emits
# `additionalContext` and never a `deny`. Merge state has genuine unresolvable
# cases (pruned branches, stale remote-tracking refs, beads with no branch at
# all), so a withhold here would trade the false-go for a false-block. Option
# (b) of the bead — readiness that is merge-aware at the source — belongs in bd
# core, which is not this repo.
#
# Failsafe by construction: every leg degrades to silence. A missing tool, a bd
# hiccup, or a git error costs the warning, never the dispatch. Disable with
# SABLE_DEP_MERGE_GUARD=0.
if [ "${SABLE_DEP_MERGE_GUARD:-1}" = "1" ]; then
  # Resolution order for the checker: explicit override, then PATH (the
  # installed spine bin), then the repo-relative sibling for a dev checkout
  # whose bins are not installed. Absent everywhere => skip silently.
  DEP_CHECK_BIN="${SABLE_DEP_CHECK_BIN:-}"
  if [ -z "$DEP_CHECK_BIN" ] && command -v sable-dep-check >/dev/null 2>&1; then
    DEP_CHECK_BIN=$(command -v sable-dep-check)
  fi
  if [ -z "$DEP_CHECK_BIN" ]; then
    REPO_SIBLING="$(dirname "${BASH_SOURCE[0]}")/../../bin/sable-dep-check"
    [ -x "$REPO_SIBLING" ] && DEP_CHECK_BIN="$REPO_SIBLING"
  fi

  if [ -n "$DEP_CHECK_BIN" ] && [ -x "$DEP_CHECK_BIN" ]; then
    # Merge state is a property of the REPO being dispatched into, so judge it
    # in the dispatch's cwd (same reasoning as SABLE-041 in
    # post-push-merge-notify.sh: never assume the hook's own $PWD).
    DISPATCH_CWD=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('cwd', '') or '')
" 2>/dev/null) || DISPATCH_CWD=""
    [ -z "$DISPATCH_CWD" ] && DISPATCH_CWD="$PWD"

    # --format=hook prints the warning block and NOTHING when clean, so the
    # emptiness test below is the whole decision. Exit code is deliberately
    # ignored (3 = findings) — the text is the signal.
    #
    # Bounded by `timeout` because the checker costs two batched `bd`
    # invocations and bd is ~0.8s of process startup a call — with the claims
    # loop above already spending its own, a dispatch naming several beads can
    # outlast this hook's 5s budget (SABLE-5r5vq tracks that). On a timeout
    # the warning is simply lost (the claims above are already written, so
    # nothing else is at risk), which is the right trade for an advisory
    # surface. Tune with SABLE_DEP_CHECK_TIMEOUT.
    # shellcheck disable=SC2086
    DEP_WARN=$(timeout -k 1 "${SABLE_DEP_CHECK_TIMEOUT:-4}" \
                 "$DEP_CHECK_BIN" --format=hook --repo "$DISPATCH_CWD" $BEAD_IDS 2>/dev/null) || true

    if [ -n "$DEP_WARN" ]; then
      DEP_WARN="$DEP_WARN" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': os.environ.get('DEP_WARN', '')
    }
}))
" 2>/dev/null || true
    fi
  fi
fi

exit 0
