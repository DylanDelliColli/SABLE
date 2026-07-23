#!/usr/bin/env bash
# test-overlap-dispatch-e2e.sh — INTEGRATION test for the SABLE-jd5fj.6
# overlap SCHEDULING CONSTRAINT, against a real bd (no mocks/stubs).
#
# Creates two real scratch beads in the project bd DB:
#   - bead A: in-progress, wip_claims metadata already established on a shared
#     file (simulating an earlier dispatch's claim).
#   - bead B: the bead about to be dispatched, whose description carries a
#     '## File footprint' section naming the SAME file (declared footprint,
#     not yet claimed — pre-dispatch-claim.sh and pre-dispatch-overlap.sh fire
#     on the same trigger with no ordering guarantee, so the overlap hook must
#     be able to read the declared footprint straight off the description).
#
# Asserts:
#   - dispatching B with no Serialize-with line is DENIED (permissionDecision).
#   - dispatching B with 'Serialize-with: <A>' is ALLOWED, and the
#     serialize_with tag lands in BOTH beads' real metadata (bd show --json) —
#     the for-chuck handoff reads this same dedicated metadata field.
#   - SABLE-47try: a bead whose '## File footprint' heading is PRESENT but names
#     no path does NOT silently proceed — the gate reports could-not-assess and
#     denies, instead of exiting 0 indistinguishably from a clean check.
#   - SABLE-47try complement, load-bearing: a bead that declares NO footprint at
#     all still dispatches while an overlapping bead is in-progress. Without
#     this leg the fix above could be a gate that never releases.
#
# Run with:
#   bash hooks/test/test-overlap-dispatch-e2e.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/pre-dispatch-overlap.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

# This whole suite IS the real-bd leg — there is no git-only half to fall
# back to (unlike test-dep-merge-state.sh). So bd absence (the ci-verify
# clean room, SABLE-59zu) skips it in full. That used to exit here with a
# single bare "SKIP:" line and no summary — indistinguishable, to anything
# scanning the CI log for a final tally, from a suite that has no tests at
# all. A suite that self-skips its most important (here: only) leg must
# never be able to print a clean summary that reads the same as having run
# it (SABLE-jd5fj.16) — so print the same "Tests | Passed | Failed | Skipped"
# shape the bd-present path below prints, with a non-zero Skipped count and a
# named reason. Keep REALBD_SUBTESTS in sync with the pass()/fail() count
# below (5 today) — this suite's own coverage is checked by
# hooks/test/test-shell-run-set-strict.sh case (h) and by
# hooks/test/test-ci-bd-coverage-gap.sh's negative control (bd present ->
# 7/7, no skips).
REALBD_SUBTESTS=7
if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP: bd not found on PATH — this suite requires a real bd (no mocks)"
  echo
  echo "=========================================="
  echo "Tests: 0 | Passed: 0 | Failed: 0 | Skipped: $REALBD_SUBTESTS (entire real-bd leg — bd absent, SABLE-59zu clean room; the real executor is chuck's local combined-tree impact tier, SABLE-jd5fj.13/.16)"
  echo "=========================================="
  exit 0
fi

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

AGENTS_YAML="$FIXTURE_DIR/agents.yaml"
cat > "$AGENTS_YAML" <<'YAML'
agents:
  optimus:
    type: epic_manager
YAML

EXEC_MODE="$FIXTURE_DIR/mode-exec.json"
echo '{"mode":"execution","since":"2026-07-21"}' > "$EXEC_MODE"

SHARED_FILE="hooks/foo-e2e-jd5fj6-test.sh"

make_input() { # <prompt>
  python3 -c "
import json, sys
d = {'tool_name':'Agent','tool_input':{'subagent_type':'general-purpose','prompt':sys.argv[1]},'hook_event_name':'PreToolUse','agent_type':'optimus'}
print(json.dumps(d))
" "$1"
}

run_hook() { # <prompt>
  make_input "$1" | \
    env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE -u SABLE_WORKER_PANE -u SABLE_BEAD \
        SABLE_AGENTS_YAML="$AGENTS_YAML" \
        SABLE_MODE_STATE="$EXEC_MODE" \
        bash "$HOOK" 2>/dev/null
}

