#!/usr/bin/env bash
# test-sable-contained.sh — INTEGRATION coverage for bin/sable-contained
# (SABLE-gdp05).
#
# THE DEFECT, REPRODUCED END TO END
# ----------------------------------
# SABLE-4344d prescribes `git merge-base --is-ancestor <sha> origin/<int>` as
# THE containment check between serialized dispatches. Both argument orders
# are valid git and both exit 0/1 — nothing warns on a swap. Live incident,
# 2026-07-21: optimus ran it inverted, read a merged branch as lost work, and
# began an unnecessary recovery.
#
# This suite builds a REAL git repo, a REAL merge, and a REAL unmerged branch
# — no stubbing of the ancestry engine, which is the entire question — and
# asserts:
#   1. CONTAINED / NOT-CONTAINED verdicts against real merged/unmerged shas.
#   2. NEGATIVE CONTROL (both inversion directions): the RAW inverted
#      merge-base call gives the WRONG answer in this exact fixture, while
#      the wrapper still reports correctly — proving the fixture actually
#      reproduces the bug, not just a story about it.
#   3. The dual-method cross-check FIRES on a seeded disagreement, via a
#      fault-injecting git stub (SABLE_CONTAINED_GIT seam) — real git can
#      never disagree with itself, so a seeded fault is the only way to
#      prove the cross-check leg runs at all.
#
# Run with:
#   bash hooks/test/test-sable-contained.sh

set -uo pipefail

# Resolve absolute paths BEFORE the sandbox preamble cds away (SABLE-0ssz.2).
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$TESTDIR/../.." && pwd)"
CONTAINED="$REPO/bin/sable-contained"

# Env-neutralize real-repo git escapes for the suite duration. Every git op
# below names its own fixture repo with -C; this is defence in depth.
# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() {
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"
  echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"
}

if [ ! -x "$CONTAINED" ]; then
  echo "FAIL: sable-contained not executable at $CONTAINED"
  exit 2
fi

FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT

WORK="$FIX/work"
INT_BRANCH="tmux-only"

git init -q "$WORK"
git -C "$WORK" config user.email "test@example.invalid"
git -C "$WORK" config user.name "SABLE Test"
# The tool must judge ancestry against the repo's OWN configured integration
# branch, exactly as a real SABLE checkout does.
git -C "$WORK" config sable.integrationBranch "$INT_BRANCH"

git -C "$WORK" checkout -q -b "$INT_BRANCH"
echo "base layout" > "$WORK/base.txt"
git -C "$WORK" add base.txt
git -C "$WORK" commit -qm "base"
BASE_SHA=$(git -C "$WORK" rev-parse HEAD)

# A worker branch that gets MERGED back into the integration branch.
git -C "$WORK" checkout -q -b wk-merged
echo "merged work" > "$WORK/merged.txt"
git -C "$WORK" add merged.txt
git -C "$WORK" commit -qm "merged work"
MERGED_SHA=$(git -C "$WORK" rev-parse HEAD)
git -C "$WORK" checkout -q "$INT_BRANCH"
git -C "$WORK" merge -q --no-ff --no-edit wk-merged

# The integration branch keeps moving AFTER the merge — this is the shape of
# the false-ALARM direction: an old, already-merged sha, with the integration
# tip advanced past it.
echo "post-merge advance" > "$WORK/advance.txt"
git -C "$WORK" add advance.txt
git -C "$WORK" commit -qm "post-merge advance on the integration branch"

# Publish a remote-tracking ref for the integration branch (no real network
# remote needed — sable-contained only reads refs).
INT_TIP=$(git -C "$WORK" rev-parse "$INT_BRANCH")
git -C "$WORK" update-ref "refs/remotes/origin/$INT_BRANCH" "$INT_TIP"

