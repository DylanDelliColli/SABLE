#!/usr/bin/env bash
# pre-dispatch-overlap.sh — Declared file-footprint overlap SCHEDULING CONSTRAINT
# Trigger: PreToolUse:Agent | Timeout: 5000ms
#
# Reads claims from all in-progress beads (other than the ones being dispatched
# now) and compares them against this dispatch's declared file footprint.
#
# SABLE-jd5fj.6: this is a SCHEDULING CONSTRAINT, not advisory — an overlap
# with a DIFFERENT in-progress bead's footprint DENIES the dispatch outright.
# Two named outs:
#   1. Wait for the overlapping bead to clear (close/land), or
#   2. Dispatch with an explicit 'Serialize-with: <bead-id>' line naming the
#      overlapping bead — this ALLOWS the dispatch and tags BOTH beads'
#      `serialize_with` metadata so Chuck's for-chuck handoff sequences the
#      merges together instead of racing them.
#
# SABLE-e2ic3: a dispatch that declares NO footprint at all still releases —
# it collides with nothing the gate could check — but is announced LOUDLY as
# NO-DECLARATION (additionalContext, not a deny) rather than exiting silently.
# That is a DIFFERENT verdict from a footprint that was actually compared and
# found clean; before this fix both were the same silent exit-0-no-output.
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

# SABLE-jd5fj.6: the two named outs. 'Serialize-with: <bead-id>[, <bead-id>...]'
# in the dispatch prompt names the overlapping bead(s) the operator has decided
# to serialize with — matched against the ACTUAL overlap hits below, not taken
# on faith (naming an unrelated bead does not launder an unrelated overlap).
# Extracted BEFORE the DISPATCH_IDS pass below, whose input has the
# 'Serialize-with:' line stripped out first — otherwise the named partner bead
# (which is NOT part of this dispatch) gets swept into DISPATCH_IDS by the
# generic id regex and is then wrongly excluded from its own overlap check.
SERIALIZE_WITH=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
ids = set()
for m in re.finditer(r'Serialize-with:\s*([^\n]+)', text, re.IGNORECASE):
    for tok in re.split(r'[,\s]+', m.group(1).strip()):
        if tok:
            ids.add(tok)
print(' '.join(sorted(ids)))
" 2>/dev/null)

PROMPT_SANS_SERIALIZE=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
print(re.sub(r'(?i)Serialize-with:\s*[^\n]+', '', text))
" 2>/dev/null)