metadata_field() { # <bead_id> <field>
  bd show "$1" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        print((d[0].get('metadata', {}) or {}).get(sys.argv[1], '') or '')
except Exception:
    pass
" "$2" 2>/dev/null || echo ""
}

cleanup_bead() { # <bead_id>
  [ -z "$1" ] && return 0
  bd update "$1" --sandbox --notes "[no-test] integration test scratch — safe to close" >/dev/null 2>&1 || true
  bd close "$1" --sandbox >/dev/null 2>&1 || true
}

# --- bead A: already in-progress, claim already established ---------------
BEAD_A=$(bd create --sandbox \
  --title="[int-test] jd5fj.6 overlap-e2e bead A" \
  --description="Scratch bead A for the SABLE-jd5fj.6 overlap-constraint e2e test." \
  --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

if [ -z "$BEAD_A" ]; then
  echo "SKIP (integration): could not create scratch bead A"
  exit 0
fi
trap 'cleanup_bead "$BEAD_A"; cleanup_bead "${BEAD_B:-}"; rm -rf "$FIXTURE_DIR"' EXIT
echo "Integration: created scratch bead A = $BEAD_A"

bd update "$BEAD_A" --sandbox --claim >/dev/null 2>&1 || true
bd update "$BEAD_A" --sandbox --set-metadata "wip_claims=$SHARED_FILE" >/dev/null 2>&1

# --- bead B: the dispatch target, declared footprint via description ------
BEAD_B=$(bd create --sandbox \
  --title="[int-test] jd5fj.6 overlap-e2e bead B" \
  --description="Scratch bead B for the SABLE-jd5fj.6 overlap-constraint e2e test.

## File footprint
$SHARED_FILE" \
  --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

if [ -z "$BEAD_B" ]; then
  echo "SKIP (integration): could not create scratch bead B"
  exit 0
fi
echo "Integration: created scratch bead B = $BEAD_B"

# --- Case 1: dispatch B, no Serialize-with -> DENIED -----------------------
OUT=$(run_hook "Work $BEAD_B")
if printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"' && printf '%s' "$OUT" | grep -q "$BEAD_A" \
   && printf '%s' "$OUT" | grep -q "$SHARED_FILE"; then
  pass "real bd: dispatching B with an overlapping declared footprint is DENIED, naming bead A and the file"
else
  fail "real bd: dispatching B with an overlapping declared footprint is DENIED, naming bead A and the file" \
       "got: ${OUT:-<empty>}"
fi

# --- Case 2: dispatch B with Serialize-with: <A> -> ALLOWED, tag lands -----
OUT=$(run_hook "Work $BEAD_B
Serialize-with: $BEAD_A")
if printf '%s' "$OUT" | grep -q 'SERIALIZE-WITH ACCEPTED' && ! printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"'; then
  pass "real bd: Serialize-with naming bead A ALLOWS the dispatch"
else
  fail "real bd: Serialize-with naming bead A ALLOWS the dispatch" "got: ${OUT:-<empty>}"
fi

SERIALIZE_B=$(metadata_field "$BEAD_B" "serialize_with")
SERIALIZE_A=$(metadata_field "$BEAD_A" "serialize_with")
if printf '%s' "$SERIALIZE_B" | grep -q "$BEAD_A" && printf '%s' "$SERIALIZE_A" | grep -q "$BEAD_B"; then
  pass "real bd: serialize-together tag lands in BOTH beads' real metadata (the for-chuck handoff field)"
else
  fail "real bd: serialize-together tag lands in BOTH beads' real metadata (the for-chuck handoff field)" \
       "B.serialize_with='$SERIALIZE_B' A.serialize_with='$SERIALIZE_A'"
fi

# --- Case 3 (SABLE-86bsl): grant survives an unrelated notes rewrite ------
# The grant above already landed in BOTH beads' serialize_with METADATA (Case
# 2/verified above). Now perform a routine, UNRELATED notes write on B (the
# SABLE-sm269-class clobber: bd update --notes REPLACES the whole notes field)
# and re-dispatch B with NO Serialize-with line in the prompt at all. The
# earlier grant must still be honored from metadata alone, and the metadata
# must still agree on both sides afterward.
bd update "$BEAD_B" --sandbox --notes "unrelated bookkeeping update, nothing to do with serialization" >/dev/null 2>&1

OUT=$(run_hook "Work $BEAD_B")
if printf '%s' "$OUT" | grep -q 'SERIALIZE-WITH ACCEPTED' && ! printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"'; then
  pass "real bd: serialize_grant_survives_notes_rewrite — grant still PERMITTED after an unrelated notes rewrite, with no Serialize-with in the prompt"
else
  fail "real bd: serialize_grant_survives_notes_rewrite — grant still PERMITTED after an unrelated notes rewrite, with no Serialize-with in the prompt" \
       "got: ${OUT:-<empty>}"
fi

SERIALIZE_B_AFTER=$(metadata_field "$BEAD_B" "serialize_with")
SERIALIZE_A_AFTER=$(metadata_field "$BEAD_A" "serialize_with")
if printf '%s' "$SERIALIZE_B_AFTER" | grep -q "$BEAD_A" && printf '%s' "$SERIALIZE_A_AFTER" | grep -q "$BEAD_B"; then
  pass "real bd: metadata still agrees on both beads after the notes rewrite"
else
  fail "real bd: metadata still agrees on both beads after the notes rewrite" \
       "B.serialize_with='$SERIALIZE_B_AFTER' A.serialize_with='$SERIALIZE_A_AFTER'"
fi

# --- Case 4 (SABLE-47try): unreadable footprint, against a REAL bd ----------
# Bead C's description carries a '## File footprint' HEADING that names no
# path. Bead A is still in-progress on SHARED_FILE. The gate cannot compare
# anything, so it must NOT silently proceed — the old
# `[ -z "$DISPATCH_FILES" ] && exit 0` exited 0 here, which is byte-identical
# downstream to a check that ran and found no overlap.
BEAD_C=$(bd create --sandbox \
  --title="[int-test] 47try unreadable-footprint bead C" \
  --description="Scratch bead C for the SABLE-47try could-not-assess e2e test.

## File footprint

## Test spec
nothing here" \
  --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

if [ -n "$BEAD_C" ]; then
  trap 'cleanup_bead "$BEAD_A"; cleanup_bead "${BEAD_B:-}"; cleanup_bead "${BEAD_C:-}"; cleanup_bead "${BEAD_D:-}"; rm -rf "$FIXTURE_DIR"' EXIT
  echo "Integration: created scratch bead C = $BEAD_C"
  OUT=$(run_hook "Work $BEAD_C")
  if printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"' \
     && printf '%s' "$OUT" | grep -q 'COULD NOT RUN'; then
    pass "real bd: an unreadable declared footprint does NOT silently proceed — could-not-assess deny"
  else
    fail "real bd: an unreadable declared footprint does NOT silently proceed — could-not-assess deny" \
         "got: ${OUT:-<empty>}"
  fi
else
  fail "real bd: an unreadable declared footprint does NOT silently proceed — could-not-assess deny" \
       "could not create scratch bead C"
fi

# --- Case 5 (SABLE-47try): the LOAD-BEARING complement, against a real bd ---
# Bead D declares NO footprint at all while bead A is still in-progress on
# SHARED_FILE. It must dispatch silently. This is the assertion that proves the
# fix did not turn the gate into one that can never release.
BEAD_D=$(bd create --sandbox \
  --title="[int-test] 47try no-footprint bead D" \
  --description="Scratch bead D for the SABLE-47try negative control. It declares no footprint and names no file-shaped token at all." \
  --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

if [ -n "$BEAD_D" ]; then
  echo "Integration: created scratch bead D = $BEAD_D"
  OUT=$(run_hook "Work $BEAD_D")
  if [ -z "$OUT" ]; then
    pass "real bd: a bead declaring NO footprint still dispatches (gate can still release)"
  else
    fail "real bd: a bead declaring NO footprint still dispatches (gate can still release)" \
         "got: $OUT"
  fi
else
  fail "real bd: a bead declaring NO footprint still dispatches (gate can still release)" \
       "could not create scratch bead D"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
