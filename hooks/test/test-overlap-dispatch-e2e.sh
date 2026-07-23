#!/usr/bin/env bash
# test-overlap-dispatch-e2e.sh — INTEGRATION test for the SABLE-jd5fj.6
# overlap SCHEDULING CONSTRAINT, against a real bd (no mocks/stubs).
#
# Creates two real scratch beads in an ISOLATED, per-run bd DB (SABLE-b0w8k —
# see below; never the shared live pool):
#   - bead A: in-progress, wip_claims metadata already established on a shared
#     file (simulating an earlier dispatch's claim).
#   - bead B: the bead about to be dispatched, whose description carries a
#     '## File footprint' section naming the SAME file (declared footprint,
#     not yet claimed — pre-dispatch-claim.sh and pre-dispatch-overlap.sh fire
#     on the same trigger with no ordering guarantee, so the overlap hook must
#     be able to read the declared footprint straight off the description).
#
# HERMETICITY (SABLE-b0w8k): this suite used to create these scratch beads in
# the SHARED LIVE bd pool, declaring a CONSTANT fixture footprint
# (hooks/foo-e2e-jd5fj6-test.sh). Two runs of the suite therefore claimed the
# same path as each other, and a leftover in-progress scratch bead from one
# run could be observed — and in one captured case, deny a REAL agent's
# dispatch — by a later run or by the live fleet. Fix shape 1 from the bead
# (the SABLE-jd5fj.15 isolated-BEADS_DB pattern, same recipe
# test-landing-pair-gate.sh already uses): every run gets its OWN throwaway bd
# DB, created fresh and destroyed on exit. Nothing this suite creates is ever
# visible outside the run that created it, so the old collision class cannot
# recur regardless of the fixture path's constancy.
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
# named reason. Keep REALBD_SUBTESTS in sync with the number of distinct
# pass()/fail() assertion titles below (12 today, SABLE-b0w8k added the
# foreign-claimant, zero-residue, and plant-and-fail assertions) — this
# suite's own coverage is checked by hooks/test/test-shell-run-set-strict.sh
# case (h) and by hooks/test/test-ci-bd-coverage-gap.sh's negative control,
# which compares bd-present vs bd-absent subtest counts dynamically rather
# than pinning this exact number.
REALBD_SUBTESTS=12
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

# ---------------------------------------------------------------------------
# Isolated per-run bd DB (SABLE-b0w8k, fix shape 1 / SABLE-jd5fj.15 pattern).
# A FRESH DB every run, never a copy of and never touching the real shared
# pool. Exported so every bare `bd ...` call below — and the hook subprocess
# `run_hook` launches further down — resolves against THIS run's DB instead
# of auto-discovering the real one from CWD.
# ---------------------------------------------------------------------------
BEADS_ROOT="$FIXTURE_DIR/beads"
mkdir -p "$BEADS_ROOT"
# --prefix MUST be one pre-dispatch-overlap.sh's own DISPATCH_IDS regex
# recognizes (bd|sable|epic|task|bug|feat, case-insensitive) — any other
# prefix produces bead IDs the hook's own id-extraction never matches, so
# DISPATCH_IDS comes back empty and the hook silently no-ops on every case
# (discovered running this suite against SABLE-b0w8k's isolated DB fix).
BD_INIT_OUT="$(cd "$BEADS_ROOT" && env BD_NON_INTERACTIVE=1 bd init --prefix=sable 2>&1)"
if [ ! -d "$BEADS_ROOT/.beads" ]; then
  echo "FATAL: could not initialize an isolated per-run bd DB: $BD_INIT_OUT"
  exit 2
fi
export BEADS_DB="$BEADS_ROOT/.beads"

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

# --- TEST SPEC bullet 1: a foreign bead in ANY state claiming the fixture --
# path must not break the suite. Plant one directly in THIS run's own
# isolated DB — CLOSED, so it cannot itself participate in any overlap
# decision — before this run's own bead A/B exist, standing in for "a run
# that used this same DB before us left a claimant behind." The isolated-DB
# fix (above) means that scenario can no longer arise by ACCIDENT across real
# runs; this proves the suite also tolerates it if it ever did.
BEAD_FOREIGN=$(bd create --sandbox \
  --title="[int-test] b0w8k foreign pre-existing claimant" \
  --description="Scratch foreign bead for the SABLE-b0w8k hermeticity test-spec bullet 1." \
  --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)
if [ -n "$BEAD_FOREIGN" ]; then
  bd update "$BEAD_FOREIGN" --sandbox --set-metadata "wip_claims=$SHARED_FILE" >/dev/null 2>&1
  bd close "$BEAD_FOREIGN" --sandbox >/dev/null 2>&1
  echo "Integration: planted foreign closed claimant = $BEAD_FOREIGN (test-spec bullet 1)"
fi

