#!/usr/bin/env bash
# test-coverage-floor-gate.sh — coverage floor on pruning passes (SABLE-cmar4.5,
# story S3: diff-cover patch-coverage semantics — strict patch gate, lenient
# project floor).
#
# WHAT IS UNDER TEST
# ------------------
# bin/sable_gate_promote_lib.assert_coverage_floor, wired into promote() right
# after the freeze check (SABLE-jd5fj.5's exit-25 precedent — a MECHANICAL deny
# path, not a convention). A diff that removes a test function, adds a skip
# marker, or deletes a test file ("pruning") must carry a REAL, PASSING
# coverage-delta check (.github/ci/diff-cover-gate.sh: pytest + coverage.py +
# diff-cover --fail-under, all real) or the promote is DENIED (exit 27) unless
# a human recorded a named "Coverage override: <reason>" / --coverage-override
# reason.
#
# THE FIXTURE IS THE ARGUMENT, same discipline as test-optimistic-promotion.sh
# and test-snapshot-freeze.sh: real bare origin + real working clone, and the
# ACTUAL .github/ci/diff-cover-gate.sh shipped in this repo (copied into the
# sandbox, not reimplemented) runs REAL pytest + coverage.py + diff-cover
# against a real tiny python module. No mocked coverage db, no stubbed
# diff-cover — the over-prune in C1 is caught because the tool genuinely
# measures 0% coverage on the changed lines, not because a fixture said so.
#
#   C1 a pruning diff (modifies bar(), deletes its ONLY test) whose real
#      diff-cover run genuinely fails patch coverage -> DENIED, exit 27,
#      nothing promoted. Non-vacuity for C3: the SAME mutation, overridden,
#      promotes — so C1's denial is the check actually firing, not a fixture
#      that can only ever deny.
#   C2 a pruning diff that ALSO deletes .github/ci/diff-cover-gate.sh itself
#      (the check is not merely failing, it is ABSENT) -> DENIED, exit 27,
#      fail-closed exactly like assert_not_frozen's unreadable-freeze-file
#      contract. Defends against a pruning PR that stripped its own gate.
#   C3 C1's identical failing mutation, but with --coverage-override
#      "<reason>" -> PROMOTES (exit 0), byte-identical fast-forward. The named
#      reason is a human bypass and consults no run at all, by contract
#      (mirrors promote()'s own --override).
#   C4 a NON-pruning diff (new function, new covering test, nothing removed/
#      skipped/deleted) promotes normally -> the floor is scoped to pruning
#      diffs only, never a general coverage gate.
#
# ACTIVATION NOTE (SABLE-jd5fj.5 precedent, applies identically here):
# bin/sable-merge-gate is a PINNED SNAPSHOT DIRECTORY, so merging this changes
# nothing at runtime until an operator-brokered pin refresh. Everything here
# runs against the WORKING TREE.
#
# Run with:
#   bash hooks/test/test-coverage-floor-gate.sh
#
# Clean-room safe (SABLE-59zu): needs bash + git + python3 PLUS pytest-cov +
# diff-cover on PATH — both installed alongside pytest-testmon/pytest-impact
# in ci-verify.yml's clean-room deps step. Fails LOUD (FATAL, not a silent
# skip) if either is missing locally, since a self-skip here would be exactly
# the false-green SABLE-7v3z exists to prevent for a suite whose whole point
# is a real coverage-delta check. bd / gh are still stubbed; nothing here
# touches a real remote.

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
GATE="$REPO_ROOT/bin/sable-merge-gate"
DIFF_COVER_GATE="$REPO_ROOT/.github/ci/diff-cover-gate.sh"

# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -f "$GATE" ] || { echo "FATAL: missing $GATE"; exit 2; }
[ -f "$DIFF_COVER_GATE" ] || { echo "FATAL: missing $DIFF_COVER_GATE"; exit 2; }
command -v diff-cover >/dev/null 2>&1 || { echo "FATAL: diff-cover not on PATH — pip install diff_cover"; exit 2; }
python3 -c "import pytest_cov" 2>/dev/null || { echo "FATAL: pytest-cov not installed — pip install pytest-cov"; exit 2; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"; sable_test_git_sandbox_cleanup' EXIT

BASE_BR="trunk"

# A fake `gh` reporting a completed successful run for whatever ci-verify ref
# the gate asks about — same shape as test-optimistic-promotion.sh's FAKE_GH,
# minus the base-move injection this suite has no use for.
FAKE_GH="$TMPROOT/fake-gh"
cat > "$FAKE_GH" <<'EOF'
#!/usr/bin/env python3
import json, os, subprocess, sys
od = os.environ["FAKE_GH_ORIGIN"]
a = sys.argv[1:]
ref = a[a.index("--branch") + 1]
sha = subprocess.run(["git", "--git-dir=" + od, "rev-parse", "refs/heads/" + ref],
                     text=True, capture_output=True).stdout.strip()
print(json.dumps([{"databaseId": 1, "headSha": sha, "status": "completed",
                   "conclusion": "success", "url": "http://fake/run/1"}]))
EOF
chmod +x "$FAKE_GH"

# --------------------------------------------------------------------------
# The sandbox repo: a real (tiny) python module + tests, and the ACTUAL
# diff-cover-gate.sh this bead ships (copied, not reimplemented).
# --------------------------------------------------------------------------
seed_repo() {
  local w="$1"
  mkdir -p "$w/bin" "$w/.github/ci"
  cat > "$w/bin/mod.py" <<'EOF'
def foo(x):
    return x


def bar(x):
    return x + 1
EOF
  cat > "$w/bin/test_mod.py" <<'EOF'
from mod import foo, bar


def test_foo():
    assert foo(1) == 1


def test_bar():
    assert bar(1) == 2
EOF
  cp "$DIFF_COVER_GATE" "$w/.github/ci/diff-cover-gate.sh"
  chmod +x "$w/.github/ci/diff-cover-gate.sh"
}

setup_pair() {
  local name="$1"
  ORIGIN="$TMPROOT/$name-origin.git"
  WORK="$TMPROOT/$name-work"
  git init -q --bare -b "$BASE_BR" "$ORIGIN"
  git clone -q "$ORIGIN" "$WORK" 2>/dev/null
  git -C "$WORK" config user.email "t@sable.invalid"
  git -C "$WORK" config user.name "SABLE Test"
  seed_repo "$WORK"
  git -C "$WORK" add -A
  git -C "$WORK" commit -q -m init
  git -C "$WORK" push -q origin "$BASE_BR"
  BASE_SHA="$(git -C "$WORK" rev-parse "$BASE_BR")"
}

# mut_prune_uncovered — modifies bar() (new branch/lines land IN the diff) and
# removes test_bar, its ONLY covering test. test_foo survives, so pytest still
# collects and runs a test (an all-tests-removed fixture would exit 5, not the
# scenario under test). This is the exact shape the module docstring calls out:
# a change to an existing function, net-pruned of its own coverage.
mut_prune_uncovered() {
  cat > "$WORK/bin/mod.py" <<'EOF'
def foo(x):
    return x


def bar(x):
    if x > 0:
        return x + 2
    return x - 2
EOF
  cat > "$WORK/bin/test_mod.py" <<'EOF'
from mod import foo


def test_foo():
    assert foo(1) == 1
EOF
}

mut_delete_the_check_itself() {
  mut_prune_uncovered
  rm -f "$WORK/.github/ci/diff-cover-gate.sh"
}

mut_non_pruning() {
  cat > "$WORK/bin/mod.py" <<'EOF'
def foo(x):
    return x


def bar(x):
    return x + 1


def baz(x):
    return x * 2
EOF
  cat > "$WORK/bin/test_mod.py" <<'EOF'
from mod import foo, bar, baz


def test_foo():
    assert foo(1) == 1


def test_bar():
    assert bar(1) == 2


def test_baz():
    assert baz(3) == 6
EOF
}

commit_push() {
  git -C "$WORK" add -A
  git -C "$WORK" commit -q -m "$1"
  git -C "$WORK" push -q origin "$(git -C "$WORK" rev-parse --abbrev-ref HEAD)"
}

gate() {
  env FAKE_GH_ORIGIN="$ORIGIN" \
      SABLE_MG_GH="$FAKE_GH" SABLE_MG_BD=true SABLE_MG_NOTIFY=true \
      SABLE_MG_POLL=0 SABLE_MG_GRACE=0 SABLE_MG_TIMEOUT=0 \
      PATH="$PATH" python3 "$GATE" "$@" 2>&1
}

origin_sha() { git --git-dir="$ORIGIN" rev-parse "refs/heads/$1" 2>/dev/null; }
ci_refs() { git --git-dir="$ORIGIN" for-each-ref --format='%(refname:short)' refs/heads/ci-verify/; }

# ==========================================================================
# C1 — real over-prune, real diff-cover failure -> DENIED, exit 27
# ==========================================================================
setup_pair c1
git -C "$WORK" checkout -q -b wk-1
mut_prune_uncovered
commit_push "prune bar's only test"
WK1_SHA="$(origin_sha wk-1)"

OUT="$(gate promote --bead TEST-C1 --branch wk-1 --base "$BASE_BR" --repo "$WORK" --remote origin)"; RC=$?

if [ "$RC" -eq 27 ]; then
  pass "C1: a pruning diff with a genuinely-failing coverage-delta check is DENIED (exit 27)"
else
  fail "C1: pruning diff with a failing check is denied" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -qi 'FAILED'; then
  pass "C1: the deny message names the check as FAILED (diff-cover actually ran)"
else
  fail "C1: the deny message names a real failure" "out=$OUT"
fi
if printf '%s' "$OUT" | grep -q 'test_bar\|removed test function'; then
  pass "C1: the deny message names the pruning signal (removed test_bar)"
else
  fail "C1: the deny message names the pruning signal" "out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$BASE_SHA" ]; then
  pass "C1: the integration branch tip did not move"
