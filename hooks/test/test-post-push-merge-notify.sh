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

# SABLE-yn5t: guard every `cd` into a mktemp fixture repo. A bare `cd "$dir"`
# that fails (the busy-/tmp race where the dir was reaped or never created)
# silently leaves CWD in the REAL worktree, and the following bare git ops then
# run there — the CONFIRMED SABLE-a5a5 identity-pollution mechanism (bare
# `git config user.name Test` writing into the real .git/config). Abort instead
# so a misrouted invocation can never mutate the real repo. Paired with the
# `git -C "$REPO" config` scoping below, which can never touch the real repo
# regardless of CWD (z776 pattern, wk-fixture-isolation 55ae0ba4).
cd_fixture() {
  cd "$1" || { echo "FATAL: cd to fixture repo $1 failed — aborting so fixture git ops never touch the real worktree"; exit 2; }
}

# Create a real scratch git repo with one commit and a 'main' branch so
# git -C <path> diff origin/main...HEAD works.
FIXTURE_REPO=$(mktemp -d)
BARE_ORIGIN=$(mktemp -d)
trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR"' EXIT

git init -q --bare "$BARE_ORIGIN"
git clone -q "$BARE_ORIGIN" "$FIXTURE_REPO"
cd_fixture "$FIXTURE_REPO"
git -C "$FIXTURE_REPO" config user.email "test@test"
git -C "$FIXTURE_REPO" config user.name "Test"
# SABLE-r1zs: pin the fixture's own working branch explicitly, never the
# ambient `git init.defaultBranch` (a clone of an empty bare repo checks out
# whatever the LOCAL client's default resolves to, which drifts between
# 'master' and 'main' across environments — confirmed the suite's 22-24/45-50
# red count under init.defaultBranch=main). Must differ from 'main' (pushed to
# origin/main explicitly below) so it never collides with
# sable_resolve_integration_branch's OWN unconfigured-repo default of 'main',
# which would false-trigger the hook's integration-branch self-push guard.
git checkout -q -b sable-test-trunk
echo "x" > initial.txt
git add initial.txt
git commit -q -m "initial"
git push -q "$BARE_ORIGIN" HEAD:refs/heads/main 2>/dev/null
git update-ref refs/remotes/origin/main HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
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
# would make origin/main == HEAD and erase the diff FILES depends on.
FIXTURE_CUR_BRANCH=$(git symbolic-ref --short HEAD)
git push -q "$BARE_ORIGIN" "HEAD:refs/heads/$FIXTURE_CUR_BRANCH" 2>/dev/null
git update-ref "refs/remotes/origin/$FIXTURE_CUR_BRANCH" HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
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

# Unified tmux stub. Two probes shell out to tmux and both are stubbed here so
# every hook invocation is hermetic (never touches the operator's real tmux
# server):
#   - list-panes  (SABLE-wvk9 chuck-presence probe): emit a `chuck` role line so
#     the hook treats Chuck as reachable, UNLESS the test models Chuck absent via
#     SABLE_STUB_CHUCK_PRESENT=0.
#   - display-message (SABLE-nmmh worker-landing role gate): echo the pane role
#     the test pins via SABLE_STUB_PANE_ROLE.
# Installed with the other fixtures (not late) because the chuck probe runs on
# EVERY message-block invocation, not only when TMUX_PANE is set. Default =
# Chuck present, so pre-existing fall-through cases behave as a normal live fleet.
cat > "$STUB_DIR/tmux" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "list-panes" ]; then
    [ "${SABLE_STUB_CHUCK_PRESENT:-1}" != "0" ] && echo "chuck"
    echo "optimus"
    exit 0
  fi
  if [ "$a" = "display-message" ]; then
    echo "${SABLE_STUB_PANE_ROLE:-}"
    exit 0
  fi
