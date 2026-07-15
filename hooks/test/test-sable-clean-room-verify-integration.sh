#!/usr/bin/env bash
# test-sable-clean-room-verify-integration.sh — proves a bd/dolt-less PATH
# makes install.sh-touching suites behave correctly: skip-or-stub, not a
# false-pass (SABLE-59zu, narrowed scope v2).
#
# INTEGRATION (real composition, no mocks): drives the REAL install.sh and
# the REAL hooks/test/test-sable-bin-install.sh suite — the repo's actual
# install.sh-touching suite — through the REAL bin/sable-clean-room-verify,
# with real bd/dolt (if any are ambient on this machine) genuinely removed
# from PATH, not shadowed. Two real behaviors, both "not false-pass":
#
#   1. Bare `install.sh` (no clean-room handling of its own) FAILS LOUD at
#      Step 1/8 with an honest "bd is not on PATH" — it does not silently
#      report success while having verified nothing.
#   2. hooks/test/test-sable-bin-install.sh (which DOES handle bd absence,
#      via its own S2_STUB) runs its full real suite — writes real files,
#      makes real assertions — and reports a genuine, non-vacuous green,
#      not a skip-and-exit-0 masquerading as coverage.
#
# Run with:
#   bash hooks/test/test-sable-clean-room-verify-integration.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$REPO/bin/sable-clean-room-verify"
INSTALL_SH="$REPO/install.sh"
BIN_INSTALL_SUITE="$REPO/hooks/test/test-sable-bin-install.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$TOOL" ] || { fail "bin/sable-clean-room-verify exists and is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "bin/sable-clean-room-verify exists and is executable"
[ -f "$INSTALL_SH" ] || { fail "install.sh exists"; echo "Tests: 2 | Passed: 1 | Failed: 1"; exit 1; }
[ -f "$BIN_INSTALL_SUITE" ] || { fail "hooks/test/test-sable-bin-install.sh exists"; echo "Tests: 2 | Passed: 1 | Failed: 1"; exit 1; }

# ---------- precondition: the scrubbed PATH genuinely lacks bd/dolt ----------
out="$(bash "$TOOL" bash -c 'command -v bd >/dev/null 2>&1 && echo STILL-HAS-BD || echo bd-gone; command -v dolt >/dev/null 2>&1 && echo STILL-HAS-DOLT || echo dolt-gone' 2>&1)"
if printf '%s' "$out" | grep -q "bd-gone" && printf '%s' "$out" | grep -q "dolt-gone"; then
    pass "precondition: bd and dolt are genuinely unreachable under the scrubbed PATH"
else
    fail "precondition: bd and dolt are genuinely unreachable under the scrubbed PATH" "$out"
fi

# ---------- 1. bare install.sh: no clean-room handling -> fails LOUD, not a false-pass ----------
# --from-here bypasses the (unrelated) linked-worktree refusal so Step 1/8's
# bd check is what's actually exercised. HOME is a throwaway dir as a
# belt-and-suspenders guard: install.sh does no filesystem writes before its
# bd check (verified by reading the script), but this keeps the assertion
# true even if that ordering ever changes.
TMPHOME="$(mktemp -d)"
out="$(HOME="$TMPHOME" bash "$TOOL" bash "$INSTALL_SH" --from-here 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qi "bd (beads) is not on PATH"; then
    pass "bare install.sh under a bd-less PATH fails LOUD with an honest message (not a false-pass)"
else
    fail "bare install.sh under a bd-less PATH fails LOUD with an honest message (not a false-pass)" "rc=$rc out=${out:0:400}"
fi
if [ ! -e "$TMPHOME/.claude" ]; then
    pass "the failed run wrote nothing to HOME/.claude"
else
    fail "the failed run wrote nothing to HOME/.claude"
fi
rm -rf "$TMPHOME"

# ---------- 2. the real install.sh-touching suite: genuine stub-and-pass, not vacuous ----------
out="$(bash "$TOOL" bash "$BIN_INSTALL_SUITE" 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qE "Tests: [0-9]+ \| Passed: [0-9]+ \| Failed: 0"; then
    pass "test-sable-bin-install.sh (clean-room-aware) runs its full real suite green under a bd/dolt-less PATH"
else
    fail "test-sable-bin-install.sh (clean-room-aware) runs its full real suite green under a bd/dolt-less PATH" "rc=$rc tail=$(printf '%s' "$out" | tail -5)"
fi
# Not vacuous: a suite that silently skipped everything would report far
# fewer than its normal handful of assertions. Guard against a future
# regression where the suite starts short-circuiting on bd absence instead
# of exercising its stub path.
ran_count="$(printf '%s' "$out" | grep -oE 'Tests: [0-9]+' | grep -oE '[0-9]+' || echo 0)"
if [ "${ran_count:-0}" -ge 10 ]; then
    pass "the suite actually ran a real number of assertions (not a 1-line vacuous skip): $ran_count"
else
    fail "the suite actually ran a real number of assertions (not a 1-line vacuous skip)" "ran_count=$ran_count"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
