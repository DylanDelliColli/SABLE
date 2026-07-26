#!/usr/bin/env bash
# test-tarzan-optimus-accept-contract.sh — locks the "## Accept protocol"
# section (SABLE-5lli.6) into BOTH templates/multi-manager/roles/tarzan.md
# and optimus.md: S1's shared-code-path guard invariant, S3's premise-as-claim
# rule, S4's base-rate-derived sample-size bar, and S5's run-it-where-it-can-
# fail environment rule, in identical canonical phrasing across both cards.
# Mirrors hooks/test/test-chuck-role-contract.sh.
#
# Pure grep — no bd/git/subprocess — so it needs no clean-room guard.
#
# Run with: bash hooks/test/test-tarzan-optimus-accept-contract.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TARZAN="$REPO/templates/multi-manager/roles/tarzan.md"
OPTIMUS="$REPO/templates/multi-manager/roles/optimus.md"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

has_in() {
  local file="$1" label="$2" phrase="$3"
  if grep -qF -- "$phrase" "$file" 2>/dev/null; then
    pass "$label"
  else
    fail "$label" "$(basename "$file") missing: $phrase"
  fi
}

has_both() {
  local name="$1" phrase="$2"
  has_in "$TARZAN" "tarzan.md: $name" "$phrase"
  has_in "$OPTIMUS" "optimus.md: $name" "$phrase"
}

if [ ! -f "$TARZAN" ]; then echo "FAIL: tarzan.md not found at $TARZAN"; exit 2; fi
if [ ! -f "$OPTIMUS" ]; then echo "FAIL: optimus.md not found at $OPTIMUS"; exit 2; fi

# --- section heading present in both ---
has_both "carries the '## Accept protocol' heading" "## Accept protocol"

# --- S1: shared-code-path guard invariant ---
has_both "S1 guard must share the assertion's code path" \
  "a guard must invoke the SAME code path as the assertion it guards"
has_both "S1 neutering the shared path must turn it red" \
  "neutering that shared path must turn it red"

# --- S3: premise-as-claim rule ---
has_both "S3 premise is a claim to verify" \
  "Treat the bead's premise as a CLAIM to verify"
has_both "S3 not an instruction to execute" \
  "not an instruction to execute"

# --- S4: base-rate-derived sample size ---
has_both "S4 n>=3/p for 95% confidence" \
  "n >= 3/p for 95% confidence, never pick a round number"
has_both "S4 prefer a deterministic construction" \
  "Prefer a deterministic construction over a statistical one"
has_both "S4 state the residual verbatim" \
  "state the residual verbatim in the close reason"

# --- S5: run-it-where-it-can-fail environment rule ---
has_both "S5 run it where it can fail" "Run it where it can fail"
has_both "S5 clean-room-or-state-residual" "clean-room-or-state-residual"
has_both "S5 env -i is not a clean-room" "env -i is NOT a clean-room"
has_both "S5 stub not skip" "STUB the absent dependency, do not SKIP"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
