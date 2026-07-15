#!/usr/bin/env bash
# test-lib-identity-isolation.sh — fixture-isolation regression harness for
# SABLE-di86 (combined config-escape + push-escape; siblings SABLE-yn5t /
# SABLE-a5a5 / SABLE-xydb; template z776).
#
# test-lib-identity.sh is in the DEFAULT .sable pre-push testCommand, so it runs
# on EVERY fleet push. Its VAL_REPO fixture formerly did an unguarded
# `cd "$VAL_REPO"` before bare `git config` AND a SILENCED
# `git push -q origin HEAD:refs/heads/main 2>/dev/null`. Under a busy-/tmp race
# where the cd failed, CWD stayed in the REAL worktree and BOTH escaped: the
# config wrote Validator/v@test into the real .git/config, and the push shipped
# the real HEAD to the real origin/main with 2>/dev/null hiding the corruption.
# This harness proves the fixed suite can NEVER pollute the real repo's identity
# OR push to its origin. Three layers:
#
#   1. Deterministic sabotage (RED/GREEN gate): shim `mktemp` so the 4th
#      `mktemp -d` (VAL_REPO) hands back a non-cd-able path, reproducing the
#      cd-failure. Run the suite from inside a sentinel "real" repo that HAS a
#      bare origin, and assert BOTH the sentinel's git identity AND its
#      origin/main ref are unchanged. RED on the pre-fix suite (identity becomes
#      Validator/v@test and origin/main advances to the escaped commit); GREEN
#      on the fix (scoped `git -C` + guarded cd).
#
#   2. Concurrency under a busy /tmp: N parallel suite runs + a /tmp thrasher,
#      same identity + origin-ref assertion. Load/regression guard.
#
#   3. Structural: no bare `git config` command, no unguarded fixture `cd`, and
#      no bare `git push` command remain in the suite.
#
# Standalone — NOT wired into the .sable testCommand (the suite it guards runs
# in the gate already). Run: bash hooks/test/test-lib-identity-isolation.sh
# Optional arg: path to the suite under test (defaults to the sibling suite;
# point it at a pre-fix copy for RED verification).
#
# NOTE on the ordinal: VAL_REPO is the 4th `mktemp -d` in the suite
# (FIXTURE_DIR, then two mk_mode_repo fixtures, then VAL_REPO). The sabotage
# trips on that ordinal; the STRUCTURAL layer below is the ordinal-independent
# gate that catches any reintroduced bare git op regardless of call order.

set -uo pipefail

