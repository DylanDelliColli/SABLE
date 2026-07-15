#!/usr/bin/env bash
# test-post-push-merge-notify-isolation.sh — fixture-isolation regression
# harness for SABLE-yn5t (config-escape class; root SABLE-a5a5, template z776).
#
# Proves test-post-push-merge-notify.sh can NEVER pollute the REAL repo's git
# identity or reach the REAL origin remote, even when a fixture `cd` fails
# under a busy-/tmp race or a push targets a CWD-dependent remote NAME. Four
# layers:
#
#   1. Deterministic sabotage (the RED/GREEN gate): shim `mktemp` so the FIRST
#      `mktemp -d` hands back a non-cd-able path (a regular file), reproducing
#      the exact race where the fixture repo dir is unusable at `cd` time. Run
#      the suite from inside a sentinel "real" repo and assert the sentinel's
#      git identity is UNCHANGED afterward. RED on the pre-fix suite (bare
#      `git config user.name Test` runs in the sentinel because the unguarded
#      cd failed silently); GREEN on the fix (`git -C "$REPO" config` can never
#      touch the sentinel AND the guarded cd aborts).
#
#   2. Concurrency under a busy /tmp: N parallel suite runs plus a background
#      /tmp thrasher, same sentinel-identity assertion. Load/regression guard
#      matching the bead's test spec ("run the fixture N times in parallel
#      under a busy /tmp, assert the real repo identity UNCHANGED").
#
#   3. Bare-origin escape simulation (SABLE-ck05, defense-in-depth for the
#      z776 pattern applied to this suite): a `git` shim intercepts any `push`
#      invocation naming the remote by the LITERAL string 'origin' and
#      redirects it to a REAL_ORIGIN_BARE repo standing in for the operator's
#      real upstream — the worst case of what a CWD escape would mean for a
#      push that addresses its remote by name instead of by explicit bare-repo
#      path. Asserts `git ls-remote` on that stand-in gains NO refs after the
#      run. RED on a suite with bare `git push -q origin ...` lines (all 13
#      redirect and land); GREEN once every push addresses its fixture's bare
#      origin by explicit path (the shim's literal-'origin' match never fires,
#      since the pushed argument is a path, not the string 'origin').
#
#   4. Structural: every `git config` in the suite is `git -C`-scoped (no bare
#      form that follows CWD) and every fixture `cd` is guarded.
#
# Standalone — deliberately NOT wired into the .sable pre-push testCommand (the
# suite it guards already runs in the gate; this harness runs the whole suite
# up to N+1 times and is meant for manual / worker verification).
#
# Run:  bash hooks/test/test-post-push-merge-notify-isolation.sh
# Optional arg: path to the suite under test (defaults to the sibling suite;
# used to point the harness at a pre-fix copy for RED verification).

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

# make_sentinel <dir> — a throwaway "real" repo with a KNOWN identity and NO
# origin remote (so a fixture's `git push origin ...` fails quietly rather than
# erroring the harness). Any fixture pollution overwrites this identity.
make_sentinel() {
  local d="$1"
  git init -q "$d"
  git -C "$d" config user.name "Sentinel"
  git -C "$d" config user.email "sentinel@keep"
}

