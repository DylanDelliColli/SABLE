#!/usr/bin/env bash
# test-dep-merge-state.sh — INTEGRATION coverage for the close-vs-merge gap
# (SABLE-d5iku).
#
# THE DEFECT, REPRODUCED END TO END
# ---------------------------------
# `bd ready` releases a dependent the instant its blocker's STATUS becomes
# closed. A dependent sequenced behind a blocker for STRUCTURAL reasons needs
# the blocker's CODE on the branch it forks from, and those two events are
# separated by the whole merge queue. This suite stands the real defect up:
#
#   real bd  — a real blocker bead and a real dependent bead in the real bd DB,
#              wired with a real `bd dep add`. The suite ASSERTS the dependent
#              is withheld while the blocker is open and ASSERTS it appears in
#              `bd ready` the moment the blocker closes. That second assertion
#              IS the false release; if bd ever becomes merge-aware (option (b)
#              of the bead) this test says so out loud instead of going stale.
#   real git — a real bare origin, a real push of the blocker's branch, a real
#              `git merge` + push to move it onto the integration branch. NO
#              stubs anywhere in the merge path.
#
# Then it asserts bin/sable-dep-check WARNS across the false-release window and
# goes SILENT once the branch actually merges. Both directions, because a check
# that only fires has traded a false-go for a false-block.
#
# Mocking either side would defeat the point: the whole claim is about the
# relationship between two real systems (bd's status graph and git's ancestry),
# and a mock of either just replays the author's assumption.
#
# CLEAN-ROOM (SABLE-59zu): bd is deliberately absent from the ci-verify
# clean-room. The GIT half — the ancestry engine, which is the part that cannot
# be stubbed — runs there unconditionally against a real repo. The bd half
# SKIPs loudly, is never counted as a pass, and prints why. Nothing here
# skip-and-exits-0 as a whole suite.
#
# Run with:
#   bash hooks/test/test-dep-merge-state.sh

set -uo pipefail

# Resolve absolute paths BEFORE the sandbox preamble cds away (SABLE-0ssz.2).
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$TESTDIR/../.." && pwd)"
DEP_CHECK="$REPO/bin/sable-dep-check"

# Env-neutralize real-repo git escapes for the suite duration. Every git op
# below names its own fixture repo with -C; this is defence in depth.
# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
SKIP=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() {
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"
  echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"
}
skip() { SKIP=$((SKIP+1)); echo "SKIP: $1"; }

if [ ! -x "$DEP_CHECK" ]; then
  echo "FAIL: sable-dep-check not executable at $DEP_CHECK"
  exit 2
fi

# ---------------------------------------------------------------------------
# Real git fixture: bare origin + work clone + an UNMERGED blocker branch
# ---------------------------------------------------------------------------
FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT

ORIGIN="$FIX/origin.git"
WORK="$FIX/work"
INT_BRANCH="tmux-only"
BLOCKER_BRANCH="wk-depmerge-$$"

git init -q --bare "$ORIGIN"
git init -q "$WORK"
git -C "$WORK" config user.email "test@example.invalid"
git -C "$WORK" config user.name "SABLE Test"
# The tool must judge ancestry against the repo's OWN integration branch, so
# configure it exactly as a real SABLE checkout does.
git -C "$WORK" config sable.integrationBranch "$INT_BRANCH"
git -C "$WORK" remote add origin "$ORIGIN"

git -C "$WORK" checkout -q -b "$INT_BRANCH"
echo "base layout" > "$WORK/layout.txt"
git -C "$WORK" add layout.txt
git -C "$WORK" commit -qm "base"
git -C "$WORK" push -q origin "$INT_BRANCH"

# The blocker's work: the structural prerequisite a dependent would fork onto.
git -C "$WORK" checkout -q -b "$BLOCKER_BRANCH"
echo "snapshot unit" > "$WORK/prerequisite.txt"
git -C "$WORK" add prerequisite.txt
git -C "$WORK" commit -qm "blocker: define the snapshot unit"
git -C "$WORK" push -q origin "$BLOCKER_BRANCH"
git -C "$WORK" checkout -q "$INT_BRANCH"
git -C "$WORK" fetch -q origin

# Precondition, stated as an assertion so nothing below can pass vacuously:
# pushed and NOT merged is the exact state of the live incident.
git -C "$WORK" merge-base --is-ancestor "origin/$BLOCKER_BRANCH" "origin/$INT_BRANCH"
if [ $? -eq 1 ]; then
  pass "fixture: blocker branch is pushed to origin and NOT merged (the false-release window)"
else
  fail "fixture: blocker branch is pushed to origin and NOT merged (the false-release window)" \
       "merge-base --is-ancestor did not report unmerged"
fi

