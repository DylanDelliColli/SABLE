#!/usr/bin/env bash
# test-chuck-role-contract.sh — locks the D3 chuck-wake trigger into
# templates/multi-manager/roles/chuck.md (SABLE-jfg6.5 / S5-U2): the prose
# stranded-recovery sweep duty is retired (promoted to the tool) and every
# wake runs `sable-reconcile-handoffs` as a standing step instead. Victor's
# review note: the prose lives in TWO places (the ":31 sweep sentence" AND the
# ":88 Exception (stranded-recovery)" block) — both must be gone.
#
# Also locks the harness-ceiling doctrine (SABLE-jb5l8) into chuck.md and
# tarzan.md: a hand-run gate phase whose budget exceeds 600s must be
# launched with run_in_background and polled, never run in the Bash tool's
# foreground, because the tool's own 600s cap silently supersedes any longer
# derived budget.
#
# No bd/git dependency; uses only grep plus coreutils (mktemp, timeout,
# sleep, date) for the synthetic-ceiling fixture below — so it needs no
# clean-room guard.
#
# Run with: bash hooks/test/test-chuck-role-contract.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CHUCK="$REPO/templates/multi-manager/roles/chuck.md"
TARZAN="$REPO/templates/multi-manager/roles/tarzan.md"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
has_in() { local doc="$3"; if grep -qF -- "$2" "$doc" 2>/dev/null; then pass "$1"; else fail "$1" "$(basename "$doc") missing: $2"; fi; }
hasno_in() { local doc="$3"; if grep -qF -- "$2" "$doc" 2>/dev/null; then fail "$1" "$(basename "$doc") unexpectedly has: $2"; else pass "$1"; fi; }
has() { has_in "$1" "$2" "$CHUCK"; }
hasno() { hasno_in "$1" "$2" "$CHUCK"; }

if [ ! -f "$CHUCK" ]; then
  echo "FAIL: chuck.md not found at $CHUCK"
  exit 2
fi
if [ ! -f "$TARZAN" ]; then
  echo "FAIL: tarzan.md not found at $TARZAN"
  exit 2
fi

# --- the prose duty is gone from BOTH locations victor flagged ---
hasno "chuck.md drops the ':31 sweep sentence' phrase"          "stranded-recovery sweep"
hasno "chuck.md drops the ':88 Exception (stranded-recovery)' block" "Exception (stranded-recovery)"

# --- the tool invocation stands in its place ---
has "chuck.md names sable-reconcile-handoffs as a standing step" "sable-reconcile-handoffs"
has "chuck.md documents every-wake cadence for the reconcile step" "EVERY wake"

# --- boundaries section still forbids hand-filing, now attributing the
#     exception to the tool rather than to Chuck's own judgment call ---
has "chuck.md still forbids Chuck hand-filing for-chuck beads" "You do not file for-chuck beads yourself"

# --- harness-ceiling doctrine (SABLE-jb5l8): a hand-run gate phase whose
#     budget exceeds 600s must launch in the BACKGROUND and be polled, never
#     run in the Bash tool's foreground — the tool's own 600s cap silently
#     supersedes any longer derived budget. UNIT leg of the dispatch's
#     rewritten test spec. ---
has "chuck.md mandates background launch for promote / gate phases" "run_in_background"
has "chuck.md documents WALL-CLOCK DURATION reporting"              "WALL-CLOCK DURATION"
has "chuck.md names the derive-the-bound rule as insufficient alone" "CORRECT AND INSUFFICIENT"
has_in "tarzan.md mandates background launch for hand-run gate phases" "run_in_background" "$TARZAN"
has_in "tarzan.md documents WALL-CLOCK DURATION reporting"             "WALL-CLOCK DURATION" "$TARZAN"
has_in "tarzan.md names the derive-the-bound rule as insufficient alone" "CORRECT AND INSUFFICIENT" "$TARZAN"