else
  fail "C1: the integration branch tip did not move" "tip=$(origin_sha "$BASE_BR") want=$BASE_SHA"
fi
if [ -z "$(ci_refs)" ]; then
  pass "C1: no ci-verify preview ref was ever pushed — denied before any CI work"
else
  fail "C1: no preview ref should exist" "refs=$(ci_refs)"
fi

# ==========================================================================
# C2 — pruning diff that ALSO deletes the check itself -> DENIED, exit 27,
# fail-closed (absence, not merely failure)
# ==========================================================================
setup_pair c2
git -C "$WORK" checkout -q -b wk-2
mut_delete_the_check_itself
commit_push "prune bar's only test AND drop the coverage gate script"

OUT="$(gate promote --bead TEST-C2 --branch wk-2 --base "$BASE_BR" --repo "$WORK" --remote origin)"; RC=$?

if [ "$RC" -eq 27 ]; then
  pass "C2: a pruning diff with NO carried check at all is DENIED (exit 27)"
else
  fail "C2: pruning diff with an absent check is denied" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -qi 'no coverage-delta check\|not.*carried\|carried'; then
  pass "C2: the deny message says the check was not carried (not just 'failed')"
else
  fail "C2: the deny message distinguishes absence from failure" "out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$BASE_SHA" ]; then
  pass "C2: the integration branch tip did not move"
else
  fail "C2: the integration branch tip did not move" "tip=$(origin_sha "$BASE_BR")"
fi

# ==========================================================================
# C3 — C1's identical failing mutation, named override -> PROMOTES, exit 0
# ==========================================================================
setup_pair c3
git -C "$WORK" checkout -q -b wk-3
mut_prune_uncovered
commit_push "prune bar's only test (overridden)"

gate preview --branch wk-3 --base "$BASE_BR" --repo "$WORK" --remote origin >/dev/null
KICKED_REF="$(ci_refs | head -1)"
if [ -z "$KICKED_REF" ]; then
  fail "C3: setup — a preview ref was kicked" "no ci-verify/* ref found"
else
  pass "C3: setup — a preview ref was kicked ($KICKED_REF)"
fi
# The pushed preview is a NEW merge-tree/commit-tree object (parents base +
# branch) — byte-identical promotion means the base tip lands on THAT object,
# never the raw single-parent worker commit, even when the merge is a
# trivial fast-forward. Captured before promote deletes the ref (cleanup).
KICKED_SHA="$(origin_sha "$KICKED_REF")"

OUT="$(gate promote --bead TEST-C3 --branch wk-3 --base "$BASE_BR" --repo "$WORK" --remote origin \
       --coverage-override "known regression window, tracked in TEST-C3")"; RC=$?

if [ "$RC" -eq 0 ]; then
  pass "C3: the SAME failing mutation promotes with a named coverage override (exit 0)"
else
  fail "C3: a named override allows the promote" "rc=$RC out=$OUT"
fi
if [ -n "$KICKED_SHA" ] && [ "$(origin_sha "$BASE_BR")" = "$KICKED_SHA" ]; then
  pass "C3: the integration branch tip advanced to the tested preview, byte-identical"
else
  fail "C3: byte-identical promotion under override" "tip=$(origin_sha "$BASE_BR") want=$KICKED_SHA"
fi

# ==========================================================================
# C4 — a non-pruning diff is unaffected by the floor -> PROMOTES, exit 0
# ==========================================================================
setup_pair c4
git -C "$WORK" checkout -q -b wk-4
mut_non_pruning
commit_push "add baz(), with its own covering test"

gate preview --branch wk-4 --base "$BASE_BR" --repo "$WORK" --remote origin >/dev/null
KICKED_REF4="$(ci_refs | head -1)"
KICKED_SHA4="$(origin_sha "$KICKED_REF4")"

OUT="$(gate promote --bead TEST-C4 --branch wk-4 --base "$BASE_BR" --repo "$WORK" --remote origin)"; RC=$?

if [ "$RC" -eq 0 ]; then
  pass "C4: a non-pruning diff promotes normally — the floor never fires on it"
else
  fail "C4: a non-pruning diff promotes normally" "rc=$RC out=$OUT"
fi
if [ -n "$KICKED_SHA4" ] && [ "$(origin_sha "$BASE_BR")" = "$KICKED_SHA4" ]; then
  pass "C4: byte-identical promotion, untouched by the coverage floor"
else
  fail "C4: byte-identical promotion" "tip=$(origin_sha "$BASE_BR") want=$KICKED_SHA4"
fi

echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
[ "$FAIL" -eq 0 ]
