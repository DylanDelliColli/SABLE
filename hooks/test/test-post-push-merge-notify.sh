#!/usr/bin/env bash
# test-post-push-merge-notify.sh — Tests for post-push-merge-notify.sh
# (SABLE-jpr, SABLE-61n, SABLE-0u1)
#
# Tests:
#   Matcher unit tests (SABLE-jpr / SABLE-0u1):
#     - 'git push' triggers bd create (for-chuck label)
#     - 'git -C /x push' triggers bd create
#     - 'git -c a=b push origin main' triggers bd create
#     - 'git pushd' does NOT trigger bd create
#     - 'bd update --description="git push"' (text mention) does NOT trigger
#   Base ref fallback (SABLE-61n):
#     - SABLE_BASE_BRANCH=origin/nonexistent: hook exits 0 AND bd create called
#   Integration:
#     - scratch git repo with real structure; origin/dev absent; bd stub counts creates
#
# Run with:
#   bash hooks/test/test-post-push-merge-notify.sh

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/multi-manager/post-push-merge-notify.sh"
LIB_DIR="$(cd "$(dirname "$0")/.." && pwd)/multi-manager"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
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

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

# Create a real scratch git repo with one commit and a 'main' branch so
# git -C <path> diff origin/main...HEAD works.
FIXTURE_REPO=$(mktemp -d)
BARE_ORIGIN=$(mktemp -d)
trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR"' EXIT

git init -q --bare "$BARE_ORIGIN"
git clone -q "$BARE_ORIGIN" "$FIXTURE_REPO"
cd "$FIXTURE_REPO"
git config user.email "test@test"
git config user.name "Test"
echo "x" > initial.txt
git add initial.txt
git commit -q -m "initial"
git push -q origin HEAD:refs/heads/main 2>/dev/null
# Add a second commit so diff yields a file name
echo "y" > feature.txt
git add feature.txt
git commit -q -m "feature"
cd - >/dev/null

# Create stub bd binary that counts calls and logs for-chuck label usage
STUB_DIR=$(mktemp -d)
BD_LOG="$STUB_DIR/bd-calls.log"
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
# Stub bd: logs all args to a file; always exits 0
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"
export BD_LOG

# Stub gh so PR URL check doesn't block
cat > "$STUB_DIR/gh" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$STUB_DIR/gh"

# Stub sable-msg (SABLE-bldh.15): logs its args and exits with SABLE_MSG_STUB_RC.
# Default rc=1 simulates "no Chuck pane reachable" so the hook falls back to the
# durable for-chuck bead — keeping every pre-existing assertion valid unchanged.
SABLE_MSG_LOG="$STUB_DIR/sable-msg-calls.log"
cat > "$STUB_DIR/sable-msg" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${SABLE_MSG_LOG:-/dev/null}"
exit "${SABLE_MSG_STUB_RC:-1}"
EOF
chmod +x "$STUB_DIR/sable-msg"
export SABLE_MSG_LOG

# Manager identity env
MGR_ENV="CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager"

# --------------------------------------------------------------------------
# Helper: build a PostToolUse JSON payload
# --------------------------------------------------------------------------
# make_post_input <command> <cwd> [stdout] [stderr]
make_post_input() {
  local cmd="$1" cwd="$2" stdout="${3:-}" stderr="${4:-}"
  python3 -c "
import json, sys
cmd, cwd, stdout, stderr = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
print(json.dumps({
    'tool_input': {'command': cmd},
    'cwd': cwd,
    'tool_response': {'stdout': stdout, 'stderr': stderr}
}))
" "$cmd" "$cwd" "$stdout" "$stderr"
}