done
exit 0
EOF
chmod +x "$STUB_DIR/tmux"

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
# SABLE-tb1y: default the invocation-trace log into STUB_DIR (trap-cleaned) so
# tracing is hermetic; a test may override SABLE_HOOK_TRACE_LOG / SABLE_HOOK_TRACE
# in env_prefix (later `env` assignment wins).
run_hook() {
  local env_prefix="$1" json="$2"
  env -i PATH="$STUB_DIR:$PATH" BD_LOG="$BD_LOG" \
    SABLE_MSG_LOG="$SABLE_MSG_LOG" SABLE_MSG_STUB_RC="${SABLE_MSG_STUB_RC:-1}" \
    SABLE_HOOK_TRACE_LOG="$STUB_DIR/hook-trace.log" \
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
cd_fixture "$INT_REPO"
git -C "$INT_REPO" config user.email "int@test"
git -C "$INT_REPO" config user.name "Integration"
# SABLE-r1zs: pin explicitly — see the FIXTURE_REPO comment above.
git checkout -q -b sable-test-trunk
echo "a" > base.txt
git add base.txt
git commit -q -m "base"
git push -q "$INT_BARE" HEAD:refs/heads/main 2>/dev/null
git update-ref refs/remotes/origin/main HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
echo "b" > feature2.txt
git add feature2.txt
git commit -q -m "feature2"
# SABLE-b06t: push the "feature2" commit under the actual local branch name
# ONLY, leaving origin/main pinned at "base" — see the FIXTURE_REPO comment
# above for why (re-pushing onto refs/heads/main here would make origin/main
# == HEAD and erase the diff FILES depends on).
INT_CUR_BRANCH=$(git symbolic-ref --short HEAD)
git push -q "$INT_BARE" "HEAD:refs/heads/$INT_CUR_BRANCH" 2>/dev/null
git update-ref "refs/remotes/origin/$INT_CUR_BRANCH" HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
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
cd_fixture "$WT_041"
echo "z" > wt_change.txt
git add wt_change.txt
git commit -q -m "wt change on wk-041"
git push -q "$BARE_ORIGIN" HEAD:refs/heads/wk-041 2>/dev/null
git update-ref refs/remotes/origin/wk-041 HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
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
# SABLE-bldh.15 / SABLE-wvk9: message-first handoff vs durable bead fallback.
# Reachability is now modeled the way the hook actually decides it: a live
# @sable_role=chuck pane (tmux stub, SABLE_STUB_CHUCK_PRESENT) gates the send,
# and only a CONFIRMED sable-msg (rc=0) suppresses the durable bead.
# --------------------------------------------------------------------------

# (a) Chuck reachable (pane present + sable-msg rc=0): the message IS the handoff;
# NO for-chuck bead AND no fallback context line.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
CTX_A=$(SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV" "$(make_post_input "git push" "$FIXTURE_REPO")")
if grep -q 'chuck' "$SABLE_MSG_LOG" 2>/dev/null && grep -q 'from optimus' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "message-first: sable-msg chuck sent (from optimus) when Chuck reachable"
else
  fail "message-first: sable-msg chuck sent (from optimus)" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null | head -3)"
fi
if printf '%s' "$CTX_A" | grep -qi 'fallback bead'; then
  fail "message-first: NO fallback context line when Chuck reachable+delivered" "STDOUT: $CTX_A"
else
  pass "message-first: NO fallback context line when Chuck reachable+delivered"
fi
assert_bd_not_called "message-first: NO for-chuck bead when Chuck reachable"

# (b) Chuck pane present but delivery NOT confirmed (sable-msg rc=1): message
# attempted, then falls through to the durable bead.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=1 run_hook "$MGR_ENV" "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -q 'chuck' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "fallback: sable-msg chuck attempted before falling back to bead"
else
  fail "fallback: sable-msg chuck attempted" "MSG_LOG empty: $(cat "$SABLE_MSG_LOG" 2>/dev/null | head -3)"
fi
assert_bd_called "fallback: for-chuck bead filed when delivery not confirmed"

# (c) Messaging disabled (SABLE_MERGE_NOTIFY_VIA_MSG=0): no message attempt; bead filed.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV SABLE_MERGE_NOTIFY_VIA_MSG=0" "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if [ -s "$SABLE_MSG_LOG" ]; then
  fail "disabled: sable-msg NOT attempted when SABLE_MERGE_NOTIFY_VIA_MSG=0" "MSG_LOG: $(cat "$SABLE_MSG_LOG")"
else
  pass "disabled: sable-msg NOT attempted when SABLE_MERGE_NOTIFY_VIA_MSG=0"
fi
assert_bd_called "disabled: for-chuck bead filed when messaging disabled"

# (d) Chuck pane ABSENT (no @sable_role=chuck pane — Chuck not spawned at push
# time, the exact SABLE-wvk9 incident). The hook must NOT make the futile send,
# MUST file the durable for-chuck fallback bead, MUST exit 0, and MUST print a
# context line naming the fallback so it is never silent. Regression guard for
# the stranded wk-desc-gate-paths merge.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
CTX_D=$(SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV SABLE_STUB_CHUCK_PRESENT=0" "$(make_post_input "git push" "$FIXTURE_REPO")"); WVK9_EXIT=$?
if [ "$WVK9_EXIT" -eq 0 ]; then
  pass "SABLE-wvk9: hook exits 0 when Chuck pane absent"
else
  fail "SABLE-wvk9: hook exits 0 when Chuck pane absent" "exit=$WVK9_EXIT"
fi
if [ -s "$SABLE_MSG_LOG" ]; then
  fail "SABLE-wvk9: no futile sable-msg attempt when Chuck pane absent" "MSG_LOG: $(cat "$SABLE_MSG_LOG")"
else
  pass "SABLE-wvk9: no futile sable-msg attempt when Chuck pane absent"
fi
if printf '%s' "$CTX_D" | grep -qi 'durable for-chuck fallback bead' \
   && printf '%s' "$CTX_D" | grep -qi 'no reachable chuck pane'; then
  pass "SABLE-wvk9: context line notes the durable for-chuck fallback (Chuck absent)"
else
  fail "SABLE-wvk9: context line notes the durable for-chuck fallback (Chuck absent)" "STDOUT: $CTX_D"
fi
assert_bd_called "SABLE-wvk9: durable for-chuck bead filed when Chuck pane absent"

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
cd_fixture "$INTNOTIFY_REPO"
git -C "$INTNOTIFY_REPO" config user.email "t@t"; git -C "$INTNOTIFY_REPO" config user.name "t"
echo base > base.txt; git add base.txt; git commit -q -m base
git push -q "$INTNOTIFY_BARE" HEAD:refs/heads/main 2>/dev/null
git update-ref refs/remotes/origin/main HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
git checkout -q -b tmux-only
echo i1 > i1.txt; git add i1.txt; git commit -q -m i1
git push -q "$INTNOTIFY_BARE" HEAD:refs/heads/tmux-only 2>/dev/null
git update-ref refs/remotes/origin/tmux-only HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically; SABLE-cstk: also required so DEFAULT_BASE_BRANCH resolves to origin/tmux-only instead of silently staying origin/main
git -C "$INTNOTIFY_REPO" config sable.integrationBranch tmux-only
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
cd_fixture "$INTNOTIFY_REPO"
git checkout -q -b wk-other tmux-only
echo w1 > w1.txt; git add w1.txt; git commit -q -m w1
git push -q "$INTNOTIFY_BARE" HEAD:refs/heads/wk-other 2>/dev/null
git update-ref refs/remotes/origin/wk-other HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
cd - >/dev/null
INT_INPUT_B=$(make_post_input "git push origin wk-other" "$INTNOTIFY_REPO")
run_hook "$MGR_ENV SABLE_BASE_BRANCH=origin/llm-integration" "$INT_INPUT_B" >/dev/null
assert_bd_called "market-brief-package-2u25: non-integration branch push in same repo still notifies"

# --------------------------------------------------------------------------
# SABLE-cstk: the FILES list itself must diff against origin/<integrationBranch>
# (tmux-only, adds i1.txt) not the foreign, non-existent SABLE_BASE_BRANCH's
# internal origin/main fallback (base.txt only, missing i1.txt). Pre-fix,
# sable_validate_base_ref's OWN hardcoded origin/main fallback wins once
# origin/llm-integration fails to verify, so the diff also picks up i1.txt
# (present on tmux-only, absent from origin/main) alongside the real w1.txt
# change — reproducing the false file list chuck saw live.
# --------------------------------------------------------------------------
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
INT_INPUT_C=$(make_post_input "git push origin wk-other" "$INTNOTIFY_REPO")
run_hook "$MGR_ENV SABLE_BASE_BRANCH=origin/llm-integration SABLE_MERGE_NOTIFY_VIA_MSG=0" "$INT_INPUT_C" >/dev/null
if grep -q 'w1.txt' "$BD_LOG" 2>/dev/null; then
  pass "SABLE-cstk: FILES list includes the real change (w1.txt) vs origin/tmux-only"
else
  fail "SABLE-cstk: FILES list includes the real change (w1.txt) vs origin/tmux-only" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null)"
fi
if grep -q 'i1.txt' "$BD_LOG" 2>/dev/null; then
  fail "SABLE-cstk: FILES list does NOT leak i1.txt from a phantom diff vs origin/main" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null)"
