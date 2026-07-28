#!/usr/bin/env bash
# test-post-push-merge-notify-isolation.sh — fixture-isolation regression
# harness for SABLE-yn5t (config-escape class; root SABLE-a5a5, template z776).
#
# Proves test-post-push-merge-notify.sh can NEVER pollute the REAL repo's git
# identity or reach the REAL origin remote when a fixture `cd` fails. Two
# layers:
#
#   1. Deterministic sabotage (the RED/GREEN gate): shim `mktemp` so the FIRST
#      `mktemp -d` hands back a non-cd-able path (a regular file), reproducing
#      the exact race where the fixture repo dir is unusable at `cd` time. Run
#      the suite from inside a sentinel repo with a bare origin and assert BOTH
#      identity and origin/main are unchanged. RED on the pre-fix suite (bare
#      git operations escape after the failed cd); GREEN on scoped operations
#      plus the guarded cd.
#
#   2. Structural: every `git config` is `git -C`-scoped, no push names the
#      CWD-dependent literal remote `origin`, and every fixture `cd` is guarded.
#
# Standalone — not wired into the .sable pre-push testCommand (the suite it
# guards already runs there); independently classified by the sealed shell gate.
#
# Run:  bash hooks/test/test-post-push-merge-notify-isolation.sh
# Optional arg: path to the suite under test (defaults to the sibling suite;
# used to point the harness at a pre-fix copy for RED verification).
#
# sable-test-load: nested-runner -- one deterministic sabotage run proves config/push containment

set -uo pipefail

SUITE="${1:-$(cd "$(dirname "$0")" && pwd)/test-post-push-merge-notify.sh}"
# Absolute — the suite resolves its HOOK path from $0's dirname, and we launch
# it with CWD set to a sentinel repo, so a relative path would misresolve.
case "$SUITE" in
  /*) : ;;
  *)  SUITE="$(cd "$(dirname "$SUITE")" && pwd)/$(basename "$SUITE")" ;;
esac

if [ ! -f "$SUITE" ]; then
  echo "FAIL: suite under test not found at $SUITE"
  exit 2
fi

REAL_MKTEMP="$(command -v mktemp)"
WORKROOT="$("$REAL_MKTEMP" -d "${TMPDIR:-/tmp}/yn5t-iso.XXXXXX")"
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

# make_sentinel <dir> <bare> — a throwaway "real" repo with a KNOWN identity,
# one commit, and origin/main pinned at that commit. Escaped config or push
# operations therefore leave an observable mutation.
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
    fail "$name" "sentinel mutated: identity='$n <$e>' origin/main '$base_main' -> '$main_now'"
  fi
}

# ==========================================================================
# Layer 1 — deterministic sabotage: forced fixture-cd failure
# ==========================================================================
SHIMDIR="$WORKROOT/shim"; mkdir -p "$SHIMDIR"
STATE="$WORKROOT/state1"; mkdir -p "$STATE"
mkdir -p "$WORKROOT/t1"

cat > "$SHIMDIR/mktemp" <<EOF
#!/usr/bin/env bash
# SABLE-yn5t sabotage: the FIRST 'mktemp -d' hands back a regular FILE (not a
# directory), so the fixture repo is un-cd-able at cd time — the busy-/tmp race
# that pollutes the real repo. Every later call delegates to the real mktemp.
if [ "\$1" = "-d" ] && [ ! -e "\$MKTEMP_SHIM_STATE/tripped" ]; then
  : > "\$MKTEMP_SHIM_STATE/tripped"
  bad="\$MKTEMP_SHIM_STATE/not-a-dir-\$\$"
  : > "\$bad"
  printf '%s\n' "\$bad"
  exit 0
fi
exec "$REAL_MKTEMP" "\$@"
EOF
chmod +x "$SHIMDIR/mktemp"

S1="$WORKROOT/sentinel1"; S1_BARE="$WORKROOT/sentinel1-bare"
make_sentinel "$S1" "$S1_BARE"
S1_MAIN="$(git ls-remote "$S1_BARE" refs/heads/main | awk '{print $1}')"
# Pin OLDPWD to WORKROOT before landing in the sentinel: a vulnerable suite's
# `cd "$fixture"` failure leaves CWD unchanged, and a later `cd -` jumps to
# OLDPWD — if that were the caller's real worktree, the fixture's bare git ops
# would run THERE (the exact self-pollution this harness must not cause when
# pointed at a pre-fix suite for RED verification). WORKROOT is disposable.
(
  cd "$WORKROOT" && cd "$S1" && exec env \
    PATH="$SHIMDIR:$PATH" \
    TMPDIR="$WORKROOT/t1" \
    MKTEMP_SHIM_STATE="$STATE" \
    bash "$SUITE"
) >/dev/null 2>&1 || true
assert_sentinel_clean "$S1" "$S1_BARE" "$S1_MAIN" \
  "sabotage: a failed fixture cd never mutates real repo identity OR origin/main"

# ==========================================================================
# Layer 2 — structural guards on the suite source
# ==========================================================================
# Scoped `git -C "$X" config` does NOT contain the substring "git config";
# only the bare form does. Comment mentions of "git config" are excluded.
BARE_CFG="$(grep -n 'git config' "$SUITE" | grep -vE '^[0-9]+:[[:space:]]*#' || true)"
if [ -n "$BARE_CFG" ]; then
  fail "structural: no bare 'git config' remains (must be 'git -C <dir> config')" "$BARE_CFG"
else
  pass "structural: every 'git config' is scoped via 'git -C <dir>'"
fi

# A literal `origin` remote is CWD-dependent. Fixture pushes must name their
# explicit bare-repo path, regardless of whether they use `git` or `git -C`.
LITERAL_ORIGIN_PUSH="$(grep -nE '^[[:space:]]*git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push([[:space:]]+[^[:space:]]+)*[[:space:]]+\"?origin\"?([[:space:]]|$)' "$SUITE" || true)"
if [ -n "$LITERAL_ORIGIN_PUSH" ]; then
  fail "structural: no fixture push names the CWD-dependent literal remote 'origin'" "$LITERAL_ORIGIN_PUSH"
else
  pass "structural: every fixture push names an explicit bare-repo path"
fi

# Every `cd "$var"` into a fixture must be guarded (the cd_fixture helper is the
# only bare `cd "$..."`, and it carries `||`). `cd - ` returns are exempt.
UNGUARDED_CD="$(grep -nE '^[[:space:]]*cd "\$' "$SUITE" | grep -v '||' || true)"
if [ -n "$UNGUARDED_CD" ]; then
  fail "structural: no unguarded 'cd \"\$fixture\"' remains" "$UNGUARDED_CD"
else
  pass "structural: every fixture cd is guarded (cd_fixture / '|| exit')"
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
