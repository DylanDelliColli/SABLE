#!/usr/bin/env bash
# test-lib-git-sandbox.sh — acceptance test for the SABLE-0ssz.2 harness
# preamble (hooks/test/lib-git-sandbox.sh).
#
# ACCEPTANCE (a): with the preamble sourced, a deliberately-malicious fixture
# (a `git push origin main`, a bare `git config user.name X`) provably cannot
# affect the real repo or shared config.
#
# We do NOT experiment on the real SABLE repo. Instead we stand up a "victim"
# repo + victim remote + a sentinel global config that PLAY the role of the real
# repo / shared state, reproduce the EXACT incident mechanism (an unguarded `cd`
# that fails, leaving later bare git ops to run wherever CWD landed), and assert
# the victim is untouched WITH the preamble — after first proving, via a negative
# control, that the very same escape DOES pollute the victim WITHOUT it.
#
# Run with:
#   bash hooks/test/test-lib-git-sandbox.sh

set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/lib-git-sandbox.sh"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() {
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  $1"
  echo "FAIL: $1"
  [ -n "${2:-}" ] && echo "  detail: $2"
}
assert_eq() {
  # assert_eq <name> <expected> <actual>
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "expected [$2], got [$3]"; fi
}
assert_ne() {
  if [ "$2" != "$3" ]; then pass "$1"; else fail "$1" "expected NOT [$2], got [$3]"; fi
}

if [ ! -f "$LIB" ]; then
  echo "FAIL: preamble not found at $LIB"
  exit 2
fi

# --------------------------------------------------------------------------
# Stand up the "victim" that plays the role of the real repo / shared state.
# --------------------------------------------------------------------------
VROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-victim.XXXXXX")"
trap 'rm -rf "$VROOT"' EXIT

VREMOTE="$VROOT/remote.git"
git init -q --bare "$VREMOTE"
VREPO="$VROOT/victim"
git init -q "$VREPO"
git -C "$VREPO" remote add origin "$VREMOTE"
git -C "$VREPO" config user.email "victim@real.invalid"
git -C "$VREPO" config user.name  "VICTIM_REAL"
( cd "$VREPO" \
    && echo base > file.txt \
    && git add file.txt \
    && git commit -q -m "base" \
    && git push -q origin HEAD:refs/heads/main )

# A sentinel that plays the user's REAL global git config.
SENTINEL_GLOBAL="$VROOT/sentinel-gitconfig"
git config --file "$SENTINEL_GLOBAL" user.name "SENTINEL_GLOBAL_NAME"

VREMOTE_MAIN_BEFORE="$(git --git-dir="$VREMOTE" rev-parse refs/heads/main)"

# --------------------------------------------------------------------------
# NEGATIVE CONTROL — the escape is REAL: without the preamble, an unguarded cd
# that fails leaves a bare `git config` writing the victim's local config. (We
# scope this strictly to the victim; the real repo is never CWD here.)
# --------------------------------------------------------------------------
(
  cd "$VREPO" || exit 1
  cd "$VROOT/nope-does-not-exist" 2>/dev/null   # tripwire-allow: intentional unguarded-cd reproduction of the escape
  git config user.name "EVIL_NEGATIVE_CONTROL"  # lands in victim local (the a5a5 mechanism)
)
if [ "$(git -C "$VREPO" config --local user.name)" = "EVIL_NEGATIVE_CONTROL" ]; then
  pass "negative control: unguarded-cd escape DOES pollute victim without the preamble (test is not vacuous)"
else
  fail "negative control: escape did not reproduce — the acceptance test would be vacuous" \
       "victim user.name = $(git -C "$VREPO" config --local user.name)"
fi
# restore the victim before the real test
git -C "$VREPO" config user.name "VICTIM_REAL"