else
  pass "SABLE-cstk: FILES list does NOT leak i1.txt from a phantom diff vs origin/main"
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG"

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
cd_fixture "$PZFK_REPO"
git -C "$PZFK_REPO" config user.email "p@p"; git -C "$PZFK_REPO" config user.name "p"
echo base > base.txt; git add base.txt; git commit -q -m base
git push -q "$PZFK_BARE" HEAD:refs/heads/main 2>/dev/null
git update-ref refs/remotes/origin/main HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically

git checkout -q -b tmux-only
for i in 1 2 3 4 5 6 7 8 9; do echo "d$i" > "doc$i.txt"; done
git add doc*.txt
git commit -q -m "integration branch doc history"
git push -q "$PZFK_BARE" HEAD:refs/heads/tmux-only 2>/dev/null
git update-ref refs/remotes/origin/tmux-only HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
git -C "$PZFK_REPO" config sable.integrationBranch tmux-only

git checkout -q -b wk-worker
echo real > real_change.txt
git add real_change.txt
git commit -q -m "worker: real change"
git push -q "$PZFK_BARE" HEAD:refs/heads/wk-worker 2>/dev/null
git update-ref refs/remotes/origin/wk-worker HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
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
cd_fixture "$B06T_REPO"
git -C "$B06T_REPO" config user.email "b@b"; git -C "$B06T_REPO" config user.name "b"
echo base > base.txt; git add base.txt; git commit -q -m base
B06T_MAIN=$(git symbolic-ref --short HEAD)
git push -q "$B06T_BARE" "HEAD:refs/heads/$B06T_MAIN" 2>/dev/null
git update-ref "refs/remotes/origin/$B06T_MAIN" HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
# Also publish origin/main so BASE_BRANCH resolution (default/fallback) has
# a real diff target — mirrors the FIXTURE_REPO/INT_REPO fixtures above.
git push -q "$B06T_BARE" HEAD:refs/heads/main 2>/dev/null
git update-ref refs/remotes/origin/main HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically

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
cd_fixture "$B06T_REPO"
git push -q "$B06T_BARE" HEAD:refs/heads/wk-b06t 2>/dev/null
git update-ref refs/remotes/origin/wk-b06t HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
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
# SABLE-nmmh: a worker landing wakes the DISPATCHING MANAGER (event-driven loop)
# In the warm-pane topology the worker self-pushes; the post-push hook runs in
# the worker's env, whose CLAUDE_AGENT_NAME IS the lane manager (worker_env_args),
# so SABLE_ID_NAME already resolves to the dispatching manager. The hook must
# ALSO message that manager to wake it (managers now END their turn when idle).
# The worker-vs-manager discriminant is the pane's @sable_role tag: only a real
# worker landing (@sable_role=worker) notifies; a manager's OWN emergency push
# (@sable_role=<role>) must not self-notify. Chuck handoff stays regression-intact.
# --------------------------------------------------------------------------

