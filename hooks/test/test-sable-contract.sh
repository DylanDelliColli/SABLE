#!/usr/bin/env bash
# test-sable-contract.sh — unit tests for the active-contracts writer (SABLE-9ozz).
#
# sable-contract is the WRITE side of the active-protocol surface: flips and
# managers append/replace live contracts here, and session-role-anchor.sh SURFACES
# them at SessionStart so a restarted pane reconciles against the current protocol
# instead of its historical static identity. The surface is colocated with the
# mode-state file in <repo>/.claude/sable/state/ so both live/protocol surfaces
# travel together.
#
# Run with:
#   bash hooks/test/test-sable-contract.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$REPO/bin/sable-contract"

# Hermeticity: never let an ambient override leak in (SABLE-j3bi).
unset SABLE_ACTIVE_CONTRACTS SABLE_MODE_STATE 2>/dev/null || true

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$TOOL" ] || { fail "bin/sable-contract exists and is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "bin/sable-contract exists and is executable"

# ---------- SABLE_ACTIVE_CONTRACTS override: path/add/show/set/clear ----------
TMP="$(mktemp -d)"
CFILE="$TMP/active-contracts.md"

out="$(SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" path)"
if [ "$out" = "$CFILE" ]; then pass "path honors SABLE_ACTIVE_CONTRACTS"; else fail "path honors SABLE_ACTIVE_CONTRACTS" "got: $out"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" add "sable-merge-gate is the SOLE merge path" >/dev/null 2>&1
if grep -q 'sable-merge-gate is the SOLE merge path' "$CFILE"; then pass "add writes a contract"; else fail "add writes a contract"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" add "interim worker cap is 2 per manager" >/dev/null 2>&1
n="$(grep -c '^- ' "$CFILE" 2>/dev/null || echo 0)"
if [ "$n" = "2" ]; then pass "add appends (does not overwrite)"; else fail "add appends (does not overwrite)" "count=$n"; fi

out="$(SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" show 2>/dev/null)"
if printf '%s' "$out" | grep -q 'interim worker cap is 2'; then pass "show prints contracts"; else fail "show prints contracts" "got: ${out:0:120}"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" set "single replacing contract" >/dev/null 2>&1
n="$(grep -c '^- ' "$CFILE" 2>/dev/null || echo 0)"
if [ "$n" = "1" ] && grep -q 'single replacing contract' "$CFILE"; then pass "set replaces the surface"; else fail "set replaces the surface" "count=$n"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" clear >/dev/null 2>&1
if [ ! -f "$CFILE" ]; then pass "clear removes the surface"; else fail "clear removes the surface"; fi

# ---------- error paths ----------
SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" show >/dev/null 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "show on empty surface exits nonzero"; else fail "show on empty surface exits nonzero" "rc=$rc"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" add >/dev/null 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "add without text exits nonzero"; else fail "add without text exits nonzero" "rc=$rc"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" set >/dev/null 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "set without text exits nonzero"; else fail "set without text exits nonzero" "rc=$rc"; fi

# ---------- SABLE_MODE_STATE dirname colocation ----------
out="$(SABLE_MODE_STATE="$TMP/sub/mode-state.json" bash "$TOOL" path)"
if [ "$out" = "$TMP/sub/active-contracts.md" ]; then pass "path colocates via SABLE_MODE_STATE dirname"; else fail "path colocates via SABLE_MODE_STATE dirname" "got: $out"; fi

# ---------- git-repo resolution mirrors mode-state's state dir ----------
GITREPO="$(mktemp -d)"
git -C "$GITREPO" init -q >/dev/null 2>&1
cpath="$(cd "$GITREPO" && bash "$TOOL" path)"
expected="$GITREPO/.claude/sable/state/active-contracts.md"
if [ "$cpath" = "$expected" ]; then pass "git-repo path resolves under .claude/sable/state"; else fail "git-repo path resolves under .claude/sable/state" "got: $cpath want: $expected"; fi
mpath="$(cd "$GITREPO" && bash "$REPO/bin/sable-mode" path)"
if [ "$(dirname "$cpath")" = "$(dirname "$mpath")" ]; then pass "contracts colocated with mode-state dir (no drift)"; else fail "contracts colocated with mode-state dir (no drift)" "c=$cpath m=$mpath"; fi

rm -rf "$TMP" "$GITREPO"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
