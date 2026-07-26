#!/usr/bin/env bash
# test-provenance-guard.sh — Tests for the SABLE-rrn6r provenance guard inside
# hooks/multi-manager/pre-push-rebase-test.sh
#
# Context (SABLE-qhjq3): commit 2b1a15b landed directly on tmux-only from
# another fleet's client — single-parent, no ci-verify ref, no merge-preview
# provenance; CI ran only AFTER it was already on the branch instead of
# before. bin/sable-merge-gate's own promotion never reaches this hook (it
# pushes via a python subprocess, not a literal `git push` Bash command), so
# this guard exists for the OTHER path: an agent typing `git push` directly
# against the branch that IS the integration branch. It requires every commit
# such a push would newly introduce to already have a matching
# refs/heads/ci-verify/<bead>-<sha7> ref on origin — the ref the gate pushes
# BEFORE it ever promotes (sable_gate_preview_lib.py materialize_preview /
# kick_preview) — proving the exact object was previewed.
#
# This is mechanism 2 (local-only leg) of SABLE-rrn6r's two candidates: binds
# THIS fleet's hooked clients only. Mechanism 1 (a GitHub ruleset on
# tmux-only, which would also bind an unhooked client) is DEFERRED pending
# operator-brokered cross-fleet coordination — see SABLE-rrn6r's notes.
#
# Both polarities matter equally here: a guard that denies everything would
# pass a deny-only suite while breaking every legitimate push, so the ALLOW
# case (a push that DOES carry a matching ci-verify ref) is exercised
# explicitly, not just the DENY case.
#
# Run with:
#   bash hooks/test/test-provenance-guard.sh

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/multi-manager/pre-push-rebase-test.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-provenance-guard.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

PASS=0
FAIL=0
FAIL_NAMES=""

make_input() {
  # $1 = command, $2 = cwd
  python3 -c "
import json, sys
cmd, cwd = sys.argv[1], sys.argv[2]
print(json.dumps({'tool_input': {'command': cmd}, 'cwd': cwd}))
" "$1" "$2"
}

run_hook() {
  # $1 = env prefix, $2 = command, $3 = cwd
  local env_prefix="$1" cmd="$2" cwd="$3"
  local input
  input=$(make_input "$cmd" "$cwd")
  env -i PATH="$PATH" $env_prefix bash "$HOOK" <<< "$input" 2>/dev/null || echo "RUN_ERR:$?"
}

assert_allow() {
  local name="$1" env="$2" cmd="$3" cwd="$4"
  local out
  out=$(run_hook "$env" "$cmd" "$cwd")
  if [ -z "$out" ]; then
    PASS=$((PASS+1)); echo "PASS: $name"
  else
    FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $name (got: ${out:0:300})"
    echo "FAIL: $name"; echo "  Expected: empty (allow)"; echo "  Got:      ${out:0:300}"
  fi
}

assert_deny() {
  local name="$1" env="$2" cmd="$3" cwd="$4" expect="$5"
  local out
  out=$(run_hook "$env" "$cmd" "$cwd")
  if echo "$out" | grep -q '"permissionDecision": "deny"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1)); echo "PASS: $name"
  else
    FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $name (got: ${out:0:400})"
    echo "FAIL: $name"; echo "  Expected: deny containing '$expect'"; echo "  Got:      ${out:0:500}"
  fi
}

assert_context() {
  local name="$1" env="$2" cmd="$3" cwd="$4" expect="$5"
  local out
  out=$(run_hook "$env" "$cmd" "$cwd")
  if echo "$out" | grep -q '"additionalContext"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1)); echo "PASS: $name"
  else
    FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $name (got: ${out:0:400})"
    echo "FAIL: $name"; echo "  Expected: additionalContext containing '$expect'"; echo "  Got:      ${out:0:500}"
  fi
}

MGR_ENV="CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager"
COMMON_ENV="$MGR_ENV SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"

# Shared fixture builder: bare origin + working clone with main and tmux-only
# both published, tmux-only marked as the repo-local integration branch.
make_base_fixture() {
  local bare="$1" repo="$2"
  rm -rf "$bare" "$repo"
  git init -q --bare "$bare"
  git clone -q "$bare" "$repo" 2>/dev/null
  (
    cd "$repo" || exit 1
    git config user.email t@t; git config user.name t
    git checkout -q -B main
    echo base > f.txt; git add f.txt; git commit -q -m base
    git push -q origin main
    git checkout -q -b tmux-only
    echo t0 > f.txt; git add f.txt; git commit -q -m t0
    git push -q origin tmux-only
    git config sable.integrationBranch tmux-only
  )
}

# ===================================================================
# Test 1 (DENY): a direct commit landed on tmux-only with no matching
# refs/heads/ci-verify/<bead>-<sha7> ref on origin — the SABLE-qhjq3 shape
# (commit 2b1a15b: single-parent, no preview ref). Must be DENIED.
# ===================================================================
D1_BARE="$TMPROOT/d1-bare.git"
D1_REPO="$TMPROOT/d1-repo"
make_base_fixture "$D1_BARE" "$D1_REPO"
(
  cd "$D1_REPO" || exit 1
  echo bypass > f.txt; git add f.txt; git commit -q -m "direct-bypass-commit"
)
assert_deny "SABLE-rrn6r: direct push to tmux-only with no ci-verify ref is DENIED (provenance guard)" \
  "$COMMON_ENV" "git push origin tmux-only" "$D1_REPO" "provenance guard"