# The tmux stub (display-message role gate + chuck-presence probe) is installed
# with the fixtures at the top of this file, so the worker-landing cases below
# read SABLE_STUB_PANE_ROLE through it directly.

# (a) worker landing (@sable_role=worker), Chuck reachable: the dispatching
# manager (optimus) is messaged with a "Worker landed" wake, --from worker; the
# Chuck merge handoff still fires alongside it.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -qE '^optimus .*Worker landed' "$SABLE_MSG_LOG" 2>/dev/null \
   && grep -q 'from worker' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-nmmh: worker landing wakes the dispatching manager (optimus, from worker)"
else
  fail "SABLE-nmmh: worker landing wakes the dispatching manager (optimus, from worker)" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi
if grep -qE '^chuck .*PR ready from optimus' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-nmmh: chuck merge handoff still fires alongside the manager wake"
else
  fail "SABLE-nmmh: chuck merge handoff still fires alongside the manager wake" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi

# (b) worker landing, Chuck UNREACHABLE (rc=1): the manager wake still fires, and
# the durable for-chuck fallback bead is still filed (chuck path regression-intact).
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=1 run_hook "$MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -q 'Worker landed' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-nmmh: manager wake fires even when Chuck is unreachable"
else
  fail "SABLE-nmmh: manager wake fires even when Chuck is unreachable" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi
assert_bd_called "SABLE-nmmh: for-chuck fallback bead still filed on landing when Chuck unreachable"

# (c) manager's OWN emergency push (@sable_role=optimus, NOT worker): no
# self-notify, but the Chuck handoff still fires.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV TMUX_PANE=%mgr SABLE_STUB_PANE_ROLE=optimus" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -q 'Worker landed' "$SABLE_MSG_LOG" 2>/dev/null; then
  fail "SABLE-nmmh: manager emergency push does NOT self-notify" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
else
  pass "SABLE-nmmh: manager emergency push does NOT self-notify (no Worker-landed msg)"
fi
if grep -qE '^chuck ' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-nmmh: emergency push still hands off to chuck"
else
  fail "SABLE-nmmh: emergency push still hands off to chuck" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi

# (d) disable knob SABLE_WORKER_LAND_NOTIFY=0: no manager wake even in a worker
# pane; the Chuck handoff is unaffected.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker SABLE_WORKER_LAND_NOTIFY=0" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -q 'Worker landed' "$SABLE_MSG_LOG" 2>/dev/null; then
  fail "SABLE-nmmh: SABLE_WORKER_LAND_NOTIFY=0 suppresses the manager wake" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
else
  pass "SABLE-nmmh: SABLE_WORKER_LAND_NOTIFY=0 suppresses the manager wake"
fi
if grep -qE '^chuck ' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-nmmh: chuck handoff unaffected by SABLE_WORKER_LAND_NOTIFY=0"
else
  fail "SABLE-nmmh: chuck handoff unaffected by SABLE_WORKER_LAND_NOTIFY=0" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi

# --------------------------------------------------------------------------
# SABLE-5hcg (ADDENDUM / HEAD-VERIFY 2026-07-14): the durable fallback path
# must not go dark two ways it used to: (1) 'bd create ... || true' swallowed
# a failed bd invocation on the ONE path that's supposed to be the safety net,
# and (2) an empty diff vs BASE_BRANCH silent-exited before either handoff
# path ran at all. Both must now be loud instead of silent.
# --------------------------------------------------------------------------

# Fault-injectable bd stub for this section only, restored to the plain stub
# at the end. `bd create` behavior is driven by BD_STUB_FAIL_MODE:
#   always_fail — every call exits 1 (models a persistent bd/dolt outage)
#   fail_once   — first call exits 1, second (the retry) succeeds (models a
#                 transient lock/hiccup) — tracked via BD_STUB_COUNT_FILE
#                 since each invocation is a fresh process.
# Any other bd subcommand (e.g. the `list` OVERLAPS query) falls through to
# the same log-and-succeed behavior as the plain stub.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "create" ]; then
  case "${BD_STUB_FAIL_MODE:-}" in
    always_fail)
      echo "bd: stub simulated persistent failure" >&2
      exit 1
      ;;
    fail_once)
      COUNT_FILE="${BD_STUB_COUNT_FILE:?}"
      N=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
      echo "$N" > "$COUNT_FILE"
      if [ "$N" -eq 1 ]; then
        echo "bd: stub simulated transient failure (attempt $N)" >&2
        exit 1
      fi
      ;;
  esac
fi
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

