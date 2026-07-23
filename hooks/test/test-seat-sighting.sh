#!/usr/bin/env bash
# test-seat-sighting.sh — the merge seat records a durable observation with
# no manager in the loop, against a REAL bd store (SABLE-441vl).
#
# WHAT IS UNDER TEST
# ------------------
# Before this bead, chuck (the merge seat) could not file work-creating beads
# and had NO lesser "record an observation" path either — any finding he made
# reached the backlog only if a manager happened to read his message and
# choose to file it. That worked on ATTENTION, not STRUCTURE: an unfiled
# finding left no artifact at all. `sable-msg --file-sighting` closes the gap
# without widening the seat's authority.
#
#   C1  filing a sighting (chuck identity) produces a bead that is DURABLE
#       (bd show finds it), DEFERRED (status=deferred), carries the
#       sighting,for-triage label, and is ABSENT from `bd ready`.
#   C2  POSITIVE CONTROL: once a manager promotes it (bd update
#       --status=open), it DOES appear in `bd ready` — the deferred status
#       above was a real exclusion, not a fixture artifact.
#   C3  the seat's boundary still holds for a NORMAL work `bd create` (no
#       sighting label) run through the enforcement hook directly — the
#       governance limit this bead must not widen.
#
# Run with:
#   bash hooks/test/test-seat-sighting.sh
#
# SELF-SKIPS (loudly) when bd is not on PATH — this suite's whole claim is
# about a real bd store's ready-pool visibility; it drives its own throwaway
# DB, never the real bead pool.

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
SABLE_MSG="$REPO_ROOT/bin/sable-msg"
SEAT_GATE="$REPO_ROOT/hooks/multi-manager/seat-sighting-gate.sh"

[ -f "$SABLE_MSG" ] || { echo "FATAL: missing $SABLE_MSG"; exit 2; }
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

# ---------------------------------------------------------------------------
# C1 — filing a sighting from the seat's own identity
# ---------------------------------------------------------------------------
SIGHT_OUT="$(env CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager \
    python3 "$SABLE_MSG" --file-sighting --from chuck \
    "found a defect while verifying a branch at the seat" 2>&1)"
SIGHT_RC=$?
BID="$(echo "$SIGHT_OUT" | grep -oE 'sighting [a-zA-Z0-9_-]+' | awk '{print $2}')"

if [ "$SIGHT_RC" -eq 0 ] && [ -n "$BID" ]; then
  pass "C1 --file-sighting exits 0 and reports a bead id ($BID)"
else
  fail "C1 --file-sighting exits 0 with a bead id" "rc=$SIGHT_RC
$SIGHT_OUT"
fi

if [ -n "$BID" ] && bd show "$BID" --json >/dev/null 2>&1; then
  pass "C1 the sighting bead is DURABLE — bd show finds it"
else
  fail "C1 the sighting bead is durable" "bd show $BID failed"
fi

STATUS="$(bd show "$BID" --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print((d[0] if isinstance(d, list) else d).get('status',''))" 2>/dev/null)"
if [ "$STATUS" = "deferred" ]; then
  pass "C1 the sighting bead is DEFERRED"
else
  fail "C1 the sighting bead is deferred" "status=$STATUS"
fi

LABELS="$(bd show "$BID" --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(','.join((d[0] if isinstance(d, list) else d).get('labels') or []))" 2>/dev/null)"
if echo ",$LABELS," | grep -qE ',sighting,'; then
  pass "C1 the sighting bead carries the sighting label"
else
  fail "C1 the sighting bead carries the sighting label" "labels=$LABELS"
fi

if bd ready 2>/dev/null | grep -qF "$BID"; then
  fail "C1 the sighting bead is absent from bd ready" "found in bd ready"
else
  pass "C1 the sighting bead is ABSENT from bd ready"
fi

# ---------------------------------------------------------------------------
# C2 — POSITIVE CONTROL: a manager promotes it, and it appears in bd ready
# ---------------------------------------------------------------------------
bd update "$BID" --status=open >/dev/null 2>&1
if bd ready 2>/dev/null | grep -qF "$BID"; then
  pass "C2 once promoted (status=open), the bead DOES appear in bd ready"
else
  fail "C2 promoted sighting appears in bd ready" "$(bd ready 2>&1)"
fi

# ---------------------------------------------------------------------------
# C3 — the seat's boundary still holds for a plain work bd create
# ---------------------------------------------------------------------------
GATE_OUT="$(printf '%s' '{"tool_input":{"command":"bd create --title=\"fix the thing\" --description=\"foo bar\" --type=task"}}' \
  | env CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager bash "$SEAT_GATE" 2>&1)"
if echo "$GATE_OUT" | grep -q '"permissionDecision": "deny"'; then
  pass "C3 the seat is still refused a normal WORK bd create"
else
  fail "C3 seat refused a normal work bd create" "$GATE_OUT"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
