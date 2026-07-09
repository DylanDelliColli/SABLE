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
# SABLE-b06t: push the "feature" commit for real too, under the ACTUAL local
# branch name ONLY (not 'main' again) — the hook now positively confirms via
# ls-remote that refs/heads/<local branch> matches local HEAD before
# notifying, so a fixture that only commits locally (never landing on the
# bare origin) no longer represents "this push succeeded". origin/main stays
# pinned at the earlier "initial" commit so `git diff origin/main...HEAD`
# still yields feature.txt — re-pushing "feature" onto refs/heads/main here
# would make origin/main == HEAD and erase the diff FILES depends on. Not
# renaming the local branch to 'main' either: this environment's
# init.defaultBranch is NOT 'main' (confirmed 'master'), and renaming it TO
# 'main' would collide with sable_resolve_integration_branch's OWN
# unconfigured-repo default of 'main', false-triggering the
# integration-branch self-push guard.
FIXTURE_CUR_BRANCH=$(git symbolic-ref --short HEAD)
git push -q origin "HEAD:refs/heads/$FIXTURE_CUR_BRANCH" 2>/dev/null
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
# SABLE-b06t: push the "feature2" commit under the actual local branch name
# ONLY, leaving origin/main pinned at "base" — see the FIXTURE_REPO comment
# above for why (re-pushing onto refs/heads/main here would make origin/main
# == HEAD and erase the diff FILES depends on; the local branch isn't
# renamed to 'main' either, to avoid colliding with
# sable_resolve_integration_branch's unconfigured-repo default).
INT_CUR_BRANCH=$(git symbolic-ref --short HEAD)
git push -q origin "HEAD:refs/heads/$INT_CUR_BRANCH" 2>/dev/null
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
git push -q origin HEAD:refs/heads/wk-041 2>/dev/null
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
# market-brief-package-2u25: integration-branch self-push must NOT file a
# for-chuck handoff. Repo-local git config resolves the integration branch
# PER REPO, winning over a foreign SABLE_BASE_BRANCH env value (the
# session-global-vs-per-repo bug: env says origin/llm-integration, a ref that
# does not exist in this fixture repo at all).
# --------------------------------------------------------------------------

INTNOTIFY_REPO=$(mktemp -d)
INTNOTIFY_BARE=$(mktemp -d)
trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR" "$INTNOTIFY_REPO" "$INTNOTIFY_BARE"' EXIT
git init -q --bare "$INTNOTIFY_BARE"
git clone -q "$INTNOTIFY_BARE" "$INTNOTIFY_REPO"
cd "$INTNOTIFY_REPO"
git config user.email "t@t"; git config user.name "t"
echo base > base.txt; git add base.txt; git commit -q -m base
git push -q origin HEAD:refs/heads/main 2>/dev/null
git checkout -q -b tmux-only
echo i1 > i1.txt; git add i1.txt; git commit -q -m i1
git config sable.integrationBranch tmux-only
cd - >/dev/null

# (a) pushing the repo's own integration branch (resolved via repo-local
# config, NOT the foreign env) does NOT file for-chuck, and does not even
# attempt the message-first handoff.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
INT_INPUT_A=$(make_post_input "git push origin tmux-only" "$INTNOTIFY_REPO")
run_hook "$MGR_ENV SABLE_BASE_BRANCH=origin/llm-integration" "$INT_INPUT_A" >/dev/null
assert_bd_not_called "market-brief-package-2u25: integration-branch self-push does NOT file for-chuck"
if [ -s "$SABLE_MSG_LOG" ]; then
  fail "market-brief-package-2u25: integration-branch self-push does not attempt sable-msg either" "MSG_LOG: $(cat "$SABLE_MSG_LOG")"
else
  pass "market-brief-package-2u25: integration-branch self-push does not attempt sable-msg either"
fi
rm -f "$SABLE_MSG_LOG"