# Extract bead IDs from this dispatch
DISPATCH_IDS=$(echo "$PROMPT_SANS_SERIALIZE" | python3 -c "
import sys, re
text = sys.stdin.read()
ids = set(re.findall(r'\b((?:bd|sable|epic|task|bug|feat)-[a-zA-Z0-9_-]+)\b', text, re.IGNORECASE))
for i in sorted(ids):
    print(i)
" 2>/dev/null)

[ -z "$DISPATCH_IDS" ] && exit 0

# SABLE-86bsl: metadata-first read for an ALREADY-GRANTED serialization. The
# 'allow' branch below writes an accepted grant into BOTH beads' serialize_with
# METADATA field — durable, and the exact field this hook itself maintains. Read
# it back FIRST so a prior grant survives an unrelated notes rewrite (bd update
# --notes, a close/reopen, etc.) on a later respawn, instead of depending on the
# operator re-typing 'Serialize-with:' into the dispatch prompt every time. Fall
# back to a 'Serialize-with:' line in the bead's own NOTES only for beads
# authored before the metadata field existed; the PROMPT-parsed SERIALIZE_WITH
# above remains the mechanism for declaring a NEW grant on this dispatch.
SERIALIZE_WITH_STORED=$(for BID in $DISPATCH_IDS; do
  bd show "$BID" --json 2>/dev/null | python3 -c "
import json, re, sys

try:
    data = json.load(sys.stdin)
except Exception:
    data = []

if isinstance(data, list) and data:
    bead = data[0]
    ids = set()

    sw_meta = (bead.get('metadata', {}) or {}).get('serialize_with', '') or ''
    for tok in sw_meta.split(','):
        tok = tok.strip()
        if tok:
            ids.add(tok)

    notes = bead.get('notes', '') or ''
    for m in re.finditer(r'Serialize-with:\s*([^\n]+)', notes, re.IGNORECASE):
        for tok in re.split(r'[,\s]+', m.group(1).strip()):
            if tok:
                ids.add(tok)

    for i in sorted(ids):
        print(i)
" 2>/dev/null
done | sort -u)

SERIALIZE_WITH=$(SERIALIZE_WITH_PROMPT="$SERIALIZE_WITH" SERIALIZE_WITH_STORED="$SERIALIZE_WITH_STORED" python3 -c "
import os
prompt_ids = set(os.environ.get('SERIALIZE_WITH_PROMPT', '').split())
stored_ids = set(os.environ.get('SERIALIZE_WITH_STORED', '').split())
print(' '.join(sorted(prompt_ids | stored_ids)))
")

# Resolve the SHARED declared-footprint parser (SABLE-g0elq / SABLE-546m5 /
# SABLE-7gesd — the footprint-parser consolidation). Repo-relative sibling of
# THIS file, the same resolution inline-body-guard.sh uses: hooks are
# COPY-installed, so a *.py sibling resolves from the INSTALLED path, and
# sable-orchestration-install's closure scanner installs the lib (and its own
# transitive sable_* imports) into BASE/bin alongside this hook because the
# reference below appears on a NON-COMMENT line. There is no PATH form of a
# *.py lib — sable-bin-install deliberately skips them.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
FP_LIB="${HOOK_DIR}/../../bin/sable_footprint_lib.py"

if [ ! -f "$FP_LIB" ]; then
  # FAIL-OPEN, and LOUDLY. This gate is a scheduling constraint, not a
  # correctness gate: denying every dispatch because a library is missing is the
  # gate-that-can-never-release failure (SABLE-47try's do-not clause). But an
  # unannounced stand-down is exactly the wired-and-inert state SABLE-nn54x was
  # filed for — a hook that intercepts everything and checks nothing while
  # every presence probe reports it ACTIVE.
  python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': (
            'OVERLAP CHECK DID NOT RUN — bin/sable_footprint_lib.py was not found '
            'alongside this installed hook copy, so no declared-footprint overlap '
            'check ran on this dispatch. ALLOWING (fail-open): a scheduling '
            'constraint that cannot run must not block work. Re-run install.sh to '
            'restore the hook dependency closure (SABLE-nn54x).')
    }
}))
"
  exit 0
fi

