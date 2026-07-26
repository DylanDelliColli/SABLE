#!/usr/bin/env bash
# test-seat-sighting.sh — capture is mandatory, priority is advisory, against
# a REAL bd store (SABLE-441vl).
#
# WHAT IS UNDER TEST
# ------------------
# The bead this covers was originally scoped on the premise that the merge
# seat (chuck) CANNOT create work-creating beads at all. That premise is
# measured FALSE (chuck filed seven beads the same day the premise was
# written) and a cockpit ruling — recorded as a COMMENT on SABLE-441vl, not
# in its description — re-scoped the deliverable: CAPTURE IS MANDATORY (the
# seat's `bd create` must never be refused), PRIORITY IS ADVISORY (a
# creation-time hook auto-labels a seat-filed bead and marks its priority
# provisional, for a manager's later triage).
#
#   C1  a plain `bd create` from the seat's identity is never denied and
#       lands LIVE in `bd ready` immediately — no promotion step, because
#       capture must never be blocked.
#   C2  hooks/multi-manager/seat-sighting-gate.sh (a PostToolUse hook that
#       never denies) auto-labels the just-created bead `seat-filed` and
#       marks `metadata.priority_provisional=true` afterward.
#   C3  a `bd create` from a NON-seat identity is left completely untouched —
#       no label, no metadata — proving the hook discriminates on identity.
#
# Run with:
#   bash hooks/test/test-seat-sighting.sh
#
# SELF-SKIPS (loudly) when bd is not on PATH — this suite's whole claim is
# about a real bd store's label/metadata/ready-pool state; it drives its own
# throwaway DB, never the real bead pool.

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
SEAT_GATE="$REPO_ROOT/hooks/multi-manager/seat-sighting-gate.sh"

[ -f "$SEAT_GATE" ] || { echo "FATAL: missing $SEAT_GATE"; exit 2; }

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP: bd not on PATH — this suite's whole point is a real bd store"
  exit 0
fi

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

BEADS_ROOT="$TMPROOT/beads"
mkdir -p "$BEADS_ROOT"
INIT_OUT="$(cd "$BEADS_ROOT" && env BD_NON_INTERACTIVE=1 bd init --prefix=seat 2>&1)"
if [ ! -d "$BEADS_ROOT/.beads" ]; then
  echo "FATAL: could not initialize an isolated bd DB: $INIT_OUT"
  exit 2
fi
export BEADS_DB="$BEADS_ROOT/.beads"

run_gate() {
  # $1 = command string, $2 = bd create's real stdout, $3 = agent name
  python3 -c "
import json
print(json.dumps({
    'tool_input': {'command': '''$1'''},
    'tool_response': {'stdout': '''$2''', 'stderr': ''},
}))
" | env CLAUDE_AGENT_NAME="$3" CLAUDE_AGENT_ROLE=manager BEADS_DB="$BEADS_DB" bash "$SEAT_GATE"
}

# ---------------------------------------------------------------------------
# C1/C2 — a plain bd create from the seat is never refused, lands live in
# bd ready, then gets auto-labeled/annotated afterward.
# ---------------------------------------------------------------------------
CREATE_CMD='bd create --title="found a defect while verifying a branch" --description="text [no-test]" --type=task --priority=2'
CREATE_OUT="$(bd create --title="found a defect while verifying a branch" \
  --description="text [no-test]" --type=task --priority=2 2>&1)"
CREATE_RC=$?
BID="$(echo "$CREATE_OUT" | grep -oE 'Created issue:\s*[a-zA-Z0-9_-]+' | awk '{print $NF}')"

if [ "$CREATE_RC" -eq 0 ] && [ -n "$BID" ]; then
  pass "C1 a plain bd create from the seat succeeds (bead $BID) — capture is never refused"
else
  fail "C1 a plain bd create from the seat succeeds" "rc=$CREATE_RC
$CREATE_OUT"
fi

if bd ready 2>/dev/null | grep -qF "$BID"; then
  pass "C1 the seat-filed bead is LIVE in bd ready immediately — no promotion step"
else
  fail "C1 the seat-filed bead is live in bd ready immediately" "$(bd ready 2>&1)"
fi

run_gate "$CREATE_CMD" "$CREATE_OUT" chuck >/dev/null 2>&1
GATE_RC=$?
if [ "$GATE_RC" -eq 0 ]; then
  pass "C2 seat-sighting-gate.sh exits 0 (never denies)"
else
  fail "C2 seat-sighting-gate.sh exits 0" "rc=$GATE_RC"
fi

LABELS="$(bd show "$BID" --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d; print(','.join(d.get('labels') or []))" 2>/dev/null)"
if echo ",$LABELS," | grep -qE ',seat-filed,'; then
  pass "C2 the bead is auto-labeled seat-filed"
else
  fail "C2 the bead is auto-labeled seat-filed" "labels=$LABELS"
fi

PROVISIONAL="$(bd show "$BID" --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d; print((d.get('metadata') or {}).get('priority_provisional'))" 2>/dev/null)"
if [ "$PROVISIONAL" = "True" ] || [ "$PROVISIONAL" = "true" ]; then
  pass "C2 metadata.priority_provisional=true is set"
else
  fail "C2 metadata.priority_provisional=true is set" "got: $PROVISIONAL"
fi

if [ "$(bd show "$BID" --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d; print(d.get('priority'))")" = "2" ]; then
  pass "C2 the seat's own priority estimate (P2) is preserved, not overridden"
else
  fail "C2 the seat's priority estimate is preserved" "$(bd show "$BID" --json 2>&1)"
fi

# ---------------------------------------------------------------------------
# C3 — a non-seat identity's bd create is left completely untouched
# ---------------------------------------------------------------------------
CREATE2_OUT="$(bd create --title="ordinary work bead" --description="text [no-test]" --type=task 2>&1)"
BID2="$(echo "$CREATE2_OUT" | grep -oE 'Created issue:\s*[a-zA-Z0-9_-]+' | awk '{print $NF}')"
run_gate 'bd create --title="ordinary work bead" --description="text [no-test]" --type=task' \
  "$CREATE2_OUT" optimus >/dev/null 2>&1

LABELS2="$(bd show "$BID2" --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d; print(','.join(d.get('labels') or []))" 2>/dev/null)"
if [ -z "$LABELS2" ]; then
  pass "C3 a non-seat identity's bead is left untouched — no seat-filed label"
else
  fail "C3 a non-seat identity's bead is left untouched" "labels=$LABELS2"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