# A worker branch forked from the CURRENT (post-advance) integration tip that
# never merges back — the shape of the false-GREEN direction (SABLE-5lli
# class): the integration tip trivially IS an ancestor of a branch just
# forked from it.
git -C "$WORK" checkout -q -b wk-unmerged
echo "unmerged work" > "$WORK/unmerged.txt"
git -C "$WORK" add unmerged.txt
git -C "$WORK" commit -qm "unmerged work"
UNMERGED_SHA=$(git -C "$WORK" rev-parse HEAD)
git -C "$WORK" checkout -q "$INT_BRANCH"

# ---------------------------------------------------------------------------
# 1. Basic verdicts, real repo
# ---------------------------------------------------------------------------
OUT_MERGED=$("$CONTAINED" --repo "$WORK" "$MERGED_SHA" 2>&1)
RC_MERGED=$?
if [ "$RC_MERGED" -eq 0 ] && echo "$OUT_MERGED" | grep -q '^CONTAINED:'; then
  pass "merged sha reports CONTAINED (exit 0)"
else
  fail "merged sha reports CONTAINED (exit 0)" "rc=$RC_MERGED output: $OUT_MERGED"
fi

OUT_UNMERGED=$("$CONTAINED" --repo "$WORK" "$UNMERGED_SHA" 2>&1)
RC_UNMERGED=$?
if [ "$RC_UNMERGED" -eq 1 ] && echo "$OUT_UNMERGED" | grep -q '^NOT-CONTAINED:'; then
  pass "unmerged sha reports NOT-CONTAINED (exit 1)"
else
  fail "unmerged sha reports NOT-CONTAINED (exit 1)" "rc=$RC_UNMERGED output: $OUT_UNMERGED"
fi

OUT_BASE=$("$CONTAINED" --repo "$WORK" "$BASE_SHA" 2>&1)
RC_BASE=$?
if [ "$RC_BASE" -eq 0 ] && echo "$OUT_BASE" | grep -q '^CONTAINED:'; then
  pass "base sha (ancestor of everything) reports CONTAINED (exit 0)"
else
  fail "base sha (ancestor of everything) reports CONTAINED (exit 0)" \
       "rc=$RC_BASE output: $OUT_BASE"
fi

# ---------------------------------------------------------------------------
# 2. NEGATIVE CONTROL — the raw inverted command is actually WRONG here
# ---------------------------------------------------------------------------
# False-ALARM direction: MERGED_SHA is old; the integration tip has since
# advanced past it, so the inverted call ("is the CURRENT tip an ancestor of
# the OLD merged sha") must say NO even though MERGED_SHA is genuinely merged.
git -C "$WORK" merge-base --is-ancestor "origin/$INT_BRANCH" "$MERGED_SHA"
INVERTED_ALARM_RC=$?
if [ "$INVERTED_ALARM_RC" -eq 1 ]; then
  pass "negative control: raw INVERTED merge-base wrongly says NOT-ancestor for a merged sha (false alarm reproduced)"
else
  fail "negative control: raw INVERTED merge-base wrongly says NOT-ancestor for a merged sha (false alarm reproduced)" \
       "rc=$INVERTED_ALARM_RC — fixture does not reproduce the false-alarm shape"
fi
if [ "$RC_MERGED" -eq 0 ]; then
  pass "sable-contained still reports CONTAINED for that same sha despite the inverted false alarm"
else
  fail "sable-contained still reports CONTAINED for that same sha despite the inverted false alarm" \
       "rc=$RC_MERGED output: $OUT_MERGED"
fi

# False-GREEN direction: UNMERGED_SHA was forked from the CURRENT integration
# tip, so the inverted call ("is the tip an ancestor of the unmerged branch")
# trivially says YES — a spurious CONTAINED for work that never merged.
git -C "$WORK" merge-base --is-ancestor "origin/$INT_BRANCH" "$UNMERGED_SHA"
INVERTED_GREEN_RC=$?
if [ "$INVERTED_GREEN_RC" -eq 0 ]; then
  pass "negative control: raw INVERTED merge-base wrongly says CONTAINED for an unmerged sha (false green reproduced — the SABLE-5lli class)"