# make_member_post_input <command> <cwd> <agent_type> [stdout] [stderr]
# Builds a PostToolUse payload shaped like a TEAMS MEMBER / subagent: carries
# agent_id + agent_type (the member's spawn name) and NO env identity, so the
# hook must attribute via lib-identity's resolved name, not $CLAUDE_AGENT_NAME
# (SABLE-amj.5; the same resolved-identity fix as SABLE-8fp for nested v2).
make_member_post_input() {
  local cmd="$1" cwd="$2" atype="$3" stdout="${4:-}" stderr="${5:-}"
  python3 -c "
import json, sys
cmd, cwd, atype, stdout, stderr = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
print(json.dumps({
    'agent_id': 'opaque-member-id',
    'agent_type': atype,
    'tool_input': {'command': cmd},
    'cwd': cwd,
    'tool_response': {'stdout': stdout, 'stderr': stderr},
}))
" "$cmd" "$cwd" "$atype" "$stdout" "$stderr"
}

# run_hook <env_prefix> <json> → prints hook stdout+stderr
run_hook() {
  local env_prefix="$1" json="$2"
  env -i PATH="$STUB_DIR:$PATH" BD_LOG="$BD_LOG" \
    SABLE_MSG_LOG="$SABLE_MSG_LOG" SABLE_MSG_STUB_RC="${SABLE_MSG_STUB_RC:-1}" \
    $env_prefix bash "$HOOK" <<< "$json" 2>/dev/null
}

# assert_bd_called <test-name> → check BD_LOG has an entry
assert_bd_called() {
  local name="$1"
  if grep -q 'for-chuck' "$BD_LOG" 2>/dev/null; then
    pass "$name"
  else
    fail "$name" "bd was NOT called with for-chuck label (BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -3))"
  fi
  rm -f "$BD_LOG"
}

# assert_bd_not_called <test-name>
assert_bd_not_called() {
  local name="$1"
  if grep -q 'for-chuck' "$BD_LOG" 2>/dev/null; then
    fail "$name" "bd WAS called with for-chuck label but should NOT have been (BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -3))"
  else
    pass "$name"
  fi
  rm -f "$BD_LOG"
}

# --------------------------------------------------------------------------
# Matcher unit tests — SABLE-jpr / SABLE-0u1
# Each test confirms whether bd create (for-chuck) was / was not called.
# --------------------------------------------------------------------------

# Test 1: plain 'git push' triggers notification
INPUT=$(make_post_input "git push" "$FIXTURE_REPO")
run_hook "$MGR_ENV" "$INPUT" >/dev/null
assert_bd_called "matcher: plain 'git push' triggers for-chuck"

# Test 2: 'git -C /x push' triggers notification
INPUT=$(make_post_input "git -C $FIXTURE_REPO push origin main" "$FIXTURE_REPO")
run_hook "$MGR_ENV" "$INPUT" >/dev/null
assert_bd_called "matcher: 'git -C <path> push origin main' triggers for-chuck"

# Test 3: 'git -c a=b push origin main' triggers notification
INPUT=$(make_post_input "git -c http.extraheader=Authorization:bearer push origin main" "$FIXTURE_REPO")
run_hook "$MGR_ENV" "$INPUT" >/dev/null
assert_bd_called "matcher: 'git -c a=b push origin main' triggers for-chuck"

# Test 4: 'git --no-pager push' triggers notification
INPUT=$(make_post_input "git --no-pager push" "$FIXTURE_REPO")
run_hook "$MGR_ENV" "$INPUT" >/dev/null
assert_bd_called "matcher: 'git --no-pager push' triggers for-chuck"

# Test 5: 'git pushd' does NOT trigger notification
INPUT=$(make_post_input "git pushd" "$FIXTURE_REPO")
run_hook "$MGR_ENV" "$INPUT" >/dev/null
assert_bd_not_called "matcher: 'git pushd' does NOT trigger for-chuck"

# Test 6: text mention in quoted arg does NOT trigger (SABLE-0u1 analog for post-push)
INPUT=$(make_post_input 'bd update SABLE-abc --description "Please git push to deploy"' "$FIXTURE_REPO")
run_hook "$MGR_ENV" "$INPUT" >/dev/null
assert_bd_not_called "matcher: text mention 'git push' in description does NOT trigger"