# (a) bd create fails on every attempt — retry doesn't help. The hook must
# surface a loud, non-swallowed diagnostic (not a bare '|| true' silence) and
# still exit 0, since a PostToolUse hook must not fail the triggering push.
# SABLE_MERGE_NOTIFY_VIA_MSG=0 routes straight to the fallback bd create path
# deterministically (same knob exercised in the (c) case above).
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
FIVEHCG_A_OUT=$(run_hook "$MGR_ENV SABLE_MERGE_NOTIFY_VIA_MSG=0 BD_STUB_FAIL_MODE=always_fail" \
  "$(make_post_input "git push" "$FIXTURE_REPO")"); FIVEHCG_A_RC=$?
if [ "$FIVEHCG_A_RC" -eq 0 ]; then
  pass "SABLE-5hcg: hook still exits 0 when bd create fails persistently"
else
  fail "SABLE-5hcg: hook still exits 0 when bd create fails persistently" "exit=$FIVEHCG_A_RC"
fi
if printf '%s' "$FIVEHCG_A_OUT" | grep -qi 'FAILED to file durable for-chuck bead'; then
  pass "SABLE-5hcg: persistent bd create failure is surfaced loudly, not swallowed by '|| true'"
else
  fail "SABLE-5hcg: persistent bd create failure is surfaced loudly, not swallowed by '|| true'" "STDOUT: $FIVEHCG_A_OUT"
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG"

# (b) bd create fails once (transient) then succeeds on retry: the retry
# recovers — for-chuck bead ends up filed, and no FAILED diagnostic prints.
BD_STUB_COUNT_FILE="$STUB_DIR/bd-count-$$"
rm -f "$BD_STUB_COUNT_FILE"
FIVEHCG_B_OUT=$(run_hook "$MGR_ENV SABLE_MERGE_NOTIFY_VIA_MSG=0 BD_STUB_FAIL_MODE=fail_once BD_STUB_COUNT_FILE=$BD_STUB_COUNT_FILE" \
  "$(make_post_input "git push" "$FIXTURE_REPO")")
assert_bd_called "SABLE-5hcg: transient bd create failure recovers via retry — for-chuck bead filed"
if printf '%s' "$FIVEHCG_B_OUT" | grep -qi 'FAILED to file durable'; then
  fail "SABLE-5hcg: retry success does not print a FAILED diagnostic" "STDOUT: $FIVEHCG_B_OUT"
else
  pass "SABLE-5hcg: retry success does not print a FAILED diagnostic"
fi
rm -f "$BD_STUB_COUNT_FILE" "$SABLE_MSG_LOG"

# Restore the plain bd stub for hermeticity for any tests appended later.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

# (c) Empty diff vs BASE_BRANCH: a branch pushed with zero commits ahead of
# its base (confirmable on origin, so it clears the SABLE-b06t ls-remote
# check, but 'git diff BASE...HEAD' comes up empty). Must emit a loud skip
# line instead of the old bare '[ -z "$FILES" ] && exit 0' silent exit, and
# must not file a for-chuck bead (there is genuinely nothing to review).
EMPTYDIFF_BARE=$(mktemp -d)
EMPTYDIFF_REPO=$(mktemp -d)
trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR" "$INT_BARE" "$INT_REPO" "$INTNOTIFY_REPO" "$INTNOTIFY_BARE" "$PZFK_BARE" "$PZFK_REPO" "$B06T_BARE" "$B06T_REPO" "$EMPTYDIFF_BARE" "$EMPTYDIFF_REPO"' EXIT

git init -q --bare "$EMPTYDIFF_BARE"
git clone -q "$EMPTYDIFF_BARE" "$EMPTYDIFF_REPO"
cd_fixture "$EMPTYDIFF_REPO"
git -C "$EMPTYDIFF_REPO" config user.email "ed@ed"; git -C "$EMPTYDIFF_REPO" config user.name "ed"
echo base > base.txt; git add base.txt; git commit -q -m base
EMPTYDIFF_MAIN=$(git symbolic-ref --short HEAD)
git push -q "$EMPTYDIFF_BARE" "HEAD:refs/heads/$EMPTYDIFF_MAIN" 2>/dev/null
git update-ref "refs/remotes/origin/$EMPTYDIFF_MAIN" HEAD
# Also publish under 'main' explicitly so default BASE_BRANCH resolution has
# a real diff target even when the repo's default branch isn't named 'main'
# (mirrors the FIXTURE_REPO/INT_REPO/B06T_REPO fixtures above).
git push -q "$EMPTYDIFF_BARE" HEAD:refs/heads/main 2>/dev/null
git update-ref refs/remotes/origin/main HEAD
# A branch cut from the same commit, with no new commits of its own — the
# diff vs BASE_BRANCH is empty, but the branch itself IS confirmably on
# origin (ls-remote tip matches local HEAD), so it clears every earlier guard.
git checkout -q -b wk-emptydiff
git push -q "$EMPTYDIFF_BARE" HEAD:refs/heads/wk-emptydiff 2>/dev/null
git update-ref refs/remotes/origin/wk-emptydiff HEAD
cd - >/dev/null

rm -f "$BD_LOG" "$SABLE_MSG_LOG"
EMPTYDIFF_INPUT=$(make_post_input "git push" "$EMPTYDIFF_REPO")
CTX_EMPTYDIFF=$(run_hook "$MGR_ENV" "$EMPTYDIFF_INPUT")
if printf '%s' "$CTX_EMPTYDIFF" | grep -qi 'skipping — no file diff'; then
  pass "SABLE-5hcg: empty-diff push emits a loud skip line instead of a silent exit"