# (b) a worker/feature branch pushed in the SAME repo still notifies.
cd "$INTNOTIFY_REPO"
git checkout -q -b wk-other tmux-only
echo w1 > w1.txt; git add w1.txt; git commit -q -m w1
git push -q origin HEAD:refs/heads/wk-other 2>/dev/null
cd - >/dev/null
INT_INPUT_B=$(make_post_input "git push origin wk-other" "$INTNOTIFY_REPO")
run_hook "$MGR_ENV SABLE_BASE_BRANCH=origin/llm-integration" "$INT_INPUT_B" >/dev/null
assert_bd_called "market-brief-package-2u25: non-integration branch push in same repo still notifies"

# --------------------------------------------------------------------------
# SABLE-pzfk: default BASE_BRANCH must resolve to the repo's PUBLISHED
# integration branch, not a hardcoded origin/main, when SABLE_BASE_BRANCH is
# unset. Fixture: integration branch (tmux-only) diverges from main by 9 doc
# files; a worker branch off tmux-only adds exactly 1 real file. Pre-fix,
# diffing against origin/main reports all 10 files (the alphabetical-docs-
# prefix symptom chuck actually saw); fixed, diffing against
# origin/tmux-only reports exactly the 1 real file — and the overlap
# analysis (fed from FILES) must derive from that corrected set: no phantom
# overlap against a doc file nobody actually touched on this push.
# --------------------------------------------------------------------------

PZFK_BARE=$(mktemp -d)
PZFK_REPO=$(mktemp -d)
trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR" "$INT_BARE" "$INT_REPO" "$INTNOTIFY_REPO" "$INTNOTIFY_BARE" "$PZFK_BARE" "$PZFK_REPO"' EXIT

git init -q --bare "$PZFK_BARE"
git clone -q "$PZFK_BARE" "$PZFK_REPO"
cd "$PZFK_REPO"
git config user.email "p@p"; git config user.name "p"
echo base > base.txt; git add base.txt; git commit -q -m base
git push -q origin HEAD:refs/heads/main 2>/dev/null

git checkout -q -b tmux-only
for i in 1 2 3 4 5 6 7 8 9; do echo "d$i" > "doc$i.txt"; done
git add doc*.txt
git commit -q -m "integration branch doc history"
git push -q origin HEAD:refs/heads/tmux-only 2>/dev/null
git config sable.integrationBranch tmux-only

git checkout -q -b wk-worker
echo real > real_change.txt
git add real_change.txt
git commit -q -m "worker: real change"
git push -q origin HEAD:refs/heads/wk-worker 2>/dev/null
cd - >/dev/null

# Section-local bd stub: `list --status=in_progress --json` returns a real
# in-progress bead claiming doc1.txt via WIP-CLAIMS — a file that's part of
# the PHANTOM origin/main diff but NOT the worker's real diff. Overlap must
# NOT fire on it once the base ref is fixed. `create` still logs to BD_LOG.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list" ]; then
  cat <<'JSON'
[{"id": "SABLE-fakein", "title": "unrelated in-progress work", "assignee": "someone", "notes": "WIP-CLAIMS: doc1.txt"}]
JSON
  exit 0
fi
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

rm -f "$BD_LOG" "$SABLE_MSG_LOG"
PZFK_INPUT=$(make_post_input "git push" "$PZFK_REPO")
run_hook "$MGR_ENV" "$PZFK_INPUT" >/dev/null

if grep -q 'real_change.txt' "$BD_LOG" 2>/dev/null; then
  pass "SABLE-pzfk: default base resolves to published integration branch — real file listed"
else
  fail "SABLE-pzfk: default base resolves to published integration branch — real file listed" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null)"
fi

if grep -qE 'doc[19]\.txt' "$BD_LOG" 2>/dev/null; then
  fail "SABLE-pzfk: doc-history files from origin/main are NOT in the file list" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null)"
else
  pass "SABLE-pzfk: doc-history files from origin/main are NOT in the file list"
fi

if grep -q 'OVERLAP-WARNING' "$BD_LOG" 2>/dev/null; then
  fail "SABLE-pzfk: no spurious overlap warning against doc1.txt (not really touched by this push)" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null)"
else
  pass "SABLE-pzfk: no spurious overlap warning against doc1.txt (not really touched by this push)"
fi

rm -f "$BD_LOG"

# Restore the plain bd stub for hermeticity if more tests are appended later.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

