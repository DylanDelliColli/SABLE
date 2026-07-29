#!/usr/bin/env bash
# test-post-push-merge-notify-isolation.sh — fixture-isolation regression
# harness for SABLE-yn5t (config-escape class; root SABLE-a5a5, template z776).
#
# Proves test-post-push-merge-notify.sh can NEVER pollute the REAL repo's git
# identity or reach the REAL origin remote when a fixture `cd` fails, and that
# any checkout gate it reaches is the code under review. Three layers:
#
#   0. Checkout-gate non-vacuity (SABLE-2nz3o): resolve the gate through the
#      exact PATH inherited by the nested suite, then execute `preview` against
#      a disposable local bare origin and require the real ci-verify ref. A
#      broken checkout preview library must therefore turn this harness RED.
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
# sable-test-load: nested-runner -- one local checkout-gate preview plus one deterministic sabotage run

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/../.." && pwd)"
CHECKOUT_BIN="$REPO_ROOT/bin"
CHECKOUT_MERGE_GATE="$CHECKOUT_BIN/sable-merge-gate"
SUITE="${1:-$HARNESS_DIR/test-post-push-merge-notify.sh}"
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

SHIMDIR="$WORKROOT/shim"; mkdir -p "$SHIMDIR"

# This is the PATH inherited by the suite before its own env -i runner prepends
# STUB_DIR. Because neither fixture shim provides sable-merge-gate, resolving
# against this PATH proves which executable that nested runner will exercise.
SUITE_ENTRY_PATH="$SHIMDIR:$CHECKOUT_BIN:$PATH"
RESOLVED_MERGE_GATE="$(
  env -i PATH="$SUITE_ENTRY_PATH" /bin/sh -c 'command -v sable-merge-gate' 2>/dev/null || true
)"
if [ "$RESOLVED_MERGE_GATE" = "$CHECKOUT_MERGE_GATE" ]; then
  pass "code-under-test: nested env-i resolves checkout bin/sable-merge-gate"
else
  fail "code-under-test: nested env-i resolves checkout bin/sable-merge-gate" \
    "expected='$CHECKOUT_MERGE_GATE' resolved='${RESOLVED_MERGE_GATE:-<none>}'"
fi

# Execute the resolved checkout gate, not merely command-v it. The preview
# command only reads/writes the disposable repos below: no Actions query, bead
# mutation, notification, installed snapshot, network remote, or operator repo.
GATE_BASE="trunk"
GATE_BRANCH="wk-code-under-test"
GATE_ORIGIN="$WORKROOT/gate-origin.git"
GATE_WORK="$WORKROOT/gate-work"
GATE_HOME="$WORKROOT/gate-home"
mkdir -p "$GATE_HOME"
git init -q --bare -b "$GATE_BASE" "$GATE_ORIGIN"
git clone -q "$GATE_ORIGIN" "$GATE_WORK" 2>/dev/null
git -C "$GATE_WORK" config user.name "SABLE Gate Oracle"
git -C "$GATE_WORK" config user.email "gate-oracle@sable.invalid"
printf 'base\n' > "$GATE_WORK/base.txt"
git -C "$GATE_WORK" add base.txt
git -C "$GATE_WORK" commit -q -m "gate oracle base"
git -C "$GATE_WORK" push -q origin "$GATE_BASE" 2>/dev/null
git -C "$GATE_WORK" checkout -q -b "$GATE_BRANCH"
printf 'worker\n' > "$GATE_WORK/worker.txt"
git -C "$GATE_WORK" add worker.txt
git -C "$GATE_WORK" commit -q -m "gate oracle worker"
git -C "$GATE_WORK" push -q origin "$GATE_BRANCH" 2>/dev/null