else
  fail "SABLE-5hcg: empty-diff push emits a loud skip line instead of a silent exit" "STDOUT: $CTX_EMPTYDIFF"
fi
assert_bd_not_called "SABLE-5hcg: empty-diff push files no for-chuck bead (nothing to review)"

# --------------------------------------------------------------------------
# SABLE-rq9k: the hook's durable for-chuck bead-filing path must perform NO
# Dolt remote sync. bd auto-pushes to the shared Dolt remote on every mutating
# write (create/update/close) unless auto-push is disabled — the global
# --sandbox flag ("Sandbox mode: disables Dolt auto-push", verified present in
# bd 1.0.5). Without it, the fallback `bd create` pushed its for-chuck bead to
# the remote as a pure hook SIDE EFFECT during a fleet-wide push hold (bead
# market-brief-package-1x8v, 2026-07-09) — chuck-only dolt pushing is
# unenforceable by convention alone while any auto-pushing write lives in a hook.
#
# We model bd's push-on-write with a stub that advances a simulated remote tip
# on a mutating `create` UNLESS --sandbox is present, then assert the tip is
# UNCHANGED after the hook files its durable fallback bead. A live dolt
# sql-server + file remote would be the gold-standard fixture, but this whole
# suite is deliberately hermetic (stubbed bd/gh/tmux/sable-msg); modeling the
# documented --sandbox contract keeps the case fast and CI-safe while still
# asserting the observable property: NO remote advance. Red pre-fix (create
# lacks --sandbox → tip advances), green post-fix.
# --------------------------------------------------------------------------

# bd stub modeling Dolt auto-push-on-write: a mutating `create` advances the
# simulated remote tip (append a marker to $DOLT_REMOTE_TIP) UNLESS --sandbox
# disables it. `list` is a read → never pushes, returns an empty in-progress set.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
[ "${1:-}" = "list" ] && { echo '[]'; exit 0; }
if [ "${1:-}" = "create" ]; then
  sandbox=0
  for a in "$@"; do [ "$a" = "--sandbox" ] && sandbox=1; done
  [ "$sandbox" -eq 0 ] && printf 'remote-advanced\n' >> "${DOLT_REMOTE_TIP:-/dev/null}"
fi
exit 0
EOF
chmod +x "$STUB_DIR/bd"

RQ9K_TIP="$STUB_DIR/dolt-remote-tip"
rm -f "$BD_LOG" "$SABLE_MSG_LOG" "$RQ9K_TIP"
: > "$RQ9K_TIP"   # empty remote tip; a push would append to it
# Chuck pane absent → the hook skips the message handoff and takes the durable
# for-chuck `bd create` fallback (the exact leak path). DOLT_REMOTE_TIP is
# exported into the hook env so its `bd create` subprocess reaches the stub.
run_hook "$MGR_ENV SABLE_STUB_CHUCK_PRESENT=0 DOLT_REMOTE_TIP=$RQ9K_TIP" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null

# Precondition: the fallback bead really was filed (so a pass below is not vacuous
# from the hook exiting before bd create).
if grep -q 'for-chuck' "$BD_LOG" 2>/dev/null; then
  pass "SABLE-rq9k: durable for-chuck fallback bead is filed (precondition)"
else
  fail "SABLE-rq9k: durable for-chuck fallback bead is filed (precondition)" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -3)"
fi

# The property that matters: the bead-filing path did NOT advance the Dolt remote.
if [ ! -s "$RQ9K_TIP" ]; then
  pass "SABLE-rq9k: hook bead-filing performs NO dolt remote sync (remote tip unchanged)"
else
  fail "SABLE-rq9k: hook bead-filing performs NO dolt remote sync (remote tip unchanged)" "remote tip advanced — bd create auto-pushed (missing --sandbox): $(cat "$RQ9K_TIP" 2>/dev/null)"
fi

# And the mechanism: the for-chuck create carried the auto-push-disabling flag.
if grep -q -- '--sandbox' "$BD_LOG" 2>/dev/null; then
  pass "SABLE-rq9k: for-chuck bd create invoked with --sandbox (Dolt auto-push disabled)"
else
  fail "SABLE-rq9k: for-chuck bd create invoked with --sandbox (Dolt auto-push disabled)" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null | head -3)"
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG" "$RQ9K_TIP"

# Restore the plain bd stub for hermeticity if more tests are appended later.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

# --------------------------------------------------------------------------
# SABLE-tb1y: the post-push ls-remote confirmation is a TIMING RACE under load.
# After a real push, the hook read origin's tip ONCE (SABLE-b06t) and silently
# `exit 0`'d if REMOTE_TIP != LOCAL_HEAD — correct for an unlanded push, but it
# ALSO fired for a LANDED push whose tip origin had not yet reflected at read
# time. That raced miss was a DOUBLE silent failure: no for-chuck bead AND no
# manager-wake msg (both live below the guard). The fix settles the read with a
# bounded retry loop, and never silent-exits a manager push (loud + traced).
#
# A passthrough `git` stub injects ls-remote lag: for the first
# SABLE_TEST_LSREMOTE_LAG reads it reports the ref absent (exit 2, empty tip —
# models origin not yet reflecting the just-pushed tip), then delegates to real
# git (which returns the matching tip). Everything else execs real git.
# Fast timings via SABLE_PUSH_CONFIRM_SLEEP.
# --------------------------------------------------------------------------