# Aggregate declared-footprint file claims from this dispatch's beads.
#
# *** THIS BLOCK USED TO CARRY ITS OWN PARSER, AND THAT WAS THE DEFECT. ***
# (SABLE-7gesd.) It was the THIRD tokenizer over the '## File footprint'
# convention, and it disagreed with bin/sable-spawn-worker's overlap_check —
# the gate that is supposed to answer the SAME QUESTION. Two gates reading
# different inputs are worse than one gate, because each is silently correct
# about the subset it reads, so a bead was "declared" or "undeclared" depending
# on WHICH GATE WAS ASKING. It now pipes bd's JSON to the shared resolver and
# owns no tokenization at all; see the CLI seam at the bottom of the lib.
#
# The union the resolver reads is FOUR sources, not two: wip_claims metadata,
# footprint_writes metadata, the '## File footprint' section, and WIP-CLAIMS
# prose lines. `footprint_writes` is the SABLE-7gesd catch — a real, populated
# field that NO gate read at all, caught live at dispatch on SABLE-21rug.4
# (footprint_writes set, wip_claims None), which was covered only by luck of
# good authoring because it ALSO carried a section naming the same paths.
# Claims may not exist yet at pre-dispatch time (this hook and
# pre-dispatch-claim.sh fire on the same trigger with no ordering guarantee),
# which is why every source is unioned rather than tried in priority order.
#
# --scavenge keeps leg 3, the pre-convention file-shaped-token scrape over
# arbitrary prose, for beads authored before the footprint-section convention.
# It is DISPATCH-SIDE ONLY and the lib enforces that it fires only when no
# '## File footprint' heading was present: a scavenge over a heading that IS
# present and unreadable would launder a mis-authored declaration into a
# "successful" parse and re-open the door SABLE-47try closed.
#
# SABLE-47try: each bead is read as TWO streams, not one — 'f<path>' for a file
# and 'u<source>' for a footprint SOURCE THAT WAS PRESENT AND YIELDED NO PATH.
# The old single stream could not tell "this bead declares no footprint" (fine,
# dispatch) from "this bead's footprint could not be read" (the gate cannot run
# at all), so the `[ -z "$DISPATCH_FILES" ] && exit 0` below silently STOOD DOWN
# THE SCHEDULING CONSTRAINT for the second case.
DISPATCH_READ=$(for BID in $DISPATCH_IDS; do
  bd show "$BID" --json 2>/dev/null | python3 "$FP_LIB" --read-declared --scavenge 2>/dev/null
done | sort -u)

DISPATCH_FILES=$(printf '%s\n' "$DISPATCH_READ" | sed -n 's/^f//p')
DISPATCH_UNREADABLE=$(printf '%s\n' "$DISPATCH_READ" | sed -n 's/^u//p' | paste -sd, -)

# SABLE-47try: the could-not-assess door. Every footprint source this dispatch
# offered was PRESENT and named no path, so there is nothing to compare and the
# SCHEDULING CONSTRAINT CANNOT BE EVALUATED. That is not the same outcome as a
# completed check, and it must not exit 0 like one. Conservative default for a
# scheduling constraint that cannot run: DENY, naming what could not be read.
if [ -z "$DISPATCH_FILES" ] && [ -n "$DISPATCH_UNREADABLE" ]; then
  DISPATCH_UNREADABLE="$DISPATCH_UNREADABLE" DISPATCH_ID_LIST="$(echo $DISPATCH_IDS)" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': (
            'OVERLAP CHECK COULD NOT RUN — dispatched bead(s) '
            + os.environ.get('DISPATCH_ID_LIST', '')
            + ' declare a file footprint that could not be read ('
            + os.environ.get('DISPATCH_UNREADABLE', '')
            + ' present but naming no path). The overlap SCHEDULING CONSTRAINT '
              'cannot be evaluated against an unreadable footprint, and a check '
              'that did not run must not pass as one that found no overlap. Fix '
              'the footprint on the bead (a \'## File footprint\' section listing '
              'comma-separated paths), or remove the empty declaration entirely '
              'if this bead genuinely touches no declared files — a bead that '
              'declares NO footprint dispatches normally.')
    }
}))
"
  exit 0
fi

# No footprint source was present at all: this dispatch DECLARES NOTHING, which
# is legitimate and common. It must still be RELEASED — a gate that can never
# release is indistinguishable from correct caution (SABLE-47try) — but this
# is a DIFFERENT fact from a footprint that was actually compared and found
# clean (SABLE-e2ic3), and the two used to be the exact same silent exit-0
# no-output outcome. A manager reading a normal run could not tell "the
# constraint ran and released me" from "the constraint had nothing to check
# against" — measured at 96.4% of the live pool declaring nothing at all — so
# say so, loudly, on every dispatch that lands here, without refusing it.
if [ -z "$DISPATCH_FILES" ]; then
  DISPATCH_ID_LIST="$(echo $DISPATCH_IDS)" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': (
            'NO-DECLARATION — dispatched bead(s) '
            + os.environ.get('DISPATCH_ID_LIST', '')
            + ' declare NO file footprint at all (no wip_claims metadata, no '
              'footprint_writes metadata, no WIP-CLAIMS line, no '
            + chr(39) + '## File footprint' + chr(39) + ' section, no '
              'file-shaped token in the description). The overlap SCHEDULING '
              'CONSTRAINT has no input to compare this dispatch against, and '
              'cannot tell it apart from one that WAS checked and found '
              'clean. Dispatching normally -- this is NOT the same verdict '
              'as a checked CLEAR.')
    }
}))
"
  exit 0