else
  fail "negative control: raw INVERTED merge-base wrongly says CONTAINED for an unmerged sha (false green reproduced — the SABLE-5lli class)" \
       "rc=$INVERTED_GREEN_RC — fixture does not reproduce the false-green shape"
fi
if [ "$RC_UNMERGED" -eq 1 ]; then
  pass "sable-contained still reports NOT-CONTAINED for that same sha despite the inverted false green"
else
  fail "sable-contained still reports NOT-CONTAINED for that same sha despite the inverted false green" \
       "rc=$RC_UNMERGED output: $OUT_UNMERGED"
fi

# ---------------------------------------------------------------------------
# 3. Dual-method cross-check fires on a SEEDED disagreement
# ---------------------------------------------------------------------------
# Real git can never make `merge-base --is-ancestor` and `git log <a>..<b>`
# disagree — they answer the same reachability question through different
# plumbing. So the only way to exercise the cross-check leg is a fault
# injection: a stub `git` that forwards everything to the real binary EXCEPT
# the one merge-base call this test targets, where it deliberately returns
# the WRONG exit code.
STUB="$FIX/stub-git"
REAL_GIT="$(command -v git)"
{
  echo '#!/usr/bin/env bash'
  echo "REAL_GIT='$REAL_GIT'"
  echo "SEED_SHA='$MERGED_SHA'"
  cat <<'STUBEOF'
if [ "$1" = "merge-base" ] && [ "$2" = "--is-ancestor" ] && [ "$3" = "$SEED_SHA" ]; then
  # MERGED_SHA really IS an ancestor (rc should be 0) — lie and say it is not,
  # so method_a (merge-base) disagrees with method_b (log-range, untouched).
  exit 1
fi
exec "$REAL_GIT" "$@"
STUBEOF
} > "$STUB"
chmod +x "$STUB"

OUT_DISAGREE=$(SABLE_CONTAINED_GIT="$STUB" "$CONTAINED" --repo "$WORK" "$MERGED_SHA" 2>&1)
RC_DISAGREE=$?
if [ "$RC_DISAGREE" -eq 3 ] && echo "$OUT_DISAGREE" | grep -q 'DISAGREEMENT' \
   && echo "$OUT_DISAGREE" | grep -q "$MERGED_SHA"; then
  pass "seeded method disagreement is caught loudly (exit 3, DISAGREEMENT, names the sha) instead of silently picking a side"
else
  fail "seeded method disagreement is caught loudly (exit 3, DISAGREEMENT, names the sha) instead of silently picking a side" \
       "rc=$RC_DISAGREE output: $OUT_DISAGREE"
fi

# Complement: with the real (unfaked) git, the same sha is clean — proving
# the disagreement above came from the injected fault, not from the fixture.
OUT_NO_FAULT=$("$CONTAINED" --repo "$WORK" "$MERGED_SHA" 2>&1)
RC_NO_FAULT=$?
if [ "$RC_NO_FAULT" -eq 0 ] && ! echo "$OUT_NO_FAULT" | grep -q 'DISAGREEMENT'; then
  pass "complement: without the fault injection the same sha is clean (exit 0, no disagreement)"
else
  fail "complement: without the fault injection the same sha is clean (exit 0, no disagreement)" \
       "rc=$RC_NO_FAULT output: $OUT_NO_FAULT"
fi