# Test 7: 'echo git pushed' does NOT trigger
INPUT=$(make_post_input "echo 'just ran git push'" "$FIXTURE_REPO")
run_hook "$MGR_ENV" "$INPUT" >/dev/null
assert_bd_not_called "matcher: 'echo git push' does NOT trigger"

# Test 7b: 'SABLE_SKIP_PRE_PUSH=1 git push' DOES trigger for-chuck (env-assignment prefix, SABLE-531)
INPUT=$(make_post_input "SABLE_SKIP_PRE_PUSH=1 git push" "$FIXTURE_REPO")
run_hook "$MGR_ENV" "$INPUT" >/dev/null
assert_bd_called "matcher: 'SABLE_SKIP_PRE_PUSH=1 git push' triggers for-chuck (env-assignment prefix)"

# Test 8: No manager identity → no-op
INPUT=$(make_post_input "git push" "$FIXTURE_REPO")
run_hook "" "$INPUT" >/dev/null
assert_bd_not_called "no manager identity → no bd create"

# --------------------------------------------------------------------------
# SABLE-61n: bogus base ref → fallback → bd create still called
# --------------------------------------------------------------------------

# Test 9: SABLE_BASE_BRANCH=origin/nonexistent with no such ref — hook should
# fall back to origin/main and still file the bead.
INPUT=$(make_post_input "git push" "$FIXTURE_REPO")
HOOK_EXIT=0
run_hook "$MGR_ENV SABLE_BASE_BRANCH=origin/nonexistent" "$INPUT" >/dev/null || HOOK_EXIT=$?
if [ "$HOOK_EXIT" -eq 0 ]; then
  pass "bogus base ref: hook exits 0 (does not abort)"
else
  fail "bogus base ref: hook exited $HOOK_EXIT (should exit 0 via fallback)"
fi
assert_bd_called "bogus base ref: bd create still called via fallback base"

# --------------------------------------------------------------------------
# Integration test: scratch git repo, real bd stub, origin/dev absent
# --------------------------------------------------------------------------

# Set up a second clean repo pair for the integration scenario
INT_BARE=$(mktemp -d)
INT_REPO=$(mktemp -d)
trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR" "$INT_BARE" "$INT_REPO"' EXIT

git init -q --bare "$INT_BARE"
git clone -q "$INT_BARE" "$INT_REPO"
cd "$INT_REPO"
git config user.email "int@test"
git config user.name "Integration"
echo "a" > base.txt
git add base.txt
git commit -q -m "base"
git push -q origin HEAD:refs/heads/main 2>/dev/null
echo "b" > feature2.txt
git add feature2.txt
git commit -q -m "feature2"
cd - >/dev/null
# Note: origin/dev intentionally NOT created

INT_INPUT=$(make_post_input "git push" "$INT_REPO")
HOOK_EXIT=0
run_hook "$MGR_ENV SABLE_BASE_BRANCH=origin/dev" "$INT_INPUT" >/dev/null || HOOK_EXIT=$?
if [ "$HOOK_EXIT" -eq 0 ]; then
  pass "integration: hook exits 0 with missing origin/dev"
else
  fail "integration: hook exited $HOOK_EXIT"
fi
if grep -q 'for-chuck' "$BD_LOG" 2>/dev/null; then
  pass "integration: for-chuck bead filed despite missing origin/dev"
else
  fail "integration: for-chuck bead NOT filed despite missing origin/dev (BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -3))"
fi
rm -f "$BD_LOG"

# --------------------------------------------------------------------------
# Teams-member attribution — SABLE-amj.5
# A manager spawned as a team member has NO env CLAUDE_AGENT_NAME; its identity
# is the hook-input agent_type. The for-chuck bead must attribute to the resolved
# name (optimus), not an empty env var. Pre-fix the title was "Review PR from : ".
# --------------------------------------------------------------------------

# Hermetic fixture registry so resolution does not depend on the installed one.
FIX_YAML="$STUB_DIR/agents.yaml"
cat > "$FIX_YAML" <<'YAML'
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
  chuck:
    type: integrator