# --- negative control (dispatch section 5): the background-launch checks
#     above must not pass vacuously. Prove it against two fixtures the same
#     grep pattern is run over: one with "background" in an unrelated
#     sentence, one that teaches only the derive-the-bound rule. Both must
#     FAIL the check — if either passed, the "has" assertions above would be
#     worthless (satisfied by any doc mentioning the word "background"). ---
NEG_VACUOUS="$(mktemp)"
NEG_DERIVE_ONLY="$(mktemp)"
trap 'rm -f "$NEG_VACUOUS" "$NEG_DERIVE_ONLY"' EXIT

cat > "$NEG_VACUOUS" <<'EOF'
Keep this running in the background of your mind while you triage.
Never hardcode the timeout; derive it with promote-budget --seconds.
EOF

cat > "$NEG_DERIVE_ONLY" <<'EOF'
If you wrap promote in a timeout, DERIVE the bound (SABLE-w0zjm).
Never hardcode it. sable-merge-gate promote-budget prints the breakdown.
EOF

check_neg() {
  local label="$1" fixture="$2" pattern="$3"
  if grep -qF -- "$pattern" "$fixture" 2>/dev/null; then
    fail "$label" "negative-control fixture unexpectedly matched '$pattern' — the real check would pass vacuously"
  else
    pass "$label"
  fi
}
check_neg "negative control: an unrelated 'background' mention does not satisfy the run_in_background check" "$NEG_VACUOUS" "run_in_background"
check_neg "negative control: a derive-the-bound-only doc does not satisfy the run_in_background check" "$NEG_DERIVE_ONLY" "run_in_background"

# --- SYNTHETIC-CEILING (dispatch section 5, replaces the forbidden 600s+
#     integration leg): is a harness-kill DISTINGUISHABLE from a genuine
#     gate failure? Use a wrapper ceiling (N=2s) smaller than an inner
#     budget (M=10s) it wraps — the same shape as the real 600s Bash-tool
#     cap vs. a >600s derived promote budget, scaled to run in seconds with
#     zero manufactured load. The fixture must be able to produce BOTH
#     outcomes, or it proves nothing about distinguishability. ---
CEILING=2

START_A=$(date +%s)
timeout "$CEILING" bash -c 'sleep 5; exit 0' >/dev/null 2>&1
RC_A=$?
END_A=$(date +%s)
ELAPSED_A=$((END_A - START_A))

START_B=$(date +%s)
timeout "$CEILING" bash -c 'exit 20' >/dev/null 2>&1
RC_B=$?
END_B=$(date +%s)
ELAPSED_B=$((END_B - START_B))

if [ "$RC_A" -eq 124 ]; then
  pass "synthetic-ceiling: a run that outlives the ceiling is harness-killed (rc=124), never reaching its own outcome"
else
  fail "synthetic-ceiling: expected rc=124 for a ceiling-outlived run, got rc=$RC_A"
fi

if [ "$ELAPSED_A" -lt 5 ]; then
  pass "synthetic-ceiling: the kill fires at the ceiling (${ELAPSED_A}s), before the child's own 5s completion — the reported duration is the harness's, not the gate's"
else
  fail "synthetic-ceiling: kill did not fire before the child's own completion (elapsed=${ELAPSED_A}s)"
fi

if [ "$RC_B" -eq 20 ]; then
  pass "synthetic-ceiling: a genuine gate failure well inside the ceiling (${ELAPSED_B}s) preserves its own exit code (20), not 124"
else
  fail "synthetic-ceiling: expected rc=20 (the gate's own code) for a fast failure, got rc=$RC_B"
fi

if [ "$RC_A" != "$RC_B" ]; then
  pass "synthetic-ceiling: harness-kill (124) and genuine gate failure (20) are distinguishable outcomes from the same wrapper shape"
else
  fail "synthetic-ceiling: harness-kill and gate failure produced the SAME exit code — not distinguishable"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
