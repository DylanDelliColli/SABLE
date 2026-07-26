#!/usr/bin/env bash
# test-reconcile-rebase-copy.sh — SABLE-z7gue content-containment integration
#
# Stranded-branch reconciliation (bin/sable-reconcile-handoffs) used to key
# STRANDED entirely on TIP containment (`git merge-base --is-ancestor`). A
# rebase changes every commit sha, so a branch that was rebased and landed
# at the spine under a DIFFERENT sha reads as "not an ancestor" IDENTICALLY
# to a genuinely stranded branch — and 'ask both lanes' (the SABLE-xw32f
# remedy for the ordinary ambiguous case) cannot separate the two either,
# because every lane truthfully answers 'not mine' when the work is already
# on the spine, in nobody's hand.
#
# This suite exercises the fix (branch_content_contained, `git cherry
# <upstream> <head>` patch-id equivalence) against a REAL temporary git
# repo — real branch, real commits, real push, real ancestry/patch-id
# comparison — with the REAL reconciler entry point (bin/sable-
# reconcile-handoffs run as a subprocess, not reimplemented here).
#
# Two legs, same repo:
#   Leg 1  a branch pushed and unmerged BY TIP, but its content already
#          landed at the spine under a different commit — must file NO
#          stranded bead.
#   Leg 2  negative control: a genuinely unlanded branch (no patch-id
#          equivalent anywhere at the spine) — must still file one, so leg
#          1 cannot pass by having silently disabled the detector.
#
# Stub bd (same technique as hooks/test/test-edit-write-claim-reconciler.sh):
# this suite's SUBJECT is the git-level patch-id discriminator, not bd's
# bead-resolution machinery — that end-to-end path (real git AND real bd,
# no mocks on either) is already covered by
# bin/test_sable_reconcile_handoffs_integration.py::
# test_reconcile_against_real_git_and_real_bd. A stub here keeps this suite
# fast and free of bd/dolt fixture flakiness while every ancestry/patch-id
# call still runs against REAL git — the actual thing under test. Every
# work bead resolves "closed" (real, done merge work); the for-chuck corpus
# always reads empty (predicate 3 never suppresses in this fixture).
#
# NOTE on the dispatch note's literal "delete the original ref" step: this
# reconciler's list_origin_wk_branches only iterates CURRENTLY-LIVE origin
# wk-* refs, so a branch whose ref was already deleted is excluded from
# classification before predicate 1 ever runs — for a reason that has
# nothing to do with content-containment. Deleting the ref would make leg 1
# pass VACUOUSLY (correct output, wrong reason). Both legs here instead keep
# the branch ref alive on origin throughout, which is what actually drives
# execution through the new branch_content_contained discriminator — the
# live class SABLE-4709h's own triage records ("1 REBASED AND LANDED under a
# different SHA" among sixteen real [RECONCILE] firings from THIS
# reconciler) actually came through.
#
# Run with:
#   bash hooks/test/test-reconcile-rebase-copy.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RECONCILER="$REPO/bin/sable-reconcile-handoffs"

if [ ! -f "$RECONCILER" ]; then
  echo "FAIL: reconciler not found at $RECONCILER"
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP: python3 not found on PATH"
  exit 0
fi
if ! command -v git >/dev/null 2>&1; then
  echo "SKIP: git not found on PATH"
  exit 0
fi

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

FIXTURE_DIR=$(mktemp -d)
trap 'rm -rf "$FIXTURE_DIR"' EXIT

ORIGIN="$FIXTURE_DIR/origin.git"
WORK="$FIXTURE_DIR/work"
BASE="trunk"

git init --bare -b "$BASE" "$ORIGIN" >/dev/null 2>&1
git clone "$ORIGIN" "$WORK" >/dev/null 2>&1
git -C "$WORK" config user.email t@t
git -C "$WORK" config user.name t
echo "integrationBranch=$BASE" > "$WORK/.sable"
echo base > "$WORK/README.md"
git -C "$WORK" add README.md .sable
git -C "$WORK" commit -q -m init
git -C "$WORK" push -q origin "$BASE"