REAL_GIT=$(command -v git)
cat > "$STUB_DIR/git" <<EOF
#!/usr/bin/env bash
# Passthrough git stub with injectable ls-remote lag (SABLE-tb1y).
if { [ "\$1" = "ls-remote" ]; } || { [ "\$1" = "-C" ] && [ "\$3" = "ls-remote" ]; }; then
  if [ -n "\${SABLE_TEST_LSREMOTE_LAG:-}" ] && [ -n "\${SABLE_TEST_LSREMOTE_COUNT_FILE:-}" ]; then
    n=\$(( \$(cat "\$SABLE_TEST_LSREMOTE_COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
    echo "\$n" > "\$SABLE_TEST_LSREMOTE_COUNT_FILE"
    if [ "\$n" -le "\$SABLE_TEST_LSREMOTE_LAG" ]; then
      exit 2   # ref "not yet visible" → hook sees an empty REMOTE_TIP
    fi
  fi
fi
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$STUB_DIR/git"

TB1Y_COUNT="$STUB_DIR/lsremote-count"

# (a) A LANDED push whose origin tip lags the first 2 reads, then settles. With
# the retry budget (default 4 extra tries) the 3rd read matches → the handoff
# fires. Worker pane + Chuck reachable: BOTH the manager-wake msg AND the chuck
# merge msg must fire — the two channels that both went dark in the incident.
# RED on the single-shot guard (first empty read → silent exit, nothing sent).
rm -f "$BD_LOG" "$SABLE_MSG_LOG" "$TB1Y_COUNT"
SABLE_MSG_STUB_RC=0 run_hook \
  "$MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker SABLE_TEST_LSREMOTE_LAG=2 SABLE_TEST_LSREMOTE_COUNT_FILE=$TB1Y_COUNT SABLE_PUSH_CONFIRM_SLEEP=0.02" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -q 'Worker landed' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-tb1y: ls-remote lag then settle — manager wake still fires (not stranded)"
else
  fail "SABLE-tb1y: ls-remote lag then settle — manager wake still fires (not stranded)" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi
if grep -qE '^chuck .*PR ready from optimus' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-tb1y: ls-remote lag then settle — chuck merge handoff still fires (not stranded)"
else
  fail "SABLE-tb1y: ls-remote lag then settle — chuck merge handoff still fires (not stranded)" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi

# (b) Same lag, but Chuck pane ABSENT → the durable for-chuck bead is the
# handoff. The settle must let the hook REACH that bd create instead of silently
# exiting at the confirmation guard (the exact double-silent strand).
rm -f "$BD_LOG" "$SABLE_MSG_LOG" "$TB1Y_COUNT"
run_hook \
  "$MGR_ENV SABLE_STUB_CHUCK_PRESENT=0 SABLE_TEST_LSREMOTE_LAG=2 SABLE_TEST_LSREMOTE_COUNT_FILE=$TB1Y_COUNT SABLE_PUSH_CONFIRM_SLEEP=0.02" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
assert_bd_called "SABLE-tb1y: ls-remote lag then settle — durable for-chuck bead still filed (Chuck absent)"

# (c) A genuinely UNLANDED push (tip never confirmable within the budget). The
# SABLE-b06t guarantee must hold: NO for-chuck bead (chuck must not review
# absent work) — but the skip is now LOUD (deliverable 3), never a silent exit.
# Low retry budget so the "never settles" path is fast.
rm -f "$BD_LOG" "$SABLE_MSG_LOG" "$TB1Y_COUNT"
TB1Y_C_OUT=$(run_hook \
  "$MGR_ENV SABLE_STUB_CHUCK_PRESENT=0 SABLE_TEST_LSREMOTE_LAG=99 SABLE_TEST_LSREMOTE_COUNT_FILE=$TB1Y_COUNT SABLE_PUSH_CONFIRM_RETRIES=2 SABLE_PUSH_CONFIRM_SLEEP=0.02" \
  "$(make_post_input "git push" "$FIXTURE_REPO")")
assert_bd_not_called "SABLE-tb1y: unconfirmed push files NO for-chuck bead (b06t guarantee preserved)"
if printf '%s' "$TB1Y_C_OUT" | grep -qi 'NOT confirmed on origin'; then
  pass "SABLE-tb1y: unconfirmed push emits a LOUD skip line, not a silent exit"
else
  fail "SABLE-tb1y: unconfirmed push emits a LOUD skip line, not a silent exit" "STDOUT: $TB1Y_C_OUT"
fi

# (d) Invocation tracing: a normal confirmed push records INVOKED + CONFIRMED +
# the terminal handoff disposition to the trace log, so a future strand is
# diagnosable even after the pane is reaped.
TB1Y_TRACE="$STUB_DIR/tb1y-trace.log"
rm -f "$BD_LOG" "$SABLE_MSG_LOG" "$TB1Y_COUNT" "$TB1Y_TRACE"
SABLE_MSG_STUB_RC=0 run_hook \
  "$MGR_ENV SABLE_HOOK_TRACE_LOG=$TB1Y_TRACE" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if [ -s "$TB1Y_TRACE" ] && grep -q 'INVOKED' "$TB1Y_TRACE" 2>/dev/null \
   && grep -q 'CONFIRMED' "$TB1Y_TRACE" 2>/dev/null \
   && grep -q 'HANDOFF chuck-msg confirmed' "$TB1Y_TRACE" 2>/dev/null; then
  pass "SABLE-tb1y: invocation trace records INVOKED + CONFIRMED + terminal disposition"
else
  fail "SABLE-tb1y: invocation trace records INVOKED + CONFIRMED + terminal disposition" "TRACE: $(cat "$TB1Y_TRACE" 2>/dev/null)"
fi

# (e) The unconfirmed disposition is also traced (diagnosable strand), and
# SABLE_HOOK_TRACE=0 disables tracing entirely.
rm -f "$TB1Y_TRACE" "$TB1Y_COUNT"
run_hook \
  "$MGR_ENV SABLE_STUB_CHUCK_PRESENT=0 SABLE_TEST_LSREMOTE_LAG=99 SABLE_TEST_LSREMOTE_COUNT_FILE=$TB1Y_COUNT SABLE_PUSH_CONFIRM_RETRIES=1 SABLE_PUSH_CONFIRM_SLEEP=0.02 SABLE_HOOK_TRACE_LOG=$TB1Y_TRACE" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -q 'EXIT unconfirmed' "$TB1Y_TRACE" 2>/dev/null; then
  pass "SABLE-tb1y: unconfirmed disposition is traced (EXIT unconfirmed)"
else
  fail "SABLE-tb1y: unconfirmed disposition is traced (EXIT unconfirmed)" "TRACE: $(cat "$TB1Y_TRACE" 2>/dev/null)"
fi
rm -f "$TB1Y_TRACE"
run_hook \
  "$MGR_ENV SABLE_HOOK_TRACE=0 SABLE_HOOK_TRACE_LOG=$TB1Y_TRACE" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if [ -f "$TB1Y_TRACE" ]; then
  fail "SABLE-tb1y: SABLE_HOOK_TRACE=0 disables tracing" "trace file written despite disable: $(cat "$TB1Y_TRACE" 2>/dev/null)"
else
  pass "SABLE-tb1y: SABLE_HOOK_TRACE=0 disables tracing (no trace file)"
fi

# Remove the passthrough git stub so later fixtures/tests use real git directly.
rm -f "$STUB_DIR/git" "$TB1Y_COUNT" "$TB1Y_TRACE"

# --------------------------------------------------------------------------
# SABLE-f916: the auto-notify landing artifacts (live chuck message AND the
# durable for-chuck bead fallback) must self-label as auto-detected so Chuck
# can mechanically tell them apart from a manager's deliberate, reviewed
# PR-ready sign-off — which carries no such label. Incident 2026-07-15: an
# auto-notify for wk-bin-symlink-parity (SABLE-59t6.6) was queued+inspected
# as if PR-ready, but optimus had never accepted it (later rejected for
# false-green tests), because the two framings were byte-identical.
# --------------------------------------------------------------------------

AUTO_NOTIFY_MARKER="AUTO-NOTIFY"

# (a) Live chuck message (message-first handoff, Chuck reachable): carries
# the auto-notify marker.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -qE "^chuck .*${AUTO_NOTIFY_MARKER}.*PR ready from optimus" "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-f916: live chuck handoff message self-labels as auto-notify"
else
  fail "SABLE-f916: live chuck handoff message self-labels as auto-notify" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi

# (b) Durable for-chuck bead fallback (Chuck unreachable): both the bead
# title and description carry the same auto-notify marker, so the fallback
# path is just as distinguishable as the live-message path.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
run_hook "$MGR_ENV SABLE_STUB_CHUCK_PRESENT=0" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -q "for-chuck" "$BD_LOG" 2>/dev/null \
   && grep -qF -- "--title [${AUTO_NOTIFY_MARKER}]" "$BD_LOG" 2>/dev/null \
   && grep -q "NOT a manager sign-off" "$BD_LOG" 2>/dev/null; then
  pass "SABLE-f916: durable for-chuck bead fallback self-labels as auto-notify (title + description)"
else
  fail "SABLE-f916: durable for-chuck bead fallback self-labels as auto-notify (title + description)" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null)"
fi

# (c) The two framings are MECHANICALLY distinguishable: a manager's
# deliberate, reviewed sign-off (typed directly, never routed through this
# hook) carries no auto-notify marker — so a downstream consumer (Chuck) can
# grep for the marker's ABSENCE to recognize a real sign-off, and its
# PRESENCE to recognize an unreviewed auto-detected push.
MANUAL_SIGNOFF="PR ready from optimus: branch wk-foo (a.py b.py). Reviewed and accepted — merge into the integration branch."
if printf '%s' "$MANUAL_SIGNOFF" | grep -q "${AUTO_NOTIFY_MARKER}"; then
  fail "SABLE-f916: a manager's manual sign-off carries no auto-notify marker (framings distinguishable)" "unexpectedly matched: $MANUAL_SIGNOFF"
else
  pass "SABLE-f916: a manager's manual sign-off carries no auto-notify marker (framings distinguishable)"
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG"

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