fi

# Find all in-progress beads (status=in_progress) not in dispatch set
IN_PROGRESS=$(bd list --status=in_progress --json --limit 0 2>/dev/null || echo "[]")

# *** SABLE-7gesd, THE BEAD. THE IN-PROGRESS SIDE IS READ THROUGH THE SAME
# RESOLVER AS THE DISPATCHING SIDE. ***
#
# This block used to read `metadata.get('wip_claims')` AND NOTHING ELSE for each
# in-progress bead — no footprint section, no footprint_writes, no prose. So an
# in-progress bead whose footprint lives ONLY in its '## File footprint'
# section — the DECOMPOSITION-authored form SABLE-jd5fj.6 introduced and the form
# planners are TOLD to write — CONTRIBUTED NOTHING TO THIS COMPARISON. It could
# not be overlapped with. Meanwhile bin/sable-spawn-worker's own overlap_check
# read the full union for the same beads, so the two gates that exist to answer
# the same question answered it from different inputs.
#
# Failure shape: RELEASING AND SILENT. The gate returned "no overlap" having
# never looked at the declared footprint of the bead it should have collided
# with. pre-dispatch-claim.sh normally writes wip_claims metadata, which MASKED
# this whenever it had already fired — but this hook's own header states there is
# NO ORDERING GUARANTEE between the two hooks, and that is precisely the window
# this gate exists to cover.
#
# NOTE the asymmetry that is DELIBERATE and must stay: the dispatch read above
# passes --scavenge, this one does not. A prose scavenge on the in-progress side
# would make every bead claim every file its description happens to mention, and
# the gate would deny every dispatch — the exact mirror of the failure being
# fixed here, and trivially "safe" while destroying dispatch entirely.
IN_PROGRESS_READ=$(printf '%s' "$IN_PROGRESS" | python3 "$FP_LIB" --read-declared-list 2>/dev/null) || IN_PROGRESS_READ=""