# --------------------------------------------------------------------------
# THE ACCEPTANCE TEST — same escape, WITH the preamble sourced.
# --------------------------------------------------------------------------
(
  cd "$VREPO" || exit 1
  export GIT_CONFIG_GLOBAL="$SENTINEL_GLOBAL"   # the user's "real" global, in force
  export GIT_CONFIG_SYSTEM=/dev/null

  # shellcheck disable=SC1090
  source "$LIB"                                  # anchors CWD to a sandbox, redirects config, rewrites origin

  # env-neutralization assertions
  [ "$GIT_CONFIG_GLOBAL" != "$SENTINEL_GLOBAL" ] && echo "REDIRECT_OK"
  [ -n "${SABLE_TEST_SANDBOX:-}" ] && [ -d "$SABLE_TEST_SANDBOX" ] && echo "SANDBOX_OK"
  [ -z "${GIT_DIR:-}" ] && echo "GITDIR_UNSET_OK"

  # the deliberately-malicious fixture with an UNGUARDED cd (the bug):
  cd "$VROOT/nope-does-not-exist" 2>/dev/null    # tripwire-allow: intentional unguarded-cd reproduction of the escape
  git config user.name "EVIL_ESCAPE"             # a5a5 vector
  git commit --allow-empty -q -m "evil" 2>/dev/null
  git push origin HEAD:refs/heads/main 2>/dev/null   # xydb vector

  # Positive containment — assert INSIDE the child (before the cleanup trap
  # removes the sandbox on exit) that the evil ops RAN but landed in the sandbox.
  [ "$(git -C "$SABLE_TEST_SANDBOX/repo" config --local user.name 2>/dev/null)" = "EVIL_ESCAPE" ] && echo "CONTAINED_CONFIG_OK"
  git --git-dir="$SABLE_TEST_SANDBOX/origin.git" rev-parse refs/heads/main >/dev/null 2>&1 && echo "CONTAINED_PUSH_OK"
  echo "SANDBOX=${SABLE_TEST_SANDBOX}"
) > "$VROOT/child.out" 2>/dev/null

grep -q REDIRECT_OK      "$VROOT/child.out" && pass "preamble redirects GIT_CONFIG_GLOBAL away from the real global config" || fail "preamble did not redirect GIT_CONFIG_GLOBAL"
grep -q SANDBOX_OK       "$VROOT/child.out" && pass "preamble creates a sandbox and anchors CWD to it" || fail "preamble did not create/anchor a sandbox"
grep -q GITDIR_UNSET_OK  "$VROOT/child.out" && pass "preamble unsets GIT_DIR" || fail "preamble left GIT_DIR set"

# THE payoff assertions — the victim (== the real repo / shared state) is intact.
assert_eq "victim LOCAL config user.name is untouched (a5a5 class neutralized)" \
  "VICTIM_REAL" "$(git -C "$VREPO" config --local user.name)"
assert_eq "victim REMOTE refs/heads/main is untouched (xydb class neutralized)" \
  "$VREMOTE_MAIN_BEFORE" "$(git --git-dir="$VREMOTE" rev-parse refs/heads/main)"
assert_eq "sentinel GLOBAL config is untouched" \
  "SENTINEL_GLOBAL_NAME" "$(git config --file "$SENTINEL_GLOBAL" user.name)"

# Positive containment (asserted inside the child; markers echoed out).
grep -q CONTAINED_CONFIG_OK "$VROOT/child.out" && pass "malicious 'git config' was contained inside the sandbox repo" || fail "malicious 'git config' was NOT contained in the sandbox"
grep -q CONTAINED_PUSH_OK   "$VROOT/child.out" && pass "malicious 'git push origin main' was contained in the sandbox bare origin" || fail "malicious push was NOT contained in the sandbox bare"

# The sandbox is cleaned up when the child subshell exits (trap fired).
SANDBOX_DIR="$(sed -n 's/^SANDBOX=//p' "$VROOT/child.out")"
if [ -n "$SANDBOX_DIR" ]; then
  [ -d "$SANDBOX_DIR" ] && fail "sandbox leaked after child exit (cleanup trap did not fire)" "$SANDBOX_DIR" || pass "sandbox cleaned up on child exit"
else
  fail "could not read the sandbox path the child recorded"
fi

# --------------------------------------------------------------------------
echo "======================================================================"
if [ "$FAIL" -eq 0 ]; then
  echo "test-lib-git-sandbox.sh: all $PASS assertions GREEN"
  exit 0
fi
echo "test-lib-git-sandbox.sh: $FAIL FAILED, $PASS passed"
printf '%b\n' "$FAIL_NAMES"
exit 1