# --- bead A: already in-progress, claim already established ---------------
BEAD_A=$(bd create --sandbox \
  --title="[int-test] jd5fj.6 overlap-e2e bead A" \
  --description="Scratch bead A for the SABLE-jd5fj.6 overlap-constraint e2e test." \
  --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

if [ -z "$BEAD_A" ]; then
  echo "SKIP (integration): could not create scratch bead A"
  exit 0
fi
trap 'cleanup_bead "${BEAD_FOREIGN:-}"; cleanup_bead "$BEAD_A"; cleanup_bead "${BEAD_B:-}"; rm -rf "$FIXTURE_DIR"' EXIT
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

# TEST SPEC bullet 1, non-vacuity: the CLOSED foreign claimant planted above
# (also on $SHARED_FILE) must not be what the deny names, nor break it —
# bead A, the genuinely in-progress claimant, must still be the one cited.
if [ -n "${BEAD_FOREIGN:-}" ] && printf '%s' "$OUT" | grep -q "$BEAD_A" \
   && ! printf '%s' "$OUT" | grep -q "$BEAD_FOREIGN"; then
  pass "real bd: a foreign CLOSED bead also claiming the fixture path does not break or get mistaken for the live deny"
elif [ -z "${BEAD_FOREIGN:-}" ]; then
  fail "real bd: a foreign CLOSED bead also claiming the fixture path does not break or get mistaken for the live deny" \
       "could not create the foreign plant bead"
else
  fail "real bd: a foreign CLOSED bead also claiming the fixture path does not break or get mistaken for the live deny" \
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
  trap 'cleanup_bead "${BEAD_FOREIGN:-}"; cleanup_bead "$BEAD_A"; cleanup_bead "${BEAD_B:-}"; cleanup_bead "${BEAD_C:-}"; cleanup_bead "${BEAD_D:-}"; rm -rf "$FIXTURE_DIR"' EXIT
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

# --- Zero residue: every bead this run created is closed ------------------
# Belt-and-suspenders on top of the isolated DB itself (which is destroyed by
# the EXIT trap regardless): explicitly close the beads created above, then
# assert none remain in-progress in THIS run's own DB before it is torn down.
cleanup_bead "$BEAD_A"; cleanup_bead "${BEAD_B:-}"; cleanup_bead "${BEAD_C:-}"
cleanup_bead "${BEAD_D:-}"; cleanup_bead "${BEAD_FOREIGN:-}"
RESIDUE=$(bd list --status in_progress --json 2>/dev/null)
if [ "$RESIDUE" = "[]" ] || [ -z "$RESIDUE" ]; then
  pass "zero residue: no in-progress beads remain in this run's DB after cleanup"
else
  fail "zero residue: no in-progress beads remain in this run's DB after cleanup" \
       "got: $RESIDUE"
fi

# ---------------------------------------------------------------------------
# PLANT-AND-FAIL (SABLE-5lli.7) — the hermeticity fix above must not be a
# vacuous no-op. Prove a NEW, throwaway hermeticity probe (never the isolated
# DB the suite itself uses, and never the real project pool) actually goes
# RED when re-pointed at a SHARED CONSTANT DB across two "runs" — reproducing
# the established defect (section 1 of SABLE-b0w8k: a constant fixture
# location lets one run observe another's leftover bead) — before trusting
# that the same probe reports GREEN when each "run" gets its own per-run
# unique DB, which is the actual fix this suite now uses throughout.
# ---------------------------------------------------------------------------
hermeticity_probe_leaks() { # <db_dir> -> 0 if a prior call's probe bead is
                            # already present (leak), 1 if this DB is clean
  local db="$1"
  if [ ! -d "$db/.beads" ]; then
    mkdir -p "$db"
    # -u BEADS_DB: this function runs after the suite's own BEADS_DB export
    # above. Left ambient, `bd init` follows THAT var instead of CWD and
    # "initializes" the already-initialized isolated DB instead of this
    # probe's own dir — silently leaving $db/.beads never created.
    (cd "$db" && env -u BEADS_DB BD_NON_INTERACTIVE=1 bd init --prefix=herm >/dev/null 2>&1)
  fi
  local existing
  existing=$(BEADS_DB="$db/.beads" bd list --title-contains "hermeticity probe" --json 2>/dev/null)
  BEADS_DB="$db/.beads" bd create --sandbox -q \
    --title="[int-test] hermeticity probe" \
    --description="[no-test] SABLE-5lli.7 plant-and-fail scratch — $SHARED_FILE" \
    --type=task >/dev/null 2>&1
  [ "$existing" != "[]" ] && [ -n "$existing" ]
}

PLANT_DIR="$(mktemp -d)"
if hermeticity_probe_leaks "$PLANT_DIR" >/dev/null 2>&1; then
  fail "PLANT-AND-FAIL precondition: a fresh shared-constant DB starts clean before the plant" \
       "unexpected pre-existing probe bead on the very first call"
else
  pass "PLANT-AND-FAIL precondition: a fresh shared-constant DB starts clean before the plant"
fi
if hermeticity_probe_leaks "$PLANT_DIR"; then
  pass "PLANT-AND-FAIL: re-pointing two runs at the SAME constant DB reproduces cross-run leakage — hermeticity check correctly goes RED"
else
  fail "PLANT-AND-FAIL: re-pointing two runs at the SAME constant DB reproduces cross-run leakage — hermeticity check correctly goes RED" \
       "second call on the shared DB did not observe the first call's probe bead — the plant did not arm"
fi
rm -rf "$PLANT_DIR"

# Reuse this run's OWN already-initialized isolated DB as one of the two
# "runs" here — it is already a per-run-unique DB (that is the fix being
# proven) and by this point carries no "hermeticity probe" bead, so it is a
# safe, cheaper stand-in for a fresh mktemp+bd-init (each bd init cold-starts
# an embedded Dolt server; avoiding a redundant one keeps this suite's
# runtime reasonable).
FRESH_B="$(mktemp -d)"
hermeticity_probe_leaks "$BEADS_ROOT" >/dev/null 2>&1
if hermeticity_probe_leaks "$FRESH_B"; then
  fail "RESTORE GREEN: two runs on their OWN per-run-unique DBs do not leak into each other (the actual fix)" \
       "run B observed run A's probe bead despite separate scratch DBs"
else
  pass "RESTORE GREEN: two runs on their OWN per-run-unique DBs do not leak into each other (the actual fix)"
fi
rm -rf "$FRESH_B"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