# ---------------------------------------------------------------------------
# Real bd: blocker + dependent, really wired, really closed
# ---------------------------------------------------------------------------
BLOCKER_ID=""
DEPENDENT_ID=""
bd_cleanup() {
  [ -n "$DEPENDENT_ID" ] && bd -C "$REPO" close "$DEPENDENT_ID" --sandbox \
    --reason "[no-test] test-dep-merge-state scratch" >/dev/null 2>&1
  [ -n "$BLOCKER_ID" ] && bd -C "$REPO" close "$BLOCKER_ID" --sandbox \
    --reason "[no-test] test-dep-merge-state scratch" >/dev/null 2>&1
  rm -rf "$FIX"
}

if ! command -v bd >/dev/null 2>&1; then
  skip "bd half: bd not on PATH (SABLE-59zu clean-room) — the git ancestry half above/below still ran for real"
else
  trap bd_cleanup EXIT

  # bd is invoked with -C "$REPO" throughout: the sandbox preamble moved CWD
  # away from the real checkout, and bd auto-discovers .beads/*.db from CWD.
  BLOCKER_ID=$(bd -C "$REPO" create --sandbox -q --type=task \
    --title="[int-test] d5iku blocker ($BLOCKER_BRANCH)" \
    --description="[no-test] scratch blocker for test-dep-merge-state.sh" \
    2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9.]+' | head -1)
  DEPENDENT_ID=$(bd -C "$REPO" create --sandbox -q --type=task \
    --title="[int-test] d5iku dependent ($BLOCKER_BRANCH)" \
    --description="[no-test] scratch dependent for test-dep-merge-state.sh" \
    2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9.]+' | head -1)

  if [ -z "$BLOCKER_ID" ] || [ -z "$DEPENDENT_ID" ]; then
    fail "bd fixture: created scratch blocker + dependent beads" \
         "blocker='$BLOCKER_ID' dependent='$DEPENDENT_ID'"
  else
    echo "Integration: blocker=$BLOCKER_ID dependent=$DEPENDENT_ID branch=$BLOCKER_BRANCH"

    # The STRUCTURED branch tag bin/sable-spawn-worker writes at dispatch —
    # the same key the checker reads back.
    bd -C "$REPO" update "$BLOCKER_ID" --sandbox \
      --set-metadata "branch=$BLOCKER_BRANCH" >/dev/null 2>&1
    # A real blocking edge: dependent needs blocker.
    bd -C "$REPO" dep add "$DEPENDENT_ID" "$BLOCKER_ID" >/dev/null 2>&1

    ready_has() {
      bd -C "$REPO" ready --json 2>/dev/null | python3 -c "
import json, sys
want = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(2)
sys.exit(0 if any(isinstance(b, dict) and b.get('id') == want for b in data) else 1)
" "$1"
    }

    # --- 1: while the blocker is OPEN the dependent is correctly withheld ---
    if ready_has "$DEPENDENT_ID"; then
      fail "bd: dependent is withheld from 'bd ready' while its blocker is OPEN" \
           "$DEPENDENT_ID appeared in bd ready before the blocker closed"
    else
      pass "bd: dependent is withheld from 'bd ready' while its blocker is OPEN"
    fi

    # --- 2: closing the blocker RELEASES it — the branch is still unmerged ---
    bd -C "$REPO" close "$BLOCKER_ID" --sandbox \
      --reason "[no-test] closed before merge — reproducing the d5iku window" >/dev/null 2>&1
    if ready_has "$DEPENDENT_ID"; then
      pass "bd: closing the blocker releases the dependent into 'bd ready' WHILE its branch is unmerged (the defect, reproduced)"
    else
      fail "bd: closing the blocker releases the dependent into 'bd ready' WHILE its branch is unmerged (the defect, reproduced)" \
           "$DEPENDENT_ID did not appear in bd ready after the close — if bd became merge-aware, this suite needs rewriting, not silencing"
    fi

    # --- 3: the tooling WARNS across that window ---------------------------
    OUT=$("$DEP_CHECK" --repo "$WORK" --bd-dir "$REPO" --integration-branch "$INT_BRANCH" "$DEPENDENT_ID" 2>&1)
    RC=$?
    if [ "$RC" -eq 3 ] \
       && echo "$OUT" | grep -q 'UNMERGED-BLOCKER WARNING' \
       && echo "$OUT" | grep -q "$BLOCKER_BRANCH" \
       && echo "$OUT" | grep -q "$BLOCKER_ID"; then
      pass "sable-dep-check WARNS (exit 3) naming the blocker and its unmerged branch"
    else
      fail "sable-dep-check WARNS (exit 3) naming the blocker and its unmerged branch" \
           "rc=$RC output: ${OUT:-<empty>}"
    fi

    # --- 4: --ready finds it without being told which bead to look at ------
    OUT_READY=$("$DEP_CHECK" --repo "$WORK" --bd-dir "$REPO" --integration-branch "$INT_BRANCH" --ready 2>&1)
    if echo "$OUT_READY" | grep -q "$DEPENDENT_ID"; then
      pass "sable-dep-check --ready surfaces the falsely-released bead from the ready pool"
    else
      fail "sable-dep-check --ready surfaces the falsely-released bead from the ready pool" \
           "output: ${OUT_READY:-<empty>}"
    fi

    # --- 5: REAL merge, then the warning must STOP -------------------------
    # Nothing about the bead graph changes here — only the tree. If the check
    # still warned, it would be reading status, not merge state.
    git -C "$WORK" merge -q --no-edit "origin/$BLOCKER_BRANCH"
    git -C "$WORK" push -q origin "$INT_BRANCH"
    git -C "$WORK" fetch -q origin

    OUT_AFTER=$("$DEP_CHECK" --repo "$WORK" --bd-dir "$REPO" --integration-branch "$INT_BRANCH" "$DEPENDENT_ID" 2>&1)
    RC_AFTER=$?
    if [ "$RC_AFTER" -eq 0 ] && ! echo "$OUT_AFTER" | grep -q 'UNMERGED-BLOCKER WARNING'; then
      pass "after the REAL merge the same bead graph is clean (exit 0, no warning)"
    else
      fail "after the REAL merge the same bead graph is clean (exit 0, no warning)" \
           "rc=$RC_AFTER output: ${OUT_AFTER:-<empty>}"
    fi

    # --- 6: branch deleted post-merge stays clean --------------------------
    # This fleet prunes worker branches after they land. An absent ref must not
    # resurrect the warning, or every merged blocker would warn forever.
    git -C "$WORK" push -q origin --delete "$BLOCKER_BRANCH"
    git -C "$WORK" fetch -q --prune origin
    OUT_PRUNED=$("$DEP_CHECK" --repo "$WORK" --bd-dir "$REPO" --integration-branch "$INT_BRANCH" "$DEPENDENT_ID" 2>&1)
    RC_PRUNED=$?
    if [ "$RC_PRUNED" -eq 0 ] && ! echo "$OUT_PRUNED" | grep -q 'UNMERGED-BLOCKER WARNING'; then
      pass "blocker branch pruned after merge → still clean (no false-block)"
    else
      fail "blocker branch pruned after merge → still clean (no false-block)" \
           "rc=$RC_PRUNED output: ${OUT_PRUNED:-<empty>}"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Git-only half — runs even without bd, so the clean-room still exercises the