OVERLAPS_JSON=$(IN_PROGRESS_JSON="$IN_PROGRESS" IN_PROGRESS_READ="$IN_PROGRESS_READ" DISPATCH_IDS="$DISPATCH_IDS" DISPATCH_FILES="$DISPATCH_FILES" python3 -c "
import json, os

dispatch_ids = set(os.environ.get('DISPATCH_IDS', '').split())
dispatch_files = set(os.environ.get('DISPATCH_FILES', '').split('\n'))
dispatch_files.discard('')

# Assignees come from the bd payload; the FILES come from the shared resolver's
# '<id>\tf<path>' stream. Splitting it this way keeps every tokenization decision
# in the lib and leaves this block doing set algebra only.
try:
    data = json.loads(os.environ.get('IN_PROGRESS_JSON') or '[]')
except Exception:
    data = []
if not isinstance(data, list):
    data = []
assignees = {i.get('id', ''): (i.get('assignee', '') or 'unassigned')
             for i in data if isinstance(i, dict)}

claimed = {}
for line in (os.environ.get('IN_PROGRESS_READ') or '').splitlines():
    if chr(9) not in line:
        continue
    bid, rest = line.split(chr(9), 1)
    if not bid or not rest.startswith('f'):
        continue  # 'u' lines are the unreadable-source channel, not a claim
    claimed.setdefault(bid, set()).add(rest[1:])

overlaps = []
for bid in sorted(claimed):
    if bid in dispatch_ids:
        continue
    shared = claimed[bid] & dispatch_files
    if shared:
        overlaps.append({
            'bead': bid,
            'assignee': assignees.get(bid, 'unassigned'),
            'files': sorted(shared),
        })

print(json.dumps(overlaps))
" 2>/dev/null) || OVERLAPS_JSON="[]"
[ -z "$OVERLAPS_JSON" ] && OVERLAPS_JSON="[]"
[ "$OVERLAPS_JSON" = "[]" ] && exit 0

DECISION=$(OVERLAPS_JSON="$OVERLAPS_JSON" SERIALIZE_WITH="$SERIALIZE_WITH" python3 -c "
import json, os

overlaps = json.loads(os.environ.get('OVERLAPS_JSON') or '[]')
serialize_with = set(os.environ.get('SERIALIZE_WITH', '').split())

uncovered = [o for o in overlaps if o['bead'] not in serialize_with]
covered = [o for o in overlaps if o['bead'] in serialize_with]

if uncovered:
    lines = '\n'.join(
        f\"  - {o['bead']} ({o['assignee']}, in-progress): {', '.join(o['files'])}\"
        for o in uncovered
    )
    reason = (
        'OVERLAP DETECTED — proposed dispatch shares declared file footprint '
        f'with active in-progress work:\n{lines}\n\n'
        'SCHEDULING CONSTRAINT: this dispatch is DENIED. Two outs -- '
        '(1) wait for the overlapping bead to clear, or '
        '(2) dispatch with an explicit Serialize-with: <bead-id> line naming '
        'the overlapping bead(s) above to proceed -- both merges will be tagged '
        'serialize-together for Chuck.'
    )
    print(json.dumps({'decision': 'deny', 'reason': reason}))
elif covered:
    tagged = sorted({o['bead'] for o in covered})
    print(json.dumps({'decision': 'allow', 'tagged': tagged}))
else:
    print(json.dumps({'decision': 'none'}))
" 2>/dev/null) || DECISION='{"decision": "none"}'

DECISION_KIND=$(printf '%s' "$DECISION" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('decision', 'none'))
except Exception:
    print('none')
" 2>/dev/null)

if [ "$DECISION_KIND" = "deny" ]; then
  REASON=$(printf '%s' "$DECISION" | python3 -c "
import json, sys
print(json.load(sys.stdin).get('reason', ''))
" 2>/dev/null)
  REASON="$REASON" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': os.environ.get('REASON', '')
    }
}))
"
  exit 0
fi

if [ "$DECISION_KIND" = "allow" ]; then
  TAGGED=$(printf '%s' "$DECISION" | python3 -c "
import json, sys
print(' '.join(json.load(sys.stdin).get('tagged', [])))
" 2>/dev/null)

  # Append (never overwrite — SABLE-szd) serialize_with on every bead on BOTH
  # sides of the accepted overlap, so Chuck's handoff can find the tag from
  # either bead's metadata.
  for DID in $DISPATCH_IDS; do
    for PARTNER in $TAGGED; do
      for A in "$DID" "$PARTNER"; do
        [ "$A" = "$DID" ] && B="$PARTNER" || B="$DID"
        CURRENT=$(bd show "$A" --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and data:
        print((data[0].get('metadata', {}) or {}).get('serialize_with', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")
        NEW=$(python3 -c "
import sys
current, partner = sys.argv[1], sys.argv[2]
existing = set(p.strip() for p in current.split(',') if p.strip())
existing.add(partner)
print(','.join(sorted(existing)))
" "$CURRENT" "$B")
        bd update "$A" --sandbox --set-metadata "serialize_with=$NEW" >/dev/null 2>&1 || true
      done
    done
  done

  TAGGED="$TAGGED" python3 -c "
import json, os
tagged = os.environ.get('TAGGED', '')
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': f'SERIALIZE-WITH ACCEPTED — overlap with {tagged} permitted; tagged serialize-together (serialize_with metadata) for the for-chuck handoff.'
    }
}))
"
  exit 0
fi

exit 0