rm -rf "$D1_BARE" "$D1_REPO"

# ===================================================================
# Test 2 (ALLOW — the leg that matters most): the commit about to land
# already has a matching ci-verify/<bead>-<sha7> ref pushed to origin, i.e.
# it was previewed and CI-verified before promotion (the real gate shape).
# The push must be ALLOWED through to the remaining phases (static passes,
# tests skipped -> "phase skipped" context, not a deny).
# ===================================================================
D2_BARE="$TMPROOT/d2-bare.git"
D2_REPO="$TMPROOT/d2-repo"
make_base_fixture "$D2_BARE" "$D2_REPO"
(
  cd "$D2_REPO" || exit 1
  echo promoted > f.txt; git add f.txt; git commit -q -m "gate-promoted-commit"
  NEW_SHA=$(git rev-parse HEAD)
  git push -q origin "HEAD:refs/heads/ci-verify/SABLE-rrn6r-${NEW_SHA:0:7}"
)
assert_context "SABLE-rrn6r: push carrying a matching ci-verify ref is ALLOWED (gate fast-forward)" \
  "$COMMON_ENV" "git push origin tmux-only" "$D2_REPO" "phase skipped"
rm -rf "$D2_BARE" "$D2_REPO"

# ===================================================================
# Test 3 (DENY, precision): two new commits ahead of origin/tmux-only — the
# FIRST carries a matching ci-verify ref, the SECOND does not. Must still
# DENY, and the deny message must name the unproven commit's short SHA (not
# just "something is wrong somewhere") so the operator knows which object to
# re-preview.
# ===================================================================
D3_BARE="$TMPROOT/d3-bare.git"
D3_REPO="$TMPROOT/d3-repo"
make_base_fixture "$D3_BARE" "$D3_REPO"
(
  cd "$D3_REPO" || exit 1
  echo proven > f.txt; git add f.txt; git commit -q -m "proven-commit"
  PROVEN_SHA=$(git rev-parse HEAD)
  git push -q origin "HEAD:refs/heads/ci-verify/SABLE-rrn6r-${PROVEN_SHA:0:7}"
  echo unproven > f.txt; git add f.txt; git commit -q -m "unproven-commit"
)
UNPROVEN_SHORT_SHA=$(git -C "$D3_REPO" rev-parse --short=7 HEAD)
assert_deny "SABLE-rrn6r: one proven + one unproven commit ahead -> DENY naming the unproven SHA" \
  "$COMMON_ENV" "git push origin tmux-only" "$D3_REPO" "$UNPROVEN_SHORT_SHA"
rm -rf "$D3_BARE" "$D3_REPO"

# ===================================================================
# Test 4 (ALLOW, no-op scope check): first-time publish of the integration
# branch — origin/tmux-only does not exist yet, so there is no remote
# history to check provenance against. The guard must not fire here (mirrors
# the existing SKIP_REBASE unpublished carve-out); the push proceeds to the
# remaining phases same as any other push.
# ===================================================================
D4_BARE="$TMPROOT/d4-bare.git"
D4_REPO="$TMPROOT/d4-repo"
rm -rf "$D4_BARE" "$D4_REPO"
git init -q --bare "$D4_BARE"
git clone -q "$D4_BARE" "$D4_REPO" 2>/dev/null
(
  cd "$D4_REPO" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  echo base > f.txt; git add f.txt; git commit -q -m base
  git push -q origin main
  git checkout -q -b tmux-only
  echo first > f.txt; git add f.txt; git commit -q -m "first-publish"
  git config sable.integrationBranch tmux-only
)
assert_context "SABLE-rrn6r: unpublished tmux-only (first-time publish) -> guard does not fire" \
  "$COMMON_ENV" "git push origin tmux-only" "$D4_REPO" "phase skipped"
rm -rf "$D4_BARE" "$D4_REPO"

# ===================================================================
# Test 5 (ALLOW, no-op scope check): re-pushing with no NEW commits ahead of
# origin/tmux-only (e.g. a redundant push of an already-published tip) must
# not be blocked by the guard — there is nothing unproven to deny.
# ===================================================================
D5_BARE="$TMPROOT/d5-bare.git"
D5_REPO="$TMPROOT/d5-repo"
make_base_fixture "$D5_BARE" "$D5_REPO"
assert_context "SABLE-rrn6r: no new commits ahead of origin/tmux-only -> guard does not fire" \
  "$COMMON_ENV" "git push origin tmux-only" "$D5_REPO" "phase skipped"
rm -rf "$D5_BARE" "$D5_REPO"

# ---------- Summary ----------

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed tests:$FAIL_NAMES"
  exit 1
fi
exit 0
