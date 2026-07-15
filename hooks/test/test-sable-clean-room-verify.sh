#!/usr/bin/env bash
# test-sable-clean-room-verify.sh — unit tests for the PATH-scrubbing helper
# (SABLE-59zu, narrowed scope v2).
#
# sable-clean-room-verify removes any PATH entry that provides `bd` or
# `dolt` and runs a given command under that scrubbed PATH, so a suite that
# only passes locally because bd/dolt are ambient can be caught BEFORE push
# instead of only in the ci-verify clean-room runner. These tests fabricate
# throwaway `bd`/`dolt` stub executables on a controlled PATH and assert the
# tool actually removes their directories (not merely shadows them).
#
# Run with:
#   bash hooks/test/test-sable-clean-room-verify.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$REPO/bin/sable-clean-room-verify"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$TOOL" ] || { fail "bin/sable-clean-room-verify exists and is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "bin/sable-clean-room-verify exists and is executable"

# A throwaway PATH dir with stub `bd` and `dolt` executables, standing in for
# an ambient dev-box install of both.
FAKE_BIN="$(mktemp -d)"
printf '#!/bin/sh\necho fake-bd\n' > "$FAKE_BIN/bd"
printf '#!/bin/sh\necho fake-dolt\n' > "$FAKE_BIN/dolt"
chmod +x "$FAKE_BIN/bd" "$FAKE_BIN/dolt"

# A second, unrelated PATH dir with a harmless tool, to prove scrubbing is
# SELECTIVE (only bd/dolt-providing dirs are dropped, not the whole PATH).
OTHER_BIN="$(mktemp -d)"
printf '#!/bin/sh\necho hello-from-other-bin\n' > "$OTHER_BIN/harmless-tool"
chmod +x "$OTHER_BIN/harmless-tool"

TEST_PATH="$FAKE_BIN:$OTHER_BIN:/usr/bin:/bin"

# ---------- bd/dolt are unreachable under the scrubbed PATH ----------
out="$(PATH="$TEST_PATH" bash "$TOOL" bash -c 'command -v bd || echo NOTFOUND-bd' 2>&1)"
if printf '%s' "$out" | grep -q "NOTFOUND-bd"; then
    pass "bd is unreachable under the scrubbed PATH"
else
    fail "bd is unreachable under the scrubbed PATH" "$out"
fi

out="$(PATH="$TEST_PATH" bash "$TOOL" bash -c 'command -v dolt || echo NOTFOUND-dolt' 2>&1)"
if printf '%s' "$out" | grep -q "NOTFOUND-dolt"; then
    pass "dolt is unreachable under the scrubbed PATH"
else
    fail "dolt is unreachable under the scrubbed PATH" "$out"
fi

# ---------- an unrelated tool on a DIFFERENT PATH dir survives scrubbing ----------
out="$(PATH="$TEST_PATH" bash "$TOOL" bash -c 'command -v harmless-tool >/dev/null && harmless-tool' 2>&1)"
if printf '%s' "$out" | grep -q "hello-from-other-bin"; then
    pass "an unrelated tool on a different PATH dir still resolves (selective scrub)"
else
    fail "an unrelated tool on a different PATH dir still resolves (selective scrub)" "$out"
fi

# ---------- removal, not front-shadowing: bd/dolt's dir is gone from PATH entirely ----------
out="$(PATH="$TEST_PATH" bash "$TOOL" bash -c 'printf "%s" "$PATH"' 2>&1)"
if ! printf '%s' "$out" | grep -q "$FAKE_BIN"; then
    pass "the bd/dolt-providing directory is removed from PATH, not shadowed"
else
    fail "the bd/dolt-providing directory is removed from PATH, not shadowed" "$out"
fi

# ---------- the child command's exit status propagates ----------
PATH="$TEST_PATH" bash "$TOOL" bash -c 'exit 7'
rc=$?
if [ "$rc" -eq 7 ]; then
    pass "child command's exit status propagates"
else
    fail "child command's exit status propagates" "rc=$rc"
fi

# ---------- --help exits 0 and documents usage ----------
out="$(bash "$TOOL" --help 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "shell-run-set.sh --run"; then
    pass "--help exits 0 and documents the default gate command"
else
    fail "--help exits 0 and documents the default gate command" "rc=$rc out=$out"
fi

# ---------- no bd/dolt on PATH to begin with: still runs, notes no signal added ----------
CLEAN_PATH="/usr/bin:/bin"
out="$(PATH="$CLEAN_PATH" bash "$TOOL" bash -c 'echo ran-ok' 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q "ran-ok" && printf '%s' "$out" | grep -qi "no clean-room signal"; then
    pass "with no bd/dolt on PATH, still runs the command and says so"
else
    fail "with no bd/dolt on PATH, still runs the command and says so" "rc=$rc out=$out"
fi

rm -rf "$FAKE_BIN" "$OTHER_BIN"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