# Regression polarity for the preview library's resolved-remote contract:
# redirect only a literal `git push origin ...` to a second LOCAL bare repo.
# Correct checkout code pushes to GATE_ORIGIN's explicit path and never trips
# this branch; reverting to the old CWD-sensitive remote name leaves the
# expected origin empty and makes the oracle fail without touching a network.
GATE_WRONG_ORIGIN="$WORKROOT/gate-wrong-origin.git"
GATE_REAL_GIT="$(command -v git)"
GATE_GIT_SHIM="$WORKROOT/gate-git"
git init -q --bare -b "$GATE_BASE" "$GATE_WRONG_ORIGIN"
cat > "$GATE_GIT_SHIM" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "push" ]; then
  shift
  args=()
  redirected=0
  for arg in "$@"; do
    if [ "$redirected" -eq 0 ] && [ "$arg" = "origin" ]; then
      args+=("$GATE_WRONG_ORIGIN")
      redirected=1
    else
      args+=("$arg")
    fi
  done
  exec "$GATE_REAL_GIT" push "${args[@]}"
fi
exec "$GATE_REAL_GIT" "$@"
EOF
chmod +x "$GATE_GIT_SHIM"

GATE_SHIM_CONTROL_REF="refs/heads/literal-origin-control"
GATE_SHIM_RC=0
(
  cd "$GATE_WORK" && env \
    GATE_REAL_GIT="$GATE_REAL_GIT" \
    GATE_WRONG_ORIGIN="$GATE_WRONG_ORIGIN" \
    "$GATE_GIT_SHIM" push origin "HEAD:$GATE_SHIM_CONTROL_REF"
) >/dev/null 2>&1 || GATE_SHIM_RC=$?
if [ "$GATE_SHIM_RC" -eq 0 ] \
   && git --git-dir="$GATE_WRONG_ORIGIN" show-ref --verify --quiet "$GATE_SHIM_CONTROL_REF" \
   && ! git --git-dir="$GATE_ORIGIN" show-ref --verify --quiet "$GATE_SHIM_CONTROL_REF"; then
  pass "polarity: a literal push origin is diverted to the disposable wrong target"
else
  fail "polarity: a literal push origin is diverted to the disposable wrong target" \
    "rc=$GATE_SHIM_RC expected-origin or wrong-origin did not preserve the control"
fi

GATE_RC=0
GATE_OUT="$(
  env -i \
    PATH="$SUITE_ENTRY_PATH" \
    HOME="$GATE_HOME" \
    GATE_REAL_GIT="$GATE_REAL_GIT" \
    GATE_WRONG_ORIGIN="$GATE_WRONG_ORIGIN" \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_TERMINAL_PROMPT=0 \
    SABLE_MG_GIT="$GATE_GIT_SHIM" \
    SABLE_MG_GH=false \
    SABLE_MG_BD=true \
    SABLE_MG_NOTIFY=true \
    sable-merge-gate preview \
      --branch "$GATE_BRANCH" \
      --base "$GATE_BASE" \
      --repo "$GATE_WORK" \
      --remote origin 2>&1
)" || GATE_RC=$?
GATE_REFS="$(
  git --git-dir="$GATE_ORIGIN" for-each-ref \
    --format='%(refname:short)' refs/heads/ci-verify/
)"
GATE_WRONG_REFS="$(
  git --git-dir="$GATE_WRONG_ORIGIN" for-each-ref \
    --format='%(refname:short)' refs/heads/ci-verify/
)"
GATE_REF_COUNT="$(printf '%s\n' "$GATE_REFS" | awk 'NF { n++ } END { print n + 0 }')"
GATE_WRONG_REF_COUNT="$(
  printf '%s\n' "$GATE_WRONG_REFS" | awk 'NF { n++ } END { print n + 0 }'
)"
if [ "$GATE_RC" -eq 0 ] \
   && [ "$GATE_REF_COUNT" -eq 1 ] \
   && [ "$GATE_WRONG_REF_COUNT" -eq 0 ] \
   && printf '%s' "$GATE_OUT" | grep -qi 'NOT waiting for CI'; then
  pass "code-under-test: checkout gate executes preview and pushes one ci-verify ref to the explicit local target"
else
  fail "code-under-test: checkout gate executes preview and pushes one ci-verify ref to the explicit local target" \
    "rc=$GATE_RC expected-refs=[$GATE_REFS] wrong-refs=[$GATE_WRONG_REFS] out=$GATE_OUT"
fi

# ==========================================================================
# Layer 1 — deterministic sabotage: forced fixture-cd failure
# ==========================================================================
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
    PATH="$SUITE_ENTRY_PATH" \
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
