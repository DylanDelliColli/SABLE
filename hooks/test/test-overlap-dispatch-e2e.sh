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
#   - an unrelated notes rewrite cannot erase that durable metadata grant.
#
# The exhaustive decision matrix (closed claimants, unreadable declarations,
# no declarations, unrelated grants, and negative controls) belongs to the
# stubbed unit suite in test-overlap-constraint.sh. Repeating those states
# through a cold Dolt store added process cost without crossing a new boundary.
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
# pass()/fail() assertion titles below (8 today: five authority assertions and
# three hermeticity plant assertions) — this
# suite's own coverage is checked by hooks/test/test-shell-run-set-strict.sh
# case (h) and by hooks/test/test-ci-bd-coverage-gap.sh's negative control,
# which compares bd-present vs bd-absent subtest counts dynamically rather
# than pinning this exact number.
REALBD_SUBTESTS=8
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

# --- bead A: already in-progress, claim already established ---------------
BEAD_A=$(bd create --sandbox \
  --title="[int-test] jd5fj.6 overlap-e2e bead A" \
  --description="Scratch bead A for the SABLE-jd5fj.6 overlap-constraint e2e test." \
  --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

if [ -z "$BEAD_A" ]; then
  echo "SKIP (integration): could not create scratch bead A"
  exit 0
fi
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
probe_present() { # <db_dir> -> 0 when a probe bead is visible
  local db="$1"
  local existing
  existing=$(BEADS_DB="$db/.beads" bd list --title-contains "hermeticity probe" --json 2>/dev/null)
  [ "$existing" != "[]" ] && [ -n "$existing" ]
}

plant_probe() { # <db_dir>
  local db="$1"
  BEADS_DB="$db/.beads" bd create --sandbox -q \
    --title="[int-test] hermeticity probe" \
    --description="[no-test] SABLE-5lli.7 plant-and-fail scratch — $SHARED_FILE" \
    --type=task >/dev/null 2>&1
}

PLANT_DIR="$(mktemp -d)"
# The defect is two runs sharing one store, not how that store was initialized.
# Copy the already-quiescent real store so the plant spends its time on the
# discriminating read/write polarity instead of another cold `bd init`.
cp -a -f "$BEADS_ROOT/." "$PLANT_DIR/"
if probe_present "$PLANT_DIR"; then
  fail "PLANT-AND-FAIL precondition: a fresh shared-constant DB starts clean before the plant" \
       "unexpected pre-existing probe bead on the very first call"
else
  pass "PLANT-AND-FAIL precondition: a fresh shared-constant DB starts clean before the plant"
fi
plant_probe "$PLANT_DIR"
if probe_present "$PLANT_DIR"; then
  pass "PLANT-AND-FAIL: re-pointing two runs at the SAME constant DB reproduces cross-run leakage — hermeticity check correctly goes RED"
else
  fail "PLANT-AND-FAIL: re-pointing two runs at the SAME constant DB reproduces cross-run leakage — hermeticity check correctly goes RED" \
       "second call on the shared DB did not observe the first call's probe bead — the plant did not arm"
fi
rm -rf "$PLANT_DIR"

# Reuse this run's OWN already-initialized isolated DB as one of the two
# "runs" here — it is already a per-run-unique DB (that is the fix being
# proven) and by this point carries no "hermeticity probe" bead, so it is a
# safe, cheaper stand-in for a fresh mktemp+bd-init.
FRESH_B="$(mktemp -d)"
# Two deep copies are the actual per-run isolation shape. Seed B before A is
# mutated, then prove A's real bd create is invisible from B.
cp -a -f "$BEADS_ROOT/." "$FRESH_B/"
plant_probe "$BEADS_ROOT"
if probe_present "$FRESH_B"; then
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