SUITE="${1:-$(cd "$(dirname "$0")" && pwd)/test-lib-identity.sh}"
case "$SUITE" in
  /*) : ;;
  *)  SUITE="$(cd "$(dirname "$SUITE")" && pwd)/$(basename "$SUITE")" ;;
esac
if [ ! -f "$SUITE" ]; then
  echo "FAIL: suite under test not found at $SUITE"
  exit 2
fi

VAL_ORDINAL=4  # the mktemp -d call that yields VAL_REPO

REAL_MKTEMP="$(command -v mktemp)"
WORKROOT="$("$REAL_MKTEMP" -d "${TMPDIR:-/tmp}/di86-iso.XXXXXX")"
trap 'rm -rf "$WORKROOT"' EXIT

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() {
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  $1"
  echo "FAIL: $1"
  [ -n "${2:-}" ] && echo "  $2"
}

# make_sentinel <dir> <bare> — a throwaway "real" repo WITH an origin remote
# (the bare), a known identity, and origin/main pinned at an initial commit, so
# an escaping `git config` / `git push origin` has a target to corrupt.
make_sentinel() {
  local d="$1" bare="$2"
  git init -q --bare "$bare"
  git clone -q "$bare" "$d" 2>/dev/null
  git -C "$d" config user.name "Sentinel"
  git -C "$d" config user.email "sentinel@keep"
  echo "keep" > "$d/README.md"
  git -C "$d" add README.md
  git -C "$d" commit -q -m "sentinel base"
  git -C "$d" push -q origin HEAD:refs/heads/main 2>/dev/null
}

# assert_sentinel_clean <dir> <bare> <baseline-main-sha> <test-name>
assert_sentinel_clean() {
  local d="$1" bare="$2" base_main="$3" name="$4" n e main_now
  n="$(git -C "$d" config --local user.name  2>/dev/null || true)"
  e="$(git -C "$d" config --local user.email 2>/dev/null || true)"
  main_now="$(git ls-remote "$bare" refs/heads/main 2>/dev/null | awk '{print $1}')"
  if [ "$n" = "Sentinel" ] && [ "$e" = "sentinel@keep" ] && [ "$main_now" = "$base_main" ]; then
    pass "$name"
  else
    fail "$name" "sentinel corrupted: user.name='$n' user.email='$e' origin/main '$base_main' -> '$main_now'"
  fi
}

# ==========================================================================
# Layer 1 — deterministic sabotage: forced VAL_REPO cd failure
# ==========================================================================
SHIMDIR="$WORKROOT/shim"; mkdir -p "$SHIMDIR"
STATE="$WORKROOT/state1"; mkdir -p "$STATE"
mkdir -p "$WORKROOT/t1"

cat > "$SHIMDIR/mktemp" <<EOF
#!/usr/bin/env bash
# SABLE-di86 sabotage: the ${VAL_ORDINAL}th 'mktemp -d' (VAL_REPO) hands back a
# regular FILE (not a directory), so the fixture repo is un-cd-able at cd time —
# the busy-/tmp race that escapes to the real repo/origin. Every other call
# delegates to the real mktemp.
if [ "\$1" = "-d" ]; then
  n="\$(cat "\$MKTEMP_SHIM_STATE/n" 2>/dev/null || echo 0)"; n=\$((n+1))
  printf '%s' "\$n" > "\$MKTEMP_SHIM_STATE/n"
  if [ "\$n" = "$VAL_ORDINAL" ]; then
    bad="\$MKTEMP_SHIM_STATE/not-a-dir-\$\$"
    : > "\$bad"
    printf '%s\n' "\$bad"
    exit 0
  fi
fi
exec "$REAL_MKTEMP" "\$@"
EOF
chmod +x "$SHIMDIR/mktemp"

S1="$WORKROOT/sentinel1"; S1_BARE="$WORKROOT/sentinel1-bare"
make_sentinel "$S1" "$S1_BARE"
S1_MAIN="$(git ls-remote "$S1_BARE" refs/heads/main | awk '{print $1}')"
# Pin OLDPWD into WORKROOT before landing in the sentinel so a vulnerable
# suite's `cd -` can never jump back to the caller's real worktree (the
# self-pollution this harness must not cause when run against a pre-fix suite).
(
  cd "$WORKROOT" && cd "$S1" && exec env \
    PATH="$SHIMDIR:$PATH" \
    TMPDIR="$WORKROOT/t1" \
    MKTEMP_SHIM_STATE="$STATE" \
    bash "$SUITE"
) >/dev/null 2>&1 || true
assert_sentinel_clean "$S1" "$S1_BARE" "$S1_MAIN" \
  "sabotage: a failed VAL_REPO cd never mutates the real repo identity OR pushes to its origin"

# ==========================================================================
# Layer 2 — concurrency under a busy /tmp
# ==========================================================================
S2="$WORKROOT/sentinel2"; S2_BARE="$WORKROOT/sentinel2-bare"
make_sentinel "$S2" "$S2_BARE"
S2_MAIN="$(git ls-remote "$S2_BARE" refs/heads/main | awk '{print $1}')"
mkdir -p "$WORKROOT/t2"

THRASH_STOP="$WORKROOT/thrash.stop"
(
  while [ ! -e "$THRASH_STOP" ]; do
    d="$("$REAL_MKTEMP" -d "$WORKROOT/t2/thrash.XXXXXX" 2>/dev/null)" || continue
    rm -rf "$d" 2>/dev/null
  done
) &
THRASH_PID=$!

N=4
pids=""
for i in $(seq 1 "$N"); do
  ( cd "$WORKROOT" && cd "$S2" && exec env TMPDIR="$WORKROOT/t2" bash "$SUITE" ) >/dev/null 2>&1 &
  pids="$pids $!"
done
for p in $pids; do wait "$p" 2>/dev/null || true; done

: > "$THRASH_STOP"
wait "$THRASH_PID" 2>/dev/null || true

assert_sentinel_clean "$S2" "$S2_BARE" "$S2_MAIN" \
  "concurrency: $N parallel suite runs under a busy /tmp never mutate the real repo identity OR origin"

# ==========================================================================
# Layer 3 — structural guards on the suite source
# ==========================================================================
# A bare `git config` / `git push` COMMAND sits at command position (line start
# after optional whitespace); scoped forms are `git -C "$X" config|push`, and
# the many `git config`/`git push` mentions in test-LABEL strings are mid-line
# inside quotes. Anchoring to `^[[:space:]]*git (config|push) ` matches only
# real command invocations.
BARE_CFG="$(grep -nE '^[[:space:]]*git config ' "$SUITE" || true)"
if [ -n "$BARE_CFG" ]; then
  fail "structural: no bare 'git config' command (must be 'git -C <dir> config')" "$BARE_CFG"
else
  pass "structural: every 'git config' command is scoped via 'git -C <dir>'"
fi

BARE_PUSH="$(grep -nE '^[[:space:]]*git push ' "$SUITE" || true)"
if [ -n "$BARE_PUSH" ]; then
  fail "structural: no bare 'git push' command (must be 'git -C <dir> push')" "$BARE_PUSH"
else
  pass "structural: every 'git push' command is scoped via 'git -C <dir>'"
fi

UNGUARDED_CD="$(grep -nE '^[[:space:]]*cd "\$' "$SUITE" | grep -v '||' || true)"
if [ -n "$UNGUARDED_CD" ]; then
  fail "structural: no unguarded 'cd \"\$fixture\"' remains" "$UNGUARDED_CD"
else
  pass "structural: every fixture cd is guarded ('|| exit' / '|| return')"
fi

# ==========================================================================
# Summary
# ==========================================================================
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