# ---------------------------------------------------------------------------
# 4. JSON format carries the same verdict machine-readably
# ---------------------------------------------------------------------------
JSON_OUT=$("$CONTAINED" --repo "$WORK" --format=json "$MERGED_SHA" 2>&1)
JSON_VERDICT=$(printf '%s' "$JSON_OUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('verdict', ''))
except Exception:
    print('INVALID-JSON')
")
if [ "$JSON_VERDICT" = "contained" ]; then
  pass "--format=json emits valid JSON carrying the same verdict"
else
  fail "--format=json emits valid JSON carrying the same verdict" "parsed='$JSON_VERDICT' raw='$JSON_OUT'"
fi

# ---------------------------------------------------------------------------
# 5. Unresolvable integration branch is COULD-NOT-ASSESS, not a guess
# ---------------------------------------------------------------------------
OUT_UNRESOLVED=$("$CONTAINED" --repo "$WORK" --integration-branch no-such-branch "$MERGED_SHA" 2>&1)
RC_UNRESOLVED=$?
if [ "$RC_UNRESOLVED" -eq 4 ] && echo "$OUT_UNRESOLVED" | grep -q 'COULD NOT ASSESS'; then
  pass "an unresolvable integration branch reports COULD NOT ASSESS (exit 4), never a guessed verdict"
else
  fail "an unresolvable integration branch reports COULD NOT ASSESS (exit 4), never a guessed verdict" \
       "rc=$RC_UNRESOLVED output: $OUT_UNRESOLVED"
fi

# ---------------------------------------------------------------------------
# 6. PATH MODE (SABLE-4snb4) — presence of a path in a ref's tree
# ---------------------------------------------------------------------------
# The sibling footgun. `git ls-tree <ref> <path>` EXITS 0 FOR AN ABSENT PATH
# (it just prints nothing), so `ls-tree ... && echo PRESENT` false-POSITIVES —
# the hold-RELEASING direction. Live 2026-07-22: it reported a jd5fj.13 file
# as ON SPINE while .13 was unmerged, nearly releasing the cmar4.5 hold onto a
# base lacking it.
#
# The fixture already IS the two-commit history the case needs: advance.txt is
# absent at BASE_SHA and present at the integration tip.

# NEGATIVE CONTROL FIRST — prove the bad idiom really is broken in THIS repo,
# at process level, before asserting the wrapper resists it.
BAD_IDIOM_OUT=$(git -C "$WORK" ls-tree --name-only "$BASE_SHA" advance.txt)
BAD_IDIOM_RC=$?
if [ "$BAD_IDIOM_RC" -eq 0 ] && [ -z "$BAD_IDIOM_OUT" ]; then
  pass "negative control: raw 'git ls-tree' exits 0 with EMPTY output for an ABSENT path (the false-positive idiom reproduced)"
else
  fail "negative control: raw 'git ls-tree' exits 0 with EMPTY output for an ABSENT path (the false-positive idiom reproduced)" \
       "rc=$BAD_IDIOM_RC output='$BAD_IDIOM_OUT' — fixture does not reproduce the shape"
fi

OUT_PATH_ABSENT=$("$CONTAINED" --repo "$WORK" --path advance.txt --ref "$BASE_SHA" 2>&1)
RC_PATH_ABSENT=$?
if [ "$RC_PATH_ABSENT" -eq 1 ] && echo "$OUT_PATH_ABSENT" | grep -q '^NOT-CONTAINED:'; then
  pass "path mode reports NOT-CONTAINED (exit 1) for a path absent from the ref, where the ls-tree idiom said PRESENT"
else
  fail "path mode reports NOT-CONTAINED (exit 1) for a path absent from the ref, where the ls-tree idiom said PRESENT" \
       "rc=$RC_PATH_ABSENT output: $OUT_PATH_ABSENT"
fi

OUT_PATH_PRESENT=$("$CONTAINED" --repo "$WORK" --path advance.txt --ref "origin/$INT_BRANCH" 2>&1)
RC_PATH_PRESENT=$?
if [ "$RC_PATH_PRESENT" -eq 0 ] && echo "$OUT_PATH_PRESENT" | grep -q '^CONTAINED:'; then
  pass "path mode reports CONTAINED (exit 0) for the same path in the later ref (the negative control's complement)"
else
  fail "path mode reports CONTAINED (exit 0) for the same path in the later ref (the negative control's complement)" \
       "rc=$RC_PATH_PRESENT output: $OUT_PATH_PRESENT"
fi

# --ref defaults to the integration ref: "is this file on the spine yet" is
# the live question and must not require naming the ref by hand.
OUT_PATH_DEFAULT=$("$CONTAINED" --repo "$WORK" --path advance.txt 2>&1)
RC_PATH_DEFAULT=$?
if [ "$RC_PATH_DEFAULT" -eq 0 ] && echo "$OUT_PATH_DEFAULT" | grep -q "origin/$INT_BRANCH"; then
  pass "path mode defaults --ref to the resolved integration ref"
else
  fail "path mode defaults --ref to the resolved integration ref" \
       "rc=$RC_PATH_DEFAULT output: $OUT_PATH_DEFAULT"
fi

# CROSS-MODE COHERENCE — the property probe and the sha probe must answer the
# same question the same way. unmerged.txt exists ONLY on wk-unmerged, whose
# sha commit mode already reported NOT-CONTAINED above; path mode must agree.
OUT_XMODE=$("$CONTAINED" --repo "$WORK" --path unmerged.txt 2>&1)
RC_XMODE=$?
if [ "$RC_XMODE" -eq 1 ] && [ "$RC_UNMERGED" -eq 1 ]; then
  pass "cross-mode coherence: the unmerged branch's FILE and its SHA both report NOT-CONTAINED (property probe agrees with the sha probe)"
else
  fail "cross-mode coherence: the unmerged branch's FILE and its SHA both report NOT-CONTAINED (property probe agrees with the sha probe)" \
       "path rc=$RC_XMODE (output: $OUT_XMODE) sha rc=$RC_UNMERGED"
fi

# A TYPO'D REF MUST NOT LOOK LIKE ABSENCE. `git cat-file -e <ref>:<path>`
# returns the same non-zero for "no such ref" as for "no such path"; reporting
# NOT-CONTAINED there would be a fabricated verdict, which is the failure this
# whole tool exists to refuse.
OUT_BADREF=$("$CONTAINED" --repo "$WORK" --path advance.txt --ref no-such-ref 2>&1)
RC_BADREF=$?
if [ "$RC_BADREF" -eq 4 ] && echo "$OUT_BADREF" | grep -q 'COULD NOT ASSESS'; then
  pass "path mode against an unresolvable ref reports COULD NOT ASSESS (exit 4), not a confident NOT-CONTAINED"
else
  fail "path mode against an unresolvable ref reports COULD NOT ASSESS (exit 4), not a confident NOT-CONTAINED" \
       "rc=$RC_BADREF output: $OUT_BADREF"
fi

# Mode selection fails closed rather than picking a question for the caller.
OUT_BOTHMODES=$("$CONTAINED" --repo "$WORK" "$MERGED_SHA" --path advance.txt 2>&1)
RC_BOTHMODES=$?
if [ "$RC_BOTHMODES" -eq 2 ]; then
  pass "asking both containment questions at once is a usage error (exit 2), not a silent precedence rule"
else
  fail "asking both containment questions at once is a usage error (exit 2), not a silent precedence rule" \
       "rc=$RC_BOTHMODES output: $OUT_BOTHMODES"
fi

JSON_PATH_OUT=$("$CONTAINED" --repo "$WORK" --path advance.txt --ref "$BASE_SHA" --format=json 2>&1)
JSON_PATH_VERDICT=$(printf '%s' "$JSON_PATH_OUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('mode', '') + ':' + d.get('verdict', ''))
except Exception:
    print('INVALID-JSON')
")
if [ "$JSON_PATH_VERDICT" = "path:not-contained" ]; then
  pass "path mode --format=json emits valid JSON carrying the mode and the same verdict"
else
  fail "path mode --format=json emits valid JSON carrying the mode and the same verdict" \
       "parsed='$JSON_PATH_VERDICT' raw='$JSON_PATH_OUT'"
fi

# ---------------------------------------------------------------------------
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