YAML

MEMBER_INPUT=$(make_member_post_input "git push" "$FIXTURE_REPO" "optimus")
run_hook "SABLE_AGENTS_YAML=$FIX_YAML" "$MEMBER_INPUT" >/dev/null
if grep -q 'for-chuck' "$BD_LOG" 2>/dev/null; then
  pass "teams member (no env identity): for-chuck bead filed via resolved identity"
else
  fail "teams member (no env identity): for-chuck bead filed via resolved identity" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -3)"
fi
if grep -q 'from optimus' "$BD_LOG" 2>/dev/null; then
  pass "teams member: PR attributed to resolved name (optimus), not empty env"
else
  fail "teams member: PR attributed to resolved name (optimus)" "title missing resolved name (BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -3))"
fi
rm -f "$BD_LOG"

# --------------------------------------------------------------------------
# v3 resolved-identity attribution — SABLE-8fp / SABLE-aok
# In v3 the push happens inside a manager SUBAGENT: env belongs to the PARENT
# session and must be ignored. Attribution comes from SABLE_ID_NAME, never the
# raw CLAUDE_AGENT_NAME env var, and the hook must survive an unset env var
# under set -euo pipefail.
# --------------------------------------------------------------------------

# Case 1: manager-subagent push (tarzan) with deliberate env contamination
# (CLAUDE_AGENT_NAME=lincoln). Resolved identity must win; lincoln must NOT leak.
MEMBER_INPUT_C=$(make_member_post_input "git push" "$FIXTURE_REPO" "tarzan")
run_hook "CLAUDE_AGENT_NAME=lincoln CLAUDE_AGENT_ROLE=manager SABLE_AGENTS_YAML=$FIX_YAML" "$MEMBER_INPUT_C" >/dev/null
if grep -q 'Review PR from tarzan' "$BD_LOG" 2>/dev/null \
   && grep -q 'Submitted by: tarzan' "$BD_LOG" 2>/dev/null \
   && grep -q 'for-tarzan' "$BD_LOG" 2>/dev/null; then
  pass "manager-subagent (tarzan) attributes to resolved identity despite env CLAUDE_AGENT_NAME=lincoln"
else
  fail "manager-subagent (tarzan) attributes to resolved identity despite env contamination" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -8)"
fi
if grep -qi 'lincoln' "$BD_LOG" 2>/dev/null; then
  fail "env contamination: no 'lincoln' leaks into the for-chuck bead" "BD_LOG contains lincoln: $(grep -i lincoln "$BD_LOG" | head -2)"
else
  pass "env contamination: no 'lincoln' leaks into the for-chuck bead"
fi
rm -f "$BD_LOG"

# Case 2: unset CLAUDE_AGENT_NAME must not crash under set -euo pipefail
# (the silent-failure regression: a dereference of $CLAUDE_AGENT_NAME would kill
# the hook before bd create and drop the Chuck handoff).
MEMBER_INPUT_T=$(make_member_post_input "git push" "$FIXTURE_REPO" "tarzan")
ERR=$(env -i PATH="$STUB_DIR:$PATH" BD_LOG="$BD_LOG" SABLE_AGENTS_YAML="$FIX_YAML" bash "$HOOK" <<< "$MEMBER_INPUT_T" 2>&1 >/dev/null); RC=$?
if [ "$RC" -eq 0 ] && ! echo "$ERR" | grep -qi "unbound variable"; then
  pass "unset CLAUDE_AGENT_NAME: hook survives set -u (exit 0, no unbound variable)"
else
  fail "unset CLAUDE_AGENT_NAME: hook survives set -u" "rc=$RC err=${ERR:-<none>}"
fi
if grep -q 'for-chuck' "$BD_LOG" 2>/dev/null && grep -q 'from tarzan' "$BD_LOG" 2>/dev/null; then
  pass "unset CLAUDE_AGENT_NAME: bead still files attributed to tarzan"