# ancestry engine (the part that genuinely cannot be stubbed) for real.
# ---------------------------------------------------------------------------
GITONLY="$FIX/gitonly"
git init -q "$GITONLY"
git -C "$GITONLY" config user.email "test@example.invalid"
git -C "$GITONLY" config user.name "SABLE Test"
git -C "$GITONLY" config sable.integrationBranch "$INT_BRANCH"
git -C "$GITONLY" checkout -q -b "$INT_BRANCH"
echo a > "$GITONLY/a.txt"
git -C "$GITONLY" add a.txt
git -C "$GITONLY" commit -qm base
GO_BASE=$(git -C "$GITONLY" rev-parse HEAD)
echo b > "$GITONLY/b.txt"
git -C "$GITONLY" add b.txt
git -C "$GITONLY" commit -qm work
GO_TIP=$(git -C "$GITONLY" rev-parse HEAD)
git -C "$GITONLY" update-ref refs/remotes/origin/wk-go "$GO_TIP"

git -C "$GITONLY" update-ref "refs/remotes/origin/$INT_BRANCH" "$GO_BASE"
if [ "$("$DEP_CHECK" --repo "$GITONLY" --integration-branch "$INT_BRANCH" \
        --format=json X 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["integration_branch"])')" = "$INT_BRANCH" ]; then
  pass "git-only: the configured integration branch is what ancestry is judged against"
else
  fail "git-only: the configured integration branch is what ancestry is judged against"
fi

# resolve_base_ref prefers the PUBLISHED integration ref; with only a local
# branch it must still resolve rather than degrade to "unresolvable".
git -C "$GITONLY" update-ref -d "refs/remotes/origin/$INT_BRANCH"
BASE_LOCAL=$(python3 - "$DEP_CHECK" "$GITONLY" "$INT_BRANCH" <<'PY'
import importlib.util, sys
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("sdc", sys.argv[1])
spec = importlib.util.spec_from_loader("sdc", loader)
m = importlib.util.module_from_spec(spec)
loader.exec_module(m)
print(m.resolve_base_ref(sys.argv[2], "origin", sys.argv[3]))
PY
)
if [ "$BASE_LOCAL" = "refs/heads/$INT_BRANCH" ]; then
  pass "git-only: an UNPUBLISHED integration branch falls back to the local ref (real repo, real refs)"
else
  fail "git-only: an UNPUBLISHED integration branch falls back to the local ref (real repo, real refs)" \
       "got '$BASE_LOCAL'"
fi

# ---------------------------------------------------------------------------
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: $SKIP"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
