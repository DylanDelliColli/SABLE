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
# pass()/fail() assertion titles below (10 today: five authority assertions,
# two structural batching controls, and three hermeticity plant assertions) — this
# suite's own coverage is checked by hooks/test/test-shell-run-set-strict.sh
# case (h) and by hooks/test/test-ci-bd-coverage-gap.sh's negative control,
# which compares bd-present vs bd-absent subtest counts dynamically rather
# than pinning this exact number.
REALBD_SUBTESTS=10
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
#
# -u BEADS_DB (SABLE-sx1rb): when this suite runs under the impact tier, the
# tier has ALREADY exported BEADS_DB pointing at ITS OWN isolated DB. Left
# ambient, `bd init` follows that var instead of CWD and "initializes" the
# tier's already-initialized DB instead of building this suite's own —
# aborting with "This workspace is already initialized" and reading as a
# content RED on whichever branch happened to be the first to enter the
# tier. Same pattern this file already applies at the hermeticity probe
# below (line ~364) and test-dep-merge-state.sh applies at its own init.
BD_INIT_OUT="$(cd "$BEADS_ROOT" && env -u BEADS_DB BD_NON_INTERACTIVE=1 bd init --prefix=sable --skip-agents --skip-hooks --quiet 2>&1)"
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
FIXTURE_SEED_WRITES=0
PROBE_READ_STATES=()
BEAD_A="sable-overlap-a"
BEAD_B="sable-overlap-b"

probe_present() { # <db_dir> <state_label> -> 0 when the planted overlap bead is visible
  local db="$1" state="$2"
  local existing
  PROBE_READ_STATES+=("$state")
  existing=$(BEADS_DB="$db/.beads" bd show "$BEAD_A" --json 2>/dev/null) || return 1
  [ "$existing" != "[]" ] && [ -n "$existing" ]
}

# Snapshot the initialized-but-unseeded store. The one batch import below is
# both the overlap fixture transition and the hermeticity plant: the shared
# store must see BEAD_A afterward, while this pre-mutation copy must not.
FRESH_B="$FIXTURE_DIR/fresh-b"
mkdir -p "$FRESH_B"
cp -a -f "$BEADS_ROOT/." "$FRESH_B/"
if probe_present "$BEADS_ROOT" shared-before-seed; then
  PLANT_PRECONDITION_CLEAN=0
else
  PLANT_PRECONDITION_CLEAN=1
fi

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

METADATA_READ_LOG="$FIXTURE_DIR/metadata-read.log"
: > "$METADATA_READ_LOG"
metadata_pair() { # <bead_id_a> <bead_id_b> <field> -> two NUL-delimited values
  printf '%s %s\n' "$1" "$2" >> "$METADATA_READ_LOG"
  bd show "$1" "$2" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = []
by_id = {item.get('id', ''): item for item in d if isinstance(item, dict)} if isinstance(d, list) else {}
for bead_id in sys.argv[1:3]:
    item = by_id.get(bead_id, {})
    value = (item.get('metadata', {}) or {}).get(sys.argv[3], '') or ''
    sys.stdout.write(str(value) + '\\0')
" "$1" "$2" "$3" 2>/dev/null
}

# --- atomic overlap + hermeticity fixture seed -----------------------------
if ! bd import - --sandbox >/dev/null 2>&1 <<JSONL
{"id":"$BEAD_A","title":"[int-test] jd5fj.6 overlap-e2e bead A / hermeticity probe","description":"Scratch bead A for the SABLE-jd5fj.6 overlap-constraint e2e test.","issue_type":"task","status":"in_progress","assignee":"optimus","metadata":{"wip_claims":"$SHARED_FILE"}}
{"id":"$BEAD_B","title":"[int-test] jd5fj.6 overlap-e2e bead B","description":"Scratch bead B for the SABLE-jd5fj.6 overlap-constraint e2e test.\n\n## File footprint\nbin/a.py\nbin/b.py\nbin/sable-tool\n$SHARED_FILE","issue_type":"task","status":"open"}
JSONL
then
  echo "SKIP (integration): could not import scratch overlap beads"
  exit 0
fi
FIXTURE_SEED_WRITES=$((FIXTURE_SEED_WRITES + 1))
echo "Integration: created scratch bead A = $BEAD_A"
echo "Integration: created scratch bead B = $BEAD_B"

# --- Case 1: dispatch B, no Serialize-with -> DENIED -----------------------
# The shared collision is deliberately FOURTH in a newline-authored section.
# The old writer/shell tokenizer kept only bin/a.py, so this real-bd case is the
# unsafe narrowing reproduction rather than another conventional comma-list.
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