else
  fail "unset CLAUDE_AGENT_NAME: bead still files attributed to tarzan" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -8)"
fi
rm -f "$BD_LOG"

# Case 3: env-identified legacy manager attributes by env name (resolved == env).
# NB: the spec's 'chuck' example is moot — the hook skips chuck's own pushes (it
# IS the merge integrator, line 24); optimus is the representative env-terminal
# manager and exercises the legacy escape path in lib-identity.
INPUT_ENV=$(make_post_input "git push" "$FIXTURE_REPO")
run_hook "CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager" "$INPUT_ENV" >/dev/null
if grep -q 'Review PR from optimus' "$BD_LOG" 2>/dev/null && grep -q 'Submitted by: optimus' "$BD_LOG" 2>/dev/null; then
  pass "env-identified manager (optimus) attributes by resolved env name"
else
  fail "env-identified manager (optimus) attributes by resolved env name" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -8)"
fi
rm -f "$BD_LOG"

# --------------------------------------------------------------------------
# SABLE-041: worktree push via `git -C <worktree>` from the main checkout
# cwd must report the WORKTREE's branch, not the cwd's branch. Buggy hook
# rev-parses cwd (FIXTURE_REPO's branch); fixed hook resolves the -C dir.
# --------------------------------------------------------------------------
WT_041="$STUB_DIR/wt-041"
git -C "$FIXTURE_REPO" worktree add -q -b wk-041 "$WT_041" >/dev/null 2>&1
cd "$WT_041"
echo "z" > wt_change.txt
git add wt_change.txt
git commit -q -m "wt change on wk-041"
cd - >/dev/null

INPUT=$(make_post_input "git -C $WT_041 push" "$FIXTURE_REPO")
run_hook "$MGR_ENV" "$INPUT" >/dev/null
if grep -q 'wk-041' "$BD_LOG" 2>/dev/null; then
  pass "SABLE-041: for-chuck reports the -C worktree branch (wk-041), not the cwd branch"
else
  fail "SABLE-041: for-chuck reports the -C worktree branch (wk-041)" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -4)"
fi
rm -f "$BD_LOG"
git -C "$FIXTURE_REPO" worktree remove --force "$WT_041" 2>/dev/null

# --------------------------------------------------------------------------
# SABLE-bldh.15: message-first handoff (event-driven Chuck) vs bead fallback
# --------------------------------------------------------------------------

# (a) Chuck reachable (sable-msg rc=0): the message IS the handoff; NO for-chuck bead.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV" "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -q 'chuck' "$SABLE_MSG_LOG" 2>/dev/null && grep -q 'from optimus' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "message-first: sable-msg chuck sent (from optimus) when Chuck reachable"
else
  fail "message-first: sable-msg chuck sent (from optimus)" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null | head -3)"
fi
assert_bd_not_called "message-first: NO for-chuck bead when Chuck reachable"

# (b) Chuck unreachable (sable-msg rc=1): message attempted, bead filed (fallback).
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=1 run_hook "$MGR_ENV" "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -q 'chuck' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "fallback: sable-msg chuck attempted before falling back to bead"
else
  fail "fallback: sable-msg chuck attempted" "MSG_LOG empty: $(cat "$SABLE_MSG_LOG" 2>/dev/null | head -3)"
fi
assert_bd_called "fallback: for-chuck bead filed when Chuck unreachable"

# (c) Messaging disabled (SABLE_MERGE_NOTIFY_VIA_MSG=0): no message attempt; bead filed.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV SABLE_MERGE_NOTIFY_VIA_MSG=0" "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if [ -s "$SABLE_MSG_LOG" ]; then
  fail "disabled: sable-msg NOT attempted when SABLE_MERGE_NOTIFY_VIA_MSG=0" "MSG_LOG: $(cat "$SABLE_MSG_LOG")"
else
  pass "disabled: sable-msg NOT attempted when SABLE_MERGE_NOTIFY_VIA_MSG=0"
fi
assert_bd_called "disabled: for-chuck bead filed when messaging disabled"

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