# --------------------------------------------------------------------------
# SABLE-b06t: replace the vacuous failure-phrase grep with positive push
# confirmation (git ls-remote tip vs local HEAD). The old heuristic only
# caught failures matching its exact phrase list; live incidents slipped
# through it both when the failure text didn't match (wk-prodspawn: chuck
# reviewed a PR-ready message for a branch that never reached origin) and
# when there was no failure at all, just nothing NEW to push ("Everything
# up-to-date" on tmux-only, chuck received a self-referential merge request
# for unchanged content).
# --------------------------------------------------------------------------

B06T_BARE=$(mktemp -d)
B06T_REPO=$(mktemp -d)
trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR" "$INT_BARE" "$INT_REPO" "$INTNOTIFY_REPO" "$INTNOTIFY_BARE" "$PZFK_BARE" "$PZFK_REPO" "$B06T_BARE" "$B06T_REPO"' EXIT

git init -q --bare "$B06T_BARE"
git clone -q "$B06T_BARE" "$B06T_REPO"
cd "$B06T_REPO"
git config user.email "b@b"; git config user.name "b"
echo base > base.txt; git add base.txt; git commit -q -m base
B06T_MAIN=$(git symbolic-ref --short HEAD)
git push -q origin "HEAD:refs/heads/$B06T_MAIN" 2>/dev/null
# Also publish origin/main so BASE_BRANCH resolution (default/fallback) has
# a real diff target — mirrors the FIXTURE_REPO/INT_REPO fixtures above.
git push -q origin HEAD:refs/heads/main 2>/dev/null

# A branch with a real local commit that is NOT (yet) on origin — models a
# push that never actually landed, whatever the reason (rejected, network/
# auth failure, or any failure text the old grep didn't know about).
git checkout -q -b wk-b06t
echo change > b06t_change.txt; git add b06t_change.txt; git commit -q -m "worker change"
cd - >/dev/null

# (1) Realistic rejected-push stderr text — the old heuristic WOULD catch
# this one (it matches 'rejected'/'failed to push'), but confirm the new
# positive check also agrees: no notify, since the branch tip really isn't
# on origin.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
B06T_INPUT_1=$(make_post_input "git push" "$B06T_REPO" "" "! [rejected]        wk-b06t -> wk-b06t (non-fast-forward)
error: failed to push some refs to 'origin'")
run_hook "$MGR_ENV" "$B06T_INPUT_1" >/dev/null
assert_bd_not_called "SABLE-b06t: rejected push (branch not really on origin) does NOT file for-chuck"

# (2) The exact schema-drift hypothesis this bead investigated: EMPTY
# stdout/stderr (as if output capture were unavailable), same unpushed
# branch. The old grep-only heuristic has nothing to match here and would
# notify anyway; the positive ls-remote check doesn't depend on the text at
# all and must still refuse.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
B06T_INPUT_2=$(make_post_input "git push" "$B06T_REPO" "" "")
run_hook "$MGR_ENV" "$B06T_INPUT_2" >/dev/null
assert_bd_not_called "SABLE-b06t: empty stdout/stderr + unpushed branch does NOT file for-chuck"

# (3) Now actually push wk-b06t for real — the branch tip IS confirmable on
# origin — and confirm notify fires.
cd "$B06T_REPO"
git push -q origin HEAD:refs/heads/wk-b06t 2>/dev/null
cd - >/dev/null
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
B06T_INPUT_3=$(make_post_input "git push" "$B06T_REPO")
run_hook "$MGR_ENV" "$B06T_INPUT_3" >/dev/null
assert_bd_called "SABLE-b06t: confirmable push (branch tip matches origin) files for-chuck"

# (4) No-op push guard: 'Everything up-to-date' in the captured output must
# not notify even though the branch tip trivially matches origin (it's the
# same content as an earlier, already-notified push) — ls-remote tip
# comparison alone can't distinguish a fresh landing from a resend of
# already-landed content.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
B06T_INPUT_4=$(make_post_input "git push" "$B06T_REPO" "" "Everything up-to-date")
run_hook "$MGR_ENV" "$B06T_INPUT_4" >/dev/null
assert_bd_not_called "SABLE-b06t: 'Everything up-to-date' no-op push does NOT file for-chuck"

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