# ---------------------------------------------------------------------------
# Stub bd — see header rationale.
# ---------------------------------------------------------------------------
STUB_DIR="$FIXTURE_DIR/bin"
mkdir -p "$STUB_DIR"
BD_CALL_LOG="$FIXTURE_DIR/bd-calls.log"
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
echo "BD_CALLED: $*" >> "$BD_CALL_LOG"
case "$1" in
  list)
    echo "[]"
    ;;
  show)
    echo '[{"id":"stub-bead","status":"closed"}]'
    ;;
  search)
    echo '[{"id":"stub-bead","status":"closed"}]'
    ;;
  *)
    exit 0
    ;;
esac
STUB
chmod +x "$STUB_DIR/bd"

run_reconciler() {
  : > "$BD_CALL_LOG"
  env BD_CALL_LOG="$BD_CALL_LOG" PATH="$STUB_DIR:$PATH" \
      SABLE_RC_TMUX="sable-reconcile-tests-hermetic-no-such-tmux-binary" \
      python3 "$RECONCILER" --repo "$WORK" --remote origin --age-min 0
}

land_same_content_on_trunk() {
  # land_same_content_on_trunk <feature-file> <content>
  local feat="$1" content="$2"
  git -C "$WORK" checkout -q "$BASE"
  printf '%s\n' "$content" > "$WORK/$feat"
  git -C "$WORK" add "$feat"
  git -C "$WORK" commit -q -m "worker feature (landed via squash/rebase onto trunk)"
  git -C "$WORK" push -q origin "$BASE"
}

# ---------------------------------------------------------------------------
# Leg 1: rebased-and-landed — must file NO stranded bead.
# ---------------------------------------------------------------------------
BRANCH1="wk-rebase-copy"
git -C "$WORK" checkout -q -b "$BRANCH1" "$BASE"
printf 'worker feature\n' > "$WORK/feature.txt"
git -C "$WORK" add feature.txt
git -C "$WORK" commit -q -m "worker feature"
git -C "$WORK" push -q origin "$BRANCH1"
git -C "$WORK" checkout -q "$BASE"

# The SAME patch, landed directly on trunk under a NEW commit/sha — the
# rebase-copy relation, without this fixture needing to run an actual
# `git rebase` (git cherry computes patch-id from each commit's diff against
# its own parent, so as long as BASE has not moved since the worker branch
# forked, this commit's patch is byte-identical to the worker branch's own).
land_same_content_on_trunk "feature.txt" "worker feature"

sleep 1  # guarantee push-age > 0 for the strict age_exceeds_threshold(> , 0min) check

OUT1=$(run_reconciler)
if echo "$OUT1" | grep -q "$BRANCH1: landed-under-different-sha"; then
  pass "z7gue leg 1: rebased-and-landed branch classifies landed-under-different-sha"
else
  fail "z7gue leg 1: rebased-and-landed branch classifies landed-under-different-sha" "$OUT1"
fi

if grep -q "BD_CALLED: create.*$BRANCH1" "$BD_CALL_LOG"; then
  fail "z7gue leg 1: rebased-and-landed branch files NO stranded bead" "$(cat "$BD_CALL_LOG")"
else
  pass "z7gue leg 1: rebased-and-landed branch files NO stranded bead"
fi

# ---------------------------------------------------------------------------
# Leg 2 (negative control, same repo): genuinely unlanded — must still fire,
# so leg 1 cannot pass by having silently disabled the detector.
# ---------------------------------------------------------------------------
BRANCH2="wk-still-stranded"
git -C "$WORK" checkout -q -b "$BRANCH2" "$BASE"
printf 'genuinely different feature, never landed\n' > "$WORK/feature2.txt"
git -C "$WORK" add feature2.txt
git -C "$WORK" commit -q -m "worker feature, never lands"
git -C "$WORK" push -q origin "$BRANCH2"
git -C "$WORK" checkout -q "$BASE"

sleep 1

OUT2=$(run_reconciler)
if echo "$OUT2" | grep -q "$BRANCH2: STRANDED"; then
  pass "z7gue leg 2 (negative control): genuinely unlanded branch still classifies STRANDED"
else
  fail "z7gue leg 2 (negative control): genuinely unlanded branch still classifies STRANDED" "$OUT2"
fi

if grep -q "BD_CALLED: create.*$BRANCH2" "$BD_CALL_LOG"; then
  pass "z7gue leg 2 (negative control): genuinely unlanded branch files a stranded bead"
else
  fail "z7gue leg 2 (negative control): genuinely unlanded branch files a stranded bead" "$(cat "$BD_CALL_LOG")"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