# assert_sentinel_clean <dir> <test-name>
assert_sentinel_clean() {
  local d="$1" name="$2" n e
  n="$(git -C "$d" config --local user.name  2>/dev/null || true)"
  e="$(git -C "$d" config --local user.email 2>/dev/null || true)"
  if [ "$n" = "Sentinel" ] && [ "$e" = "sentinel@keep" ]; then
    pass "$name"
  else
    fail "$name" "sentinel identity mutated: user.name='$n' user.email='$e' — fixture pollution leaked into the real repo"
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

S1="$WORKROOT/sentinel1"
make_sentinel "$S1"
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
assert_sentinel_clean "$S1" \
  "sabotage: a failed fixture cd never mutates the real repo git identity"

# ==========================================================================
# Layer 2 — concurrency under a busy /tmp: N parallel suite runs
# ==========================================================================
S2="$WORKROOT/sentinel2"
make_sentinel "$S2"
mkdir -p "$WORKROOT/t2"

# Background /tmp thrasher: churn mktemp -d create/remove to keep the tmp dir
# busy while the suites run, maximizing contention for the fixture dirs.
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
  # Same OLDPWD pinning as Layer 1 (see note above): contain any `cd -` escape.
  ( cd "$WORKROOT" && cd "$S2" && exec env TMPDIR="$WORKROOT/t2" bash "$SUITE" ) >/dev/null 2>&1 &
  pids="$pids $!"
done
for p in $pids; do wait "$p" 2>/dev/null || true; done

: > "$THRASH_STOP"
wait "$THRASH_PID" 2>/dev/null || true

assert_sentinel_clean "$S2" \
  "concurrency: $N parallel suite runs under a busy /tmp never mutate the real repo git identity"

# ==========================================================================
# Layer 3 — bare-origin escape simulation (SABLE-ck05)
# ==========================================================================
# Stand-in for the operator's real upstream. Starts empty; any ref appearing
# here after the run means a fixture push escaped via the bare remote NAME
# 'origin' rather than an explicit bare-repo path.
REAL_ORIGIN_BARE="$WORKROOT/real-origin.git"
git init -q --bare "$REAL_ORIGIN_BARE"

REAL_GIT="$(command -v git)"
GITSHIMDIR="$WORKROOT/gitshim"; mkdir -p "$GITSHIMDIR"
cat > "$GITSHIMDIR/git" <<EOF
#!/usr/bin/env bash
# Intercept ONLY 'push' invocations whose remote arg is the LITERAL string
# 'origin' (never a path) and redirect to REAL_ORIGIN_BARE — modeling the
# worst case of a CWD escape, where a name-based remote reference resolves to
# the real upstream. A push that addresses its bare origin by explicit path
# (the ck05/z776 fix) never matches this and can never land here, regardless
# of what directory it actually runs from.
if [ "\$1" = "push" ]; then
  shift
  args=()
  matched=0
  for a in "\$@"; do
    if [ "\$matched" = "0" ] && [ "\$a" = "origin" ]; then
      args+=("$REAL_ORIGIN_BARE")
      matched=1
    else
      args+=("\$a")
    fi
  done
  exec "$REAL_GIT" push "\${args[@]}"
fi
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$GITSHIMDIR/git"

S3="$WORKROOT/sentinel3"; mkdir -p "$S3"
mkdir -p "$WORKROOT/t3"
(
  cd "$WORKROOT" && cd "$S3" && exec env PATH="$GITSHIMDIR:$PATH" TMPDIR="$WORKROOT/t3" bash "$SUITE"
) >/dev/null 2>&1 || true

REAL_ORIGIN_REFS="$(git ls-remote "$REAL_ORIGIN_BARE" 2>/dev/null | wc -l)"
if [ "$REAL_ORIGIN_REFS" -eq 0 ]; then
  pass "SABLE-ck05: bare-origin escape sim — real-origin stand-in gains NO refs (explicit bare-path pushes never resolve to 'origin')"
else
  fail "SABLE-ck05: bare-origin escape sim — real-origin stand-in gains NO refs" \
    "ls-remote shows $REAL_ORIGIN_REFS ref(s): $(git ls-remote "$REAL_ORIGIN_BARE" 2>/dev/null)"
fi

# ==========================================================================
# Layer 4 — structural guards on the suite source
# ==========================================================================
# Scoped `git -C "$X" config` does NOT contain the substring "git config";
# only the bare form does. Comment mentions of "git config" are excluded.
BARE_CFG="$(grep -n 'git config' "$SUITE" | grep -vE '^[0-9]+:[[:space:]]*#' || true)"
if [ -n "$BARE_CFG" ]; then
  fail "structural: no bare 'git config' remains (must be 'git -C <dir> config')" "$BARE_CFG"
else
  pass "structural: every 'git config' is scoped via 'git -C <dir>'"
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