SERIALIZE_PAIR=()
mapfile -d '' -t SERIALIZE_PAIR < <(metadata_pair "$BEAD_B" "$BEAD_A" "serialize_with")
SERIALIZE_B="${SERIALIZE_PAIR[0]:-}"
SERIALIZE_A="${SERIALIZE_PAIR[1]:-}"
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

SERIALIZE_PAIR_AFTER=()
mapfile -d '' -t SERIALIZE_PAIR_AFTER < <(metadata_pair "$BEAD_B" "$BEAD_A" "serialize_with")
SERIALIZE_B_AFTER="${SERIALIZE_PAIR_AFTER[0]:-}"
SERIALIZE_A_AFTER="${SERIALIZE_PAIR_AFTER[1]:-}"
if printf '%s' "$SERIALIZE_B_AFTER" | grep -q "$BEAD_A" && printf '%s' "$SERIALIZE_A_AFTER" | grep -q "$BEAD_B"; then
  pass "real bd: metadata still agrees on both beads after the notes rewrite"
else
  fail "real bd: metadata still agrees on both beads after the notes rewrite" \
       "B.serialize_with='$SERIALIZE_B_AFTER' A.serialize_with='$SERIALIZE_A_AFTER'"
fi

METADATA_READS=0
METADATA_PAIR_READS=0
METADATA_READ_DETAIL=""
while IFS= read -r METADATA_READ; do
  METADATA_READS=$((METADATA_READS + 1))
  [ "$METADATA_READ" = "$BEAD_B $BEAD_A" ] && METADATA_PAIR_READS=$((METADATA_PAIR_READS + 1))
  METADATA_READ_DETAIL="${METADATA_READ_DETAIL}${METADATA_READ}|"
done < "$METADATA_READ_LOG"
if [ "$METADATA_READS" = "2" ] && [ "$METADATA_PAIR_READS" = "2" ]; then
  pass "real bd: two fresh pair snapshots reuse each store read for both beads (one before and one after the state transition)"
else
  fail "real bd: two fresh pair snapshots reuse each store read for both beads (one before and one after the state transition)" \
       "expected two '$BEAD_B $BEAD_A' reads; observed $METADATA_READS: $METADATA_READ_DETAIL"
fi

# ---------------------------------------------------------------------------
# PLANT-AND-FAIL (SABLE-5lli.7) — the overlap fixture's one batch import is
# also the real hermeticity plant. The pre-seed read above proved the shared
# store clean. A fresh read after that mutation must find BEAD_A in the same
# store (the established constant-location leak), while the pre-mutation copy
# must remain clean (the per-run isolation fix). No observation crosses the
# batch-import mutation: each side performs its own real bd read.
# ---------------------------------------------------------------------------
if [ "$PLANT_PRECONDITION_CLEAN" = "0" ]; then
  fail "PLANT-AND-FAIL precondition: a fresh shared-constant DB starts clean before the plant" \
       "unexpected pre-existing probe bead on the very first call"
else
  pass "PLANT-AND-FAIL precondition: a fresh shared-constant DB starts clean before the plant"
fi
if probe_present "$BEADS_ROOT" shared-after-seed; then
  pass "PLANT-AND-FAIL: re-pointing two runs at the SAME constant DB reproduces cross-run leakage — hermeticity check correctly goes RED"
else
  fail "PLANT-AND-FAIL: re-pointing two runs at the SAME constant DB reproduces cross-run leakage — hermeticity check correctly goes RED" \
       "second call on the shared DB did not observe the first call's probe bead — the plant did not arm"
fi
if probe_present "$FRESH_B" isolated-copy-after-seed; then
  fail "RESTORE GREEN: two runs on their OWN per-run-unique DBs do not leak into each other (the actual fix)" \
       "run B observed run A's probe bead despite separate scratch DBs"
else
  pass "RESTORE GREEN: two runs on their OWN per-run-unique DBs do not leak into each other (the actual fix)"
fi

expected_probe_reads="shared-before-seed shared-after-seed isolated-copy-after-seed"
actual_probe_reads="${PROBE_READ_STATES[*]}"
if [ "$FIXTURE_SEED_WRITES" = "1" ] && [ "$actual_probe_reads" = "$expected_probe_reads" ]; then
  pass "structure: one batched fixture seed write spans the overlap and hermeticity scenarios"
else
  fail "structure: one batched fixture seed write spans the overlap and hermeticity scenarios" \
       "fixture seed writes=$FIXTURE_SEED_WRITES expected=1; probe reads='$actual_probe_reads'"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
