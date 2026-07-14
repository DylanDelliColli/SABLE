#!/usr/bin/env bash
# test-install-guard.sh — SABLE-s6qk: install.sh refuses to run from a linked
# git worktree (the worktree/clone hot-swap guard). Verifies:
#   1. a linked worktree refuses by default and is side-effect-free
#   2. --from-here overrides and proceeds with a normal install
#   3. the main (non-worktree) checkout proceeds as before, no flag needed
#
# Fixture safety: the "main checkout" fixture is a LOCAL CLONE of this repo
# (clone is read-only on its source — never a mutating op on the real repo),
# and the worktree fixture is added off THAT CLONE, never off the real repo.
# The suite never runs a git op against the real repo/worktree it lives in.
#
# Run with:
#   bash hooks/test/test-install-guard.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# Canonicalize a directory the same way the guard does, so expected paths
# match regardless of /tmp symlink quirks.
canon() { ( cd "$1" 2>/dev/null && pwd ); }

# ---------- fixtures ----------
CLONE_PARENT="$(mktemp -d)"
MAIN="$CLONE_PARENT/main-checkout"
if ! git clone --quiet --depth 1 "$REPO_ROOT" "$MAIN" >/tmp/tig-clone.log 2>&1; then
    echo "FATAL: could not clone $REPO_ROOT for fixture"
    cat /tmp/tig-clone.log
    exit 2
fi
MAIN_CANON="$(canon "$MAIN")"
INSTALL="$MAIN/install.sh"

WT="$(mktemp -u)"
if ! git -C "$MAIN" worktree add -q "$WT" -b sable-s6qk-guard-test >/tmp/tig-wt.log 2>&1; then
    echo "FATAL: could not add worktree fixture off the clone"
    cat /tmp/tig-wt.log
    rm -rf "$CLONE_PARENT"
    exit 2
fi
WT_INSTALL="$WT/install.sh"

# ---------- 1. linked worktree refuses by default, side-effect-free ----------
FAKE1="$(mktemp -d)"
OUT1="$(HOME="$FAKE1" bash "$WT_INSTALL" 2>&1)"; RC1=$?
[ "$RC1" -ne 0 ] && pass "worktree install.sh exits nonzero by default" || fail "worktree install.sh exits nonzero by default" "rc=$RC1 out=$OUT1"
printf '%s' "$OUT1" | grep -qi "refusing to run from a linked git worktree" \
    && pass "refusal message names the guard reason" \
    || fail "refusal message names the guard reason" "$OUT1"
printf '%s' "$OUT1" | grep -qF "$MAIN_CANON" \
    && pass "refusal message names the canonical checkout" \
    || fail "refusal message names the canonical checkout" "$OUT1"
[ -z "$(ls -A "$FAKE1" 2>/dev/null)" ] \
    && pass "worktree refusal is side-effect-free (fake HOME untouched)" \
    || fail "worktree refusal is side-effect-free" "created under FAKE1: $(ls -A "$FAKE1")"

# ---------- 2. --from-here overrides and proceeds ----------
FAKE2="$(mktemp -d)"
OUT2="$(HOME="$FAKE2" bash "$WT_INSTALL" --from-here 2>&1)"; RC2=$?
[ "$RC2" -eq 0 ] && pass "--from-here proceeds (exit 0)" || fail "--from-here proceeds" "rc=$RC2 out=$OUT2"
[ -e "$FAKE2/.claude/hooks/multi-manager/mode-interlock.sh" ] \
    && pass "--from-here installs hooks from the worktree" \
    || fail "--from-here installs hooks from the worktree" "missing under $FAKE2"

# ---------- 3. main (non-worktree) checkout proceeds as before ----------
FAKE3="$(mktemp -d)"
OUT3="$(HOME="$FAKE3" bash "$INSTALL" 2>&1)"; RC3=$?
[ "$RC3" -eq 0 ] && pass "main checkout installs without --from-here (exit 0)" || fail "main checkout installs without --from-here" "rc=$RC3 out=$OUT3"
[ -e "$FAKE3/.claude/hooks/multi-manager/mode-interlock.sh" ] \
    && pass "main checkout installs hooks" \
    || fail "main checkout installs hooks" "missing under $FAKE3"
printf '%s' "$OUT3" | grep -qi "refusing to run from a linked git worktree" \
    && fail "main checkout prints no worktree refusal" "$OUT3" \
    || pass "main checkout prints no worktree refusal"

# ---------- cleanup ----------
git -C "$MAIN" worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"
rm -rf "$CLONE_PARENT" "$FAKE1" "$FAKE2" "$FAKE3"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then
    echo -e "Failed tests:$FAIL_NAMES"
    exit 1
fi
exit 0
