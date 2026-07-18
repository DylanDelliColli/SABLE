#!/usr/bin/env bash
# test-worktree-isolation.sh — reproduction + characterization harness for
# SABLE-nhrb (worktree isolation violation; SABLE-53if family).
#
# WHAT SABLE-nhrb OBSERVED
# ------------------------
# During a parallel dispatch, hooks/tdd-gate.sh + hooks/test/test-tdd-gate.sh
# appeared DIRTY in the wk-reconcile-bd-path (SABLE-7oj5) worktree — files
# 7oj5 never touches. The stray diff was byte-for-byte SABLE-p84b's work
# (branch wk-tdd-gate-notest-desc). p84b's worker wrote its edits into a
# SIBLING worker's worktree. The contamination silently BLOCKED a rebase in
# 7oj5 ("cannot rebase: You have unstaged changes"), so the cost is real:
# cross-lane corruption that can get a sibling's diff committed onto the WRONG
# branch, invisible until a later git op trips over it.
#
# THE MECHANISM (reproduced by this harness)
# ------------------------------------------
# Per-worker worktrees are the load-bearing isolation primitive behind the
# whole "bd worktree create <name> per worker" dispatch model. That isolation
# is REAL at the filesystem layer — a relative-path Edit/Write in worktree A
# can never reach worktree B's checkout (Test 1). But git worktrees SHARE the
# common git dir, and `refs/stash` lives there, NOT per-worktree. So the stash
# STACK is global: `git stash` in worktree A and `git stash pop` in worktree B
# lands A's diff into B's checkout — with NO absolute path, NO cd-out, and NO
# symlink involved (Test 3). A worker doing ordinary rebase hygiene
# (`git stash` → rebase → `git stash pop`) pops whatever is on top of the
# SHARED stack, which may be a concurrent sibling's entry. That is the
# SABLE-nhrb reproduction and the SABLE-53if root cause ("refs/stash is shared
# across worktrees").
#
# The only way a DIRECT write crosses worktrees is by explicitly naming the
# sibling's absolute path (Test 2) — i.e. the "absolute-path resolution" /
# "worker cd-out" hypotheses require the worker to resolve a path OUTSIDE its
# own tree. The stash mechanism needs none of that, which is why it is the
# compelling explanation for a contamination the writing worker never intended.
#
# STABILITY
# ---------
# Tests 1-3 assert PROPERTIES OF GIT + the filesystem, not of SABLE code, so
# they stay GREEN regardless of the eventual fix. The fix for nhrb is worker
# DISCIPLINE (never bare `git stash`/`git stash pop` in the warm-pane rebase
# flow) — filed as a separate bead — NOT a change to git's shared-stash
# behavior, which this harness pins down and documents.
#
# Run:  bash hooks/test/test-worktree-isolation.sh

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# Anchor CWD + identity to a throwaway sandbox (never the real repo). Our own
# fixture below is fully self-contained via absolute paths + `git -C`, so this
# is defense-in-depth consistent with the isolation-test family (SABLE-0ssz.2).
source "$HERE/lib-git-sandbox.sh"

WORKROOT="$(mktemp -d "${TMPDIR:-/tmp}/nhrb-wt-iso.XXXXXX")"
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

# ---------------------------------------------------------------------------
# Fixture: a main checkout with a shared-tracked file, plus two SIBLING
# worktrees — the exact shape `bd worktree create wk-<name>` produces per
# worker (siblings of the repo root, sharing the common git dir).
# ---------------------------------------------------------------------------
MAIN="$WORKROOT/main"
git init -q "$MAIN"
mkdir -p "$MAIN/hooks"
printf 'ORIGINAL tdd-gate\n' > "$MAIN/hooks/tdd-gate.sh"
git -C "$MAIN" add -A
git -C "$MAIN" commit -qm "init: shared-tracked hooks/tdd-gate.sh"

WK_A="$WORKROOT/wk-A"   # stand-in for p84b's worktree (the WRITER)
WK_B="$WORKROOT/wk-B"   # stand-in for 7oj5's worktree (the VICTIM)
git -C "$MAIN" worktree add -q "$WK_A" -b wk-A
git -C "$MAIN" worktree add -q "$WK_B" -b wk-B

# porcelain_count <worktree> — number of dirty entries in a worktree.
porcelain_count() { git -C "$1" status --porcelain 2>/dev/null | grep -c . || true; }

# ==========================================================================
# Test 1 — filesystem isolation HOLDS: a relative-path edit in A never
# dirties sibling B. This is the guarantee the dispatch model relies on.
# ==========================================================================
printf 'A-EDIT relative path\n' > "$WK_A/hooks/tdd-gate.sh"
b_dirty="$(porcelain_count "$WK_B")"
if [ "$b_dirty" -eq 0 ]; then
  pass "filesystem isolation: a relative-path edit in worktree A leaves sibling B clean"
else
  fail "filesystem isolation: a relative-path edit in worktree A leaves sibling B clean" \
    "sibling B shows $b_dirty dirty ent(y/ies): $(git -C "$WK_B" status --porcelain)"
fi
git -C "$WK_A" checkout -q -- hooks/tdd-gate.sh

# ==========================================================================
# Test 2 — a DIRECT cross-worktree write is only possible by explicitly
# naming the sibling's ABSOLUTE path (the absolute-path / cd-out hypotheses).
# Confirms the boundary: escape requires a path OUTSIDE the worker's own tree,
# which the shared-stash mechanism (Test 3) does NOT need.
# ==========================================================================
( cd "$WK_A" && printf 'foreign write\n' > "$WK_B/hooks/tdd-gate.sh" )
b_dirty="$(porcelain_count "$WK_B")"
if [ "$b_dirty" -ge 1 ]; then
  pass "boundary: a direct cross-write dirties B ONLY when B's absolute path is named (cd-out/abs-path class)"
else
  fail "boundary: a direct cross-write dirties B ONLY when B's absolute path is named" \
    "expected B dirty after an absolute-path write, got $b_dirty"
fi
git -C "$WK_B" checkout -q -- hooks/tdd-gate.sh

# ==========================================================================
# Test 3 — SABLE-nhrb REPRODUCTION: the shared refs/stash stack crosses
# worktrees. A stashes an edit to the shared-tracked file; B pops the shared
# stack and A's diff materializes in B — no absolute path, no cd-out, no
# symlink. This is the mechanism behind the observed p84b->7oj5 contamination.
# ==========================================================================
printf 'A-STASH-EDIT tdd-gate\n' > "$WK_A/hooks/tdd-gate.sh"
git -C "$WK_A" stash -q

a_dirty="$(porcelain_count "$WK_A")"
shared_entries="$(git -C "$WK_B" stash list 2>/dev/null | grep -c . || true)"

# B does ordinary rebase hygiene (`git stash pop`) — and gets A's entry off
# the SHARED stack.
pop_out="$(git -C "$WK_B" stash pop -q 2>&1)"
b_content="$(cat "$WK_B/hooks/tdd-gate.sh" 2>/dev/null || true)"

if [ "$a_dirty" -eq 0 ] && [ "$shared_entries" -ge 1 ] && [ "$b_content" = "A-STASH-EDIT tdd-gate" ]; then
  pass "SABLE-nhrb repro: shared refs/stash crosses worktrees — A's stashed edit lands in sibling B via a bare 'git stash pop'"
else
  fail "SABLE-nhrb repro: shared refs/stash crosses worktrees" \
    "a_dirty=$a_dirty shared_stash_entries=$shared_entries B_content='$b_content' pop='$pop_out' (expected A clean, stash visible from B, and A's edit in B)"
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
