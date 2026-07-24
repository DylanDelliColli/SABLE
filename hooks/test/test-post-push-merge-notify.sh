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
# SABLE-xuxx: a LEAKED SABLE_BASE_BRANCH that actually EXISTS in this repo
# (origin/main — present in nearly every repo) must NOT override a PUBLISHED
# integration branch (origin/tmux-only). The SABLE-cstk test above only
# covers a leaked value that does NOT exist (origin/llm-integration), so the
# old exists-check (`git rev-parse --verify --quiet "$SABLE_BASE_BRANCH"`)
# never got exercised on its true-branch: honoring SABLE_BASE_BRANCH whenever
# it resolves, even over a published origin/<INT>. Reusing INTNOTIFY_REPO
# (origin/main has base.txt only; tmux-only adds i1.txt; wk-other branches off
# tmux-only adding w1.txt) — pre-fix, SABLE_BASE_BRANCH=origin/main EXISTS so
# it wins, diffing origin/main...wk-other and leaking i1.txt (tmux-only's own
# history) into the file list alongside the real w1.txt change.
# --------------------------------------------------------------------------
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
XUXX_INPUT=$(make_post_input "git push origin wk-other" "$INTNOTIFY_REPO")
run_hook "$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_MERGE_NOTIFY_VIA_MSG=0" "$XUXX_INPUT" >/dev/null
if grep -q 'w1.txt' "$BD_LOG" 2>/dev/null; then
  pass "SABLE-xuxx: leaked-but-existing SABLE_BASE_BRANCH=origin/main — FILES list still includes the real change (w1.txt)"
else
  fail "SABLE-xuxx: leaked-but-existing SABLE_BASE_BRANCH=origin/main — FILES list still includes the real change (w1.txt)" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null)"
fi
if grep -q 'i1.txt' "$BD_LOG" 2>/dev/null; then
  fail "SABLE-xuxx: leaked-but-existing SABLE_BASE_BRANCH=origin/main does NOT override published origin/tmux-only (no i1.txt leak)" "BD_LOG: $(cat "$BD_LOG" 2>/dev/null)"
else
  pass "SABLE-xuxx: leaked-but-existing SABLE_BASE_BRANCH=origin/main does NOT override published origin/tmux-only (no i1.txt leak)"
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
# in-progress bead claiming doc1.txt via wip_claims metadata (SABLE-szd: NOT
# notes) — a file that's part of the PHANTOM origin/main diff but NOT the
# worker's real diff. Overlap must NOT fire on it once the base ref is fixed.
# `create` still logs to BD_LOG.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list" ]; then
  cat <<'JSON'
[{"id": "SABLE-fakein", "title": "unrelated in-progress work", "assignee": "someone", "metadata": {"wip_claims": "doc1.txt"}}]
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
# manager (optimus) is messaged with an AUTO-NOTIFY wake, --from worker; the
# Chuck merge handoff still fires alongside it. SABLE-gx7p3: the wording is no
# longer a fixed "Worker landed ... bead closed" string (that was the false
# terminal claim this bead fixes) — with the plain bd stub returning no
# metadata match, the bead status resolves unknown, so the message reads
# "Worker pushed ... status: unknown". The wake-fired assertion below checks
# for the AUTO-NOTIFY tag reaching optimus rather than the old fixed wording.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -qE '^optimus .*\[AUTO-NOTIFY' "$SABLE_MSG_LOG" 2>/dev/null \
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
if grep -qE '^optimus .*\[AUTO-NOTIFY' "$SABLE_MSG_LOG" 2>/dev/null; then
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
if grep -qE '^optimus ' "$SABLE_MSG_LOG" 2>/dev/null; then
  fail "SABLE-nmmh: manager emergency push does NOT self-notify" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
else
  pass "SABLE-nmmh: manager emergency push does NOT self-notify (no worker-landing wake msg)"
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
if grep -qE '^optimus ' "$SABLE_MSG_LOG" 2>/dev/null; then
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
# Passthrough git stub with injectable ls-remote lag (SABLE-tb1y) and an
# injectable ls-remote HANG (SABLE-27r3: a stalled GitHub SSH connection
# never returns at all, as opposed to the lag case above which returns late
# but promptly). SABLE_TEST_LSREMOTE_SLEEP sleeps far longer than any
# per-attempt timeout under test, so the assertion is on the hook's \`timeout\`
# wrapper actually cutting the hang short — not on this stub ever completing.
if { [ "\$1" = "ls-remote" ]; } || { [ "\$1" = "-C" ] && [ "\$3" = "ls-remote" ]; }; then
  if [ -n "\${SABLE_TEST_LSREMOTE_SLEEP:-}" ]; then
    # exec (not a forked child) so the process `timeout` signals IS the sleep
    # itself — no orphaned grandchild that could outlive the signal and keep
    # this call's output pipe open past the timeout bound.
    exec sleep "\$SABLE_TEST_LSREMOTE_SLEEP"
  fi
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
if grep -qE '^optimus .*\[AUTO-NOTIFY' "$SABLE_MSG_LOG" 2>/dev/null; then
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

# --------------------------------------------------------------------------
# SABLE-27r3: a HUNG ls-remote (stalled GitHub SSH, as opposed to the
# lag-then-settle case above) must not block the hook for the whole retry
# budget. Each ls-remote read is now wrapped in `timeout`, tunable via
# SABLE_LSREMOTE_TIMEOUT. The stub's ls-remote sleeps far longer (30s) than
# the 1s per-attempt timeout under test, across both of the 2 allowed
# attempts (SABLE_PUSH_CONFIRM_RETRIES=1) — if the timeout wrapper is
# missing/ineffective this test would hang for ~30s+ instead of finishing in
# ~2s, and the wall-clock assertion below catches that regression directly.
# --------------------------------------------------------------------------
rm -f "$BD_LOG" "$SABLE_MSG_LOG" "$TB1Y_COUNT"
SECONDS=0
TIMEOUT_OUT=$(run_hook \
  "$MGR_ENV SABLE_STUB_CHUCK_PRESENT=0 SABLE_TEST_LSREMOTE_SLEEP=30 SABLE_LSREMOTE_TIMEOUT=1 SABLE_PUSH_CONFIRM_RETRIES=1 SABLE_PUSH_CONFIRM_SLEEP=0.01" \
  "$(make_post_input "git push" "$FIXTURE_REPO")")
TIMEOUT_ELAPSED=$SECONDS
if [ "$TIMEOUT_ELAPSED" -le 10 ]; then
  pass "SABLE-27r3: hung ls-remote is bounded by timeout — hook returns in ${TIMEOUT_ELAPSED}s, not the ~30s hang"
else
  fail "SABLE-27r3: hung ls-remote is bounded by timeout — hook returns in ${TIMEOUT_ELAPSED}s, not the ~30s hang" "OUTPUT: $TIMEOUT_OUT"
fi
assert_bd_not_called "SABLE-27r3: hung ls-remote skips (no notify) rather than reviewing unconfirmed work"
if printf '%s' "$TIMEOUT_OUT" | grep -qi 'NOT confirmed on origin'; then
  pass "SABLE-27r3: hung ls-remote skip is LOUD, not silent"
else
  fail "SABLE-27r3: hung ls-remote skip is LOUD, not silent" "OUTPUT: $TIMEOUT_OUT"
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
# SABLE-riu: fallback-path idempotency. A repeated push of the SAME branch
# during a chuck-down/unreachable window (message-first handoff never
# confirms) must file ONLY ONE for-chuck bead, not a new near-identical bead
# per push. Mirrors bin/sable-reconcile-handoffs's title_names_branch
# predicate. clean-room: this section only shells out to the STUB bd (never
# the real bd binary), so it needs no HAVE_BD/command-v self-skip.
# --------------------------------------------------------------------------

RIU_FIXTURE_BRANCH=""

# (a) An existing open for-chuck bead already names this exact branch (title
# 'wk-riu-dup') — the fallback create must be SKIPPED and the hook must still
# exit 0.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list" ]; then
  cat <<'JSON'
[{"id": "SABLE-existing", "title": "[AUTO-NOTIFY] Review PR from optimus: wk-riu-dup", "status": "open"}]
JSON
  exit 0
fi
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

RIU_BARE=$(mktemp -d)
RIU_REPO=$(mktemp -d)
trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR" "$INT_BARE" "$INT_REPO" "$INTNOTIFY_REPO" "$INTNOTIFY_BARE" "$PZFK_BARE" "$PZFK_REPO" "$B06T_BARE" "$B06T_REPO" "$RIU_BARE" "$RIU_REPO"' EXIT

git init -q --bare "$RIU_BARE"
git clone -q "$RIU_BARE" "$RIU_REPO"
cd_fixture "$RIU_REPO"
git -C "$RIU_REPO" config user.email "riu@riu"; git -C "$RIU_REPO" config user.name "riu"
echo base > base.txt; git add base.txt; git commit -q -m base
git push -q "$RIU_BARE" HEAD:refs/heads/main 2>/dev/null
git update-ref refs/remotes/origin/main HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
git checkout -q -b wk-riu-dup
echo change > riu_change.txt; git add riu_change.txt; git commit -q -m "worker: riu change"
git push -q "$RIU_BARE" HEAD:refs/heads/wk-riu-dup 2>/dev/null
git update-ref refs/remotes/origin/wk-riu-dup HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
cd - >/dev/null

rm -f "$BD_LOG" "$SABLE_MSG_LOG"
RIU_INPUT_A=$(make_post_input "git push" "$RIU_REPO")
RIU_OUT_A=$(run_hook "$MGR_ENV" "$RIU_INPUT_A")
assert_bd_not_called "SABLE-riu: existing open for-chuck bead naming the branch → no duplicate bd create"
if printf '%s' "$RIU_OUT_A" | grep -qi 'not filing a duplicate'; then
  pass "SABLE-riu: skip is reported with a loud context line, not silent"
else
  fail "SABLE-riu: skip is reported with a loud context line, not silent" "OUTPUT: $RIU_OUT_A"
fi

# (b) Token-boundary match, not substring: pushing 'wk-riu-short' while the
# only existing for-chuck bead names the LONGER branch 'wk-riu-short-extra'
# (which contains 'wk-riu-short' as a bare prefix, no delimiter after it)
# must NOT be treated as already-named — mirrors the fix spec's own example
# ('wk-foo' must not match a title naming 'wk-foobar'). bd create must fire.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list" ]; then
  cat <<'JSON'
[{"id": "SABLE-existing", "title": "[AUTO-NOTIFY] Review PR from optimus: wk-riu-short-extra", "status": "open"}]
JSON
  exit 0
fi
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

cd_fixture "$RIU_REPO"
git checkout -q -b wk-riu-short origin/main
echo change2 > riu_change2.txt; git add riu_change2.txt; git commit -q -m "worker: riu change short"
git push -q "$RIU_BARE" HEAD:refs/heads/wk-riu-short 2>/dev/null
git update-ref refs/remotes/origin/wk-riu-short HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
cd - >/dev/null

rm -f "$BD_LOG" "$SABLE_MSG_LOG"
RIU_INPUT_B=$(make_post_input "git push" "$RIU_REPO")
run_hook "$MGR_ENV" "$RIU_INPUT_B" >/dev/null
assert_bd_called "SABLE-riu: a shorter branch is NOT treated as a duplicate of a longer branch's title (token-boundary, not substring)"

# (c) No existing for-chuck bead names the branch → files exactly one.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list" ]; then
  echo "[]"
  exit 0
fi
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

rm -f "$BD_LOG" "$SABLE_MSG_LOG"
RIU_INPUT_C=$(make_post_input "git push" "$RIU_REPO")
run_hook "$MGR_ENV" "$RIU_INPUT_C" >/dev/null
RIU_CREATE_COUNT=$(grep -c 'for-chuck' "$BD_LOG" 2>/dev/null || echo 0)
if [ "$RIU_CREATE_COUNT" -eq 1 ]; then
  pass "SABLE-riu: no existing for-chuck bead names the branch → exactly one bd create"
else
  fail "SABLE-riu: no existing for-chuck bead names the branch → exactly one bd create" "count=$RIU_CREATE_COUNT BD_LOG: $(cat "$BD_LOG" 2>/dev/null)"
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG"

# (d) Integration: a STATEFUL bd stub that behaves like a real bead store —
# 'create' appends a bead whose title embeds the branch; 'list' returns
# whatever is currently stored. Two consecutive fallback-path invocations for
# the SAME branch must result in exactly ONE for-chuck bead on record.
RIU_STATE="$STUB_DIR/riu-bead-state.json"
echo "[]" > "$RIU_STATE"
RIU_STATE_PY="$STUB_DIR/riu_bd_stub.py"
cat > "$RIU_STATE_PY" <<'PYEOF'
import json, os, sys

state_path = os.environ["RIU_STATE"]
try:
    with open(state_path) as f:
        beads = json.load(f)
except Exception:
    beads = []

args = sys.argv[1:]
if args and args[0] == "list":
    print(json.dumps(beads))
    sys.exit(0)
if args and args[0] == "create":
    title = ""
    for i, a in enumerate(args):
        if a == "--title" and i + 1 < len(args):
            title = args[i + 1]
    beads.append({"id": f"SABLE-riu{len(beads)}", "title": title, "status": "open"})
    with open(state_path, "w") as f:
        json.dump(beads, f)
    print(f"created SABLE-riu{len(beads) - 1}")
    sys.exit(0)
sys.exit(0)
PYEOF
cat > "$STUB_DIR/bd" <<EOF
#!/usr/bin/env bash
# RIU_STATE is baked in at stub-creation time (not passed through run_hook's
# env -i) so the state file path survives the hermetic env wipe.
RIU_STATE="$RIU_STATE" exec python3 "$RIU_STATE_PY" "\$@"
EOF
chmod +x "$STUB_DIR/bd"

cd_fixture "$RIU_REPO"
git checkout -q wk-riu-dup
echo change3 >> riu_change.txt; git add riu_change.txt; git commit -q -m "worker: riu change v2"
git push -q "$RIU_BARE" HEAD:refs/heads/wk-riu-dup 2>/dev/null
git update-ref refs/remotes/origin/wk-riu-dup HEAD  # SABLE-ck05: mirror the tracking-ref update a named-remote push does automatically
cd - >/dev/null

RIU_INPUT_D1=$(make_post_input "git push" "$RIU_REPO")
run_hook "$MGR_ENV" "$RIU_INPUT_D1" >/dev/null
RIU_INPUT_D2=$(make_post_input "git push" "$RIU_REPO")
run_hook "$MGR_ENV" "$RIU_INPUT_D2" >/dev/null

RIU_FINAL_COUNT=$(python3 -c "import json; print(len(json.load(open('$RIU_STATE'))))")
if [ "$RIU_FINAL_COUNT" -eq 1 ]; then
  pass "SABLE-riu integration: two consecutive fallback-path invocations for one branch → exactly ONE for-chuck bead"
else
  fail "SABLE-riu integration: two consecutive fallback-path invocations for one branch → exactly ONE for-chuck bead" "count=$RIU_FINAL_COUNT STATE: $(cat "$RIU_STATE" 2>/dev/null)"
fi

rm -f "$BD_LOG" "$SABLE_MSG_LOG" "$RIU_STATE" "$RIU_STATE_PY"

# Restore the plain bd stub for hermeticity if more tests are appended later.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

# --------------------------------------------------------------------------
# SABLE-gx7p3: the worker-landing notify must state ONLY what the hook
# observed (a push), never assert "bead closed"/"closed bead + for-chuck PR"
# unless the branch's work bead is ACTUALLY closed. Every auto-notify must
# carry the AUTO-NOTIFY tag regardless of recipient (it previously reached
# the manager untagged while the chuck-facing message already carried it).
#
# UNIT fixture: a dedicated bd stub models the `bd list --status all
# --metadata-field branch=<b> --json` resolver query directly (isolating the
# message-rendering logic from real bd/dolt state), driven by
# $UNIT_BEAD_STATUS (single bead) or $UNIT_BEAD_BUNDLE_FILE (a path to a
# literal JSON array, for the bundled-dispatch cardinality cases below — a
# FILE rather than an inline env value because run_hook's env_prefix is
# word-split unquoted, which would corrupt a JSON value containing spaces).
# The OVERLAPS query (`bd list --status=in_progress --json`, no
# metadata-field arg) still returns `[]` through the same stub.
# --------------------------------------------------------------------------

cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list" ]; then
  for a in "$@"; do
    case "$a" in
      branch=*)
        if [ -n "${UNIT_BEAD_BUNDLE_FILE:-}" ] && [ -f "${UNIT_BEAD_BUNDLE_FILE}" ]; then
          cat "$UNIT_BEAD_BUNDLE_FILE"
        else
          printf '[{"id": "%s", "status": "%s"}]\n' "${UNIT_BEAD_ID:-SABLE-gxunit}" "${UNIT_BEAD_STATUS:-in_progress}"
        fi
        exit 0
        ;;
    esac
  done
  echo '[]'
  exit 0
fi
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

# UNIT (a): bead is in_progress. NEGATIVE assertion — the notify must NOT
# assert closure, but MUST carry the AUTO-NOTIFY tag (both halves of the
# acceptance criteria).
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker UNIT_BEAD_STATUS=in_progress UNIT_BEAD_ID=SABLE-gxunit" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -qE '^optimus .*\[AUTO-NOTIFY' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "UNIT SABLE-gx7p3: in_progress bead — notify carries the AUTO-NOTIFY tag"
else
  fail "UNIT SABLE-gx7p3: in_progress bead — notify carries the AUTO-NOTIFY tag" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi
if grep -qiE 'bead closed|closed bead' "$SABLE_MSG_LOG" 2>/dev/null; then
  fail "UNIT SABLE-gx7p3: in_progress bead — notify must NOT assert closure" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
else
  pass "UNIT SABLE-gx7p3: in_progress bead — notify does NOT assert closure"
fi
if grep -q 'SABLE-gxunit' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "UNIT SABLE-gx7p3: notify names the resolved bead id"
else
  fail "UNIT SABLE-gx7p3: notify names the resolved bead id" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi

# UNIT (b) — NEGATIVE CONTROL: a genuinely closed bead. Without this control,
# a "fix" that simply strips all closure wording (rather than reporting real
# status) would pass (a) above while making the notify permanently useless —
# unable to ever say a worker finished.
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker UNIT_BEAD_STATUS=closed UNIT_BEAD_ID=SABLE-gxunit" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -qE '^optimus .*\[AUTO-NOTIFY' "$SABLE_MSG_LOG" 2>/dev/null \
   && grep -qiE 'closed bead' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "UNIT SABLE-gx7p3 NEGATIVE CONTROL: genuinely closed bead — notify may state closure, still tagged"
else
  fail "UNIT SABLE-gx7p3 NEGATIVE CONTROL: genuinely closed bead — notify may state closure, still tagged" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi

rm -f "$BD_LOG" "$SABLE_MSG_LOG"

# UNIT (c) — CARDINALITY / bundled dispatch (second live instance, optimus's
# lane: SABLE-dhcyu still in_progress inside a bundle where a sibling bead on
# the SAME branch had already closed). Two beads share this branch's
# metadata; ONE is closed, ONE is in_progress. The notify must NOT render a
# singular closure claim for the unit of work — a fix that resolves only the
# first-matched bead's status would wrongly say "bead closed" here.
GX_BUNDLE_PARTIAL="$STUB_DIR/bundle-partial.json"
cat > "$GX_BUNDLE_PARTIAL" <<'JSON'
[{"id": "SABLE-dhcyu-a", "status": "closed"}, {"id": "SABLE-dhcyu-b", "status": "in_progress"}]
JSON
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker UNIT_BEAD_BUNDLE_FILE=$GX_BUNDLE_PARTIAL" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -qiE 'bead closed|closed bead|ALL 2 beads' "$SABLE_MSG_LOG" 2>/dev/null; then
  fail "UNIT SABLE-gx7p3 CARDINALITY: partial bundle (1/2 closed) must NOT assert a singular/all-closed claim" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
else
  pass "UNIT SABLE-gx7p3 CARDINALITY: partial bundle (1/2 closed) does NOT assert closure for the unit of work"
fi
if grep -qE '^optimus .*\[AUTO-NOTIFY' "$SABLE_MSG_LOG" 2>/dev/null \
   && grep -q 'SABLE-dhcyu-b' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "UNIT SABLE-gx7p3 CARDINALITY: partial bundle notify names the still-open sibling bead"
else
  fail "UNIT SABLE-gx7p3 CARDINALITY: partial bundle notify names the still-open sibling bead" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi

# UNIT (d) — bundle NEGATIVE CONTROL: ALL beads sharing the branch ARE
# closed. Without this, a fix that never allows a bundle to report closure
# would pass (c) while making the notify unable to ever report a genuinely
# finished bundled dispatch.
GX_BUNDLE_FULL="$STUB_DIR/bundle-full.json"
cat > "$GX_BUNDLE_FULL" <<'JSON'
[{"id": "SABLE-dhcyu-a", "status": "closed"}, {"id": "SABLE-dhcyu-b", "status": "closed"}]
JSON
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker UNIT_BEAD_BUNDLE_FILE=$GX_BUNDLE_FULL" \
  "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null
if grep -qE '^optimus .*\[AUTO-NOTIFY' "$SABLE_MSG_LOG" 2>/dev/null \
   && grep -qiE 'ALL 2 beads.*CLOSED' "$SABLE_MSG_LOG" 2>/dev/null \
   && grep -q 'SABLE-dhcyu-a' "$SABLE_MSG_LOG" 2>/dev/null \
   && grep -q 'SABLE-dhcyu-b' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "UNIT SABLE-gx7p3 CARDINALITY NEGATIVE CONTROL: fully-closed bundle (2/2) — notify states closure for both members"
else
  fail "UNIT SABLE-gx7p3 CARDINALITY NEGATIVE CONTROL: fully-closed bundle (2/2) — notify states closure for both members" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi

rm -f "$BD_LOG" "$SABLE_MSG_LOG"

# --------------------------------------------------------------------------
# SABLE-gx7p3 PLANT-AND-FAIL (SABLE-5lli.7 pattern, mirroring
# hooks/test/test-require-all.sh's own plant): reconstruct the EXACT pre-fix
# LAND_MSG shape — unconditional "pushed & bead closed", no AUTO-NOTIFY tag —
# as a mutated copy of the real hook script, run it against the SAME
# in_progress-bead fixture as UNIT (a) above, and assert BOTH of that test's
# assertions correctly flag it as wrong. This proves the assertions are
# load-bearing: they distinguish the fixed behavior from the actual defect
# being fixed, not merely from an empty string.
# --------------------------------------------------------------------------
# The hook sources sibling libs via `dirname "${BASH_SOURCE[0]}"`
# (lib-hook-trace.sh, lib-identity.sh), and lib-identity.sh itself sources
# further siblings (e.g. lib-mode-path.sh) the SAME way — so symlinking only
# the two directly-sourced libs still fails one level down (confirmed: "line
# 47: .../lib-mode-path.sh: No such file or directory") because a sourced
# symlink's BASH_SOURCE dirname resolves to the symlink's OWN directory, not
# its target's. Symlinking every sibling *.sh into one dedicated subdir
# handles the transitive chain at any depth. A dedicated subdir keeps these
# symlinks out of STUB_DIR's own bd/tmux/sable-msg stub lookups. Any
# sourcing failure here would silently exit before ever composing LAND_MSG,
# which would make the plant's negative-tag check "pass" for the wrong
# reason — hence checking it actually ran below.
PLANT_DIR="$STUB_DIR/plant-gx7p3"
mkdir -p "$PLANT_DIR"
for f in "$LIB_DIR"/*.sh; do
  ln -sf "$f" "$PLANT_DIR/$(basename "$f")"
done
MUTATED_HOOK="$PLANT_DIR/post-push-merge-notify-PLANT.sh"
# Marker-based whole-block replace (not a fragile regex over the block's
# internals — the block's own shape changes as this hook evolves; the START
# marker is unique to the resolution block, and the END marker is the
# OVERLAPS line that unconditionally follows it, so this stays correct
# across future edits to what's IN BETWEEN).
PLANT_SETUP_ERR=$(python3 - "$HOOK" "$MUTATED_HOOK" <<'PYEOF'
import sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = open(src_path).read()
start_marker = '    BEAD_ID=""\n'
end_marker = '\n    [ -n "$OVERLAPS" ] && LAND_MSG='
start = src.find(start_marker)
end = src.find(end_marker)
if start == -1 or end == -1 or end < start:
    print(f"could not locate resolution block: start={start} end={end}", file=sys.stderr)
    sys.exit(1)
reverted = ('    LAND_MSG="Worker landed: branch ${BRANCH} (${FILES_BRIEF}) pushed & '
            'bead closed. Review the outcome — closed bead + for-chuck PR — and '
            'REVISE by re-spawning into the same worktree if wrong."')
new_src = src[:start] + reverted + src[end:]
open(dst_path, "w").write(new_src)
PYEOF
)
PLANT_SETUP_RC=$?
chmod +x "$MUTATED_HOOK" 2>/dev/null || true

if [ "$PLANT_SETUP_RC" -ne 0 ] || [ ! -s "$MUTATED_HOOK" ]; then
  fail "PLANT-AND-FAIL SABLE-gx7p3: could not construct the reverted mutant hook" "rc=$PLANT_SETUP_RC err=$PLANT_SETUP_ERR"
else
  rm -f "$BD_LOG" "$SABLE_MSG_LOG"
  env -i PATH="$STUB_DIR:$PATH" BD_LOG="$BD_LOG" \
    SABLE_MSG_LOG="$SABLE_MSG_LOG" SABLE_MSG_STUB_RC=0 \
    SABLE_HOOK_TRACE_LOG="$STUB_DIR/hook-trace-plant.log" \
    $MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker UNIT_BEAD_STATUS=in_progress UNIT_BEAD_ID=SABLE-gxunit \
    bash "$MUTATED_HOOK" <<< "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null 2>&1

  # Non-vacuity precondition: confirm the mutant actually ran past the push
  # confirmation and reached the worker-landing block, rather than exiting
  # early on a sourcing/env error and leaving both checks below vacuously
  # "pass" for the wrong reason (this is exactly the empty-MSG_LOG failure
  # mode hit while writing this plant, caused by a transitive sibling-lib
  # sourcing gap — see the PLANT_DIR symlink comment above).
  if grep -q 'CONFIRMED' "$STUB_DIR/hook-trace-plant.log" 2>/dev/null; then
    pass "PLANT-AND-FAIL SABLE-gx7p3: mutant hook actually ran (non-vacuity precondition)"
  else
    fail "PLANT-AND-FAIL SABLE-gx7p3: mutant hook actually ran (non-vacuity precondition)" "hook-trace-plant.log: $(cat "$STUB_DIR/hook-trace-plant.log" 2>/dev/null)"
  fi
  if grep -qiE 'bead closed|closed bead' "$SABLE_MSG_LOG" 2>/dev/null; then
    pass "PLANT-AND-FAIL SABLE-gx7p3: reverted wording DOES assert closure for an in_progress bead (proves the 'no closure claim' assertion is load-bearing)"
  else
    fail "PLANT-AND-FAIL SABLE-gx7p3: reverted wording DOES assert closure for an in_progress bead" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
  fi
  if grep -qE '^optimus .*\[AUTO-NOTIFY' "$SABLE_MSG_LOG" 2>/dev/null; then
    fail "PLANT-AND-FAIL SABLE-gx7p3: reverted wording correctly lacks the AUTO-NOTIFY tag" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
  else
    pass "PLANT-AND-FAIL SABLE-gx7p3: reverted wording correctly lacks the AUTO-NOTIFY tag (proves the tag assertion is load-bearing)"
  fi
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG"

# --------------------------------------------------------------------------
# SABLE-gx7p3 PLANT-AND-FAIL #2 — CARDINALITY regression (the SECOND live
# instance, optimus/tarzan's bundled-dispatch report: even a fix that
# correctly checks bead status can still assert a false singular closure if
# it only looks at the FIRST bead sharing a branch's metadata). Mutate the
# CURRENT (fixed) resolution block back to that naive first-match shape —
# this hook's own earlier, less-robust draft — and confirm it WRONGLY
# asserts closure against the partial-bundle fixture (bundle-partial.json:
# first bead closed, second still in_progress) that UNIT (c) above correctly
# refuses. Proves the "ALL members must be closed" requirement is
# load-bearing, not merely untested.
# --------------------------------------------------------------------------
CARDINALITY_MUTANT="$PLANT_DIR/post-push-merge-notify-CARD-PLANT.sh"
CARD_SETUP_ERR=$(python3 - "$HOOK" "$CARDINALITY_MUTANT" <<'PYEOF'
import sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = open(src_path).read()
start_marker = '    BEAD_ID=""\n'
end_marker = '\n    [ -n "$OVERLAPS" ] && LAND_MSG='
start = src.find(start_marker)
end = src.find(end_marker)
if start == -1 or end == -1 or end < start:
    print(f"could not locate resolution block: start={start} end={end}", file=sys.stderr)
    sys.exit(1)
# The naive, pre-cardinality-fix shape: take only the FIRST bead matching
# the branch metadata and break — reproduces the exact regression a
# bundled dispatch exposes.
naive = '''    BEAD_ID=""
    BEAD_STATUS=""
    BEAD_QUERY=$(bd list --status all --metadata-field "branch=$BRANCH" --json 2>/dev/null || echo "")
    if [ -n "$BEAD_QUERY" ]; then
      BEAD_INFO=$(printf '%s' "$BEAD_QUERY" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = []
if isinstance(data, list):
    for item in data:
        if isinstance(item, dict) and item.get('status'):
            print(f\\"{item.get('id', '')}\\t{item.get('status', '')}\\")
            break
" 2>/dev/null) || BEAD_INFO=""
      BEAD_ID=$(printf '%s' "$BEAD_INFO" | cut -f1)
      BEAD_STATUS=$(printf '%s' "$BEAD_INFO" | cut -f2)
    fi
    if [ "$BEAD_STATUS" = "closed" ]; then
      LAND_MSG="${AUTO_NOTIFY_TAG} Worker landed: branch ${BRANCH} (${FILES_BRIEF}) pushed; bead ${BEAD_ID:-?} is CLOSED. Review the outcome — closed bead + for-chuck PR — and REVISE by re-spawning into the same worktree if wrong."
    else
      LAND_MSG="${AUTO_NOTIFY_TAG} Worker pushed: branch ${BRANCH} (${FILES_BRIEF}). Bead ${BEAD_ID:-<unresolved>} status: ${BEAD_STATUS:-unknown} — the worker may still be running. This is NOT a completion signal; check \\`bd show ${BEAD_ID:-<bead>}\\` and \\`sable-worker-status\\` before reviewing."
    fi'''
new_src = src[:start] + naive + src[end:]
open(dst_path, "w").write(new_src)
PYEOF
)
CARD_SETUP_RC=$?
chmod +x "$CARDINALITY_MUTANT" 2>/dev/null || true

if [ "$CARD_SETUP_RC" -ne 0 ] || [ ! -s "$CARDINALITY_MUTANT" ]; then
  fail "PLANT-AND-FAIL SABLE-gx7p3 CARDINALITY: could not construct the naive-resolver mutant hook" "rc=$CARD_SETUP_RC err=$CARD_SETUP_ERR"
else
  rm -f "$BD_LOG" "$SABLE_MSG_LOG"
  env -i PATH="$STUB_DIR:$PATH" BD_LOG="$BD_LOG" \
    SABLE_MSG_LOG="$SABLE_MSG_LOG" SABLE_MSG_STUB_RC=0 \
    SABLE_HOOK_TRACE_LOG="$STUB_DIR/hook-trace-plant-card.log" \
    $MGR_ENV TMUX_PANE=%worker SABLE_STUB_PANE_ROLE=worker UNIT_BEAD_BUNDLE_FILE=$GX_BUNDLE_PARTIAL \
    bash "$CARDINALITY_MUTANT" <<< "$(make_post_input "git push" "$FIXTURE_REPO")" >/dev/null 2>&1

  if grep -q 'CONFIRMED' "$STUB_DIR/hook-trace-plant-card.log" 2>/dev/null; then
    pass "PLANT-AND-FAIL SABLE-gx7p3 CARDINALITY: naive-resolver mutant actually ran (non-vacuity precondition)"
  else
    fail "PLANT-AND-FAIL SABLE-gx7p3 CARDINALITY: naive-resolver mutant actually ran (non-vacuity precondition)" "hook-trace-plant-card.log: $(cat "$STUB_DIR/hook-trace-plant-card.log" 2>/dev/null)"
  fi
  if grep -qiE 'bead closed|closed bead' "$SABLE_MSG_LOG" 2>/dev/null; then
    pass "PLANT-AND-FAIL SABLE-gx7p3 CARDINALITY: naive first-match resolver WRONGLY asserts closure on a partial bundle (proves UNIT (c)'s all-members check is load-bearing)"
  else
    fail "PLANT-AND-FAIL SABLE-gx7p3 CARDINALITY: naive first-match resolver WRONGLY asserts closure on a partial bundle" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
  fi
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
rm -rf "$PLANT_DIR"

# Restore the plain bd stub for hermeticity if more tests are appended later.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

# --------------------------------------------------------------------------
# INTEGRATION SABLE-gx7p3 — real bd in the project repo, real hook invocation,
# no mocks of bd. Creates a scratch bead in the real, shared project Dolt db
# (--sandbox on every write, never pushed to the shared remote) carrying the
# `branch` metadata sable-spawn-worker writes at dispatch time, pushes a real
# worker branch naming it, and asserts the delivered worker-landing message
# reflects the bead's REAL status rather than an assumed one.
#
# PRODUCTION-POLLUTION INCIDENT (optimus, live, during this bead's own
# authoring — SABLE-1cb2b): an earlier draft of this test passed
# SABLE_MERGE_NOTIFY_VIA_MSG=0 with REAL bd on PATH. That env var disables
# ONLY the message-first attempt to chuck, forcing the hook past it into the
# REAL durable for-chuck `bd create` fallback — for a branch that never
# existed on origin. Three real, untagged, production-shaped for-chuck beads
# landed in the live pool (SABLE-r67s9, SABLE-d4kbz, SABLE-wafm7) before
# chuck/optimus caught it; all three closed as test-artifact pollution. The
# fix has three parts, matching chuck's SABLE-1cb2b spec: (1) do NOT disable
# the message-first path — stub sable-msg/tmux so it SUCCEEDS, so the hook
# takes `sable-msg chuck ... && exit 0` and never reaches real bd create; (2)
# a stray-bead safety net that runs on every exit path (success OR failure),
# not just the happy path, in case the stub ever misbehaves; (3) an explicit
# negative control asserting the pool carries NO for-chuck bead naming this
# branch afterward — proving the fix isn't "shipped" by silently disabling
# the notify (a silenced notify and a fixed one look identical from the
# pass-side without this control). bd itself stays real throughout — only
# sable-msg/tmux (transport plumbing, not the property under test) are
# stubbed, the same scope the rest of this suite stubs them at.
# --------------------------------------------------------------------------

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP (integration SABLE-gx7p3): bd not found on PATH"
else
  GX_BARE=$(mktemp -d)
  GX_REPO=$(mktemp -d)
  GX_INT_STUB=$(mktemp -d)
  # SABLE-1cb2b: reap on EVERY exit path, not only the happy path a trailing
  # cleanup line covers — a script killed, interrupted, or aborted between the
  # push above and the explicit cleanup call below must still not leave a
  # stray for-chuck bead live in the real pool. `gx7p3_cleanup_stray_forchuck`
  # is defined below (function definitions execute before this trap fires at
  # real EXIT time); GX_BRANCH is unset if the script dies before reaching its
  # assignment, in which case no push happened yet either — the guard makes
  # that a no-op rather than an error under set -u.
  trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR" "$INT_BARE" "$INT_REPO" "$INTNOTIFY_REPO" "$INTNOTIFY_BARE" "$PZFK_BARE" "$PZFK_REPO" "$B06T_BARE" "$B06T_REPO" "$EMPTYDIFF_BARE" "$EMPTYDIFF_REPO" "$GX_BARE" "$GX_REPO" "$GX_INT_STUB"; [ -n "${GX_BRANCH:-}" ] && command -v gx7p3_cleanup_stray_forchuck >/dev/null 2>&1 && gx7p3_cleanup_stray_forchuck "$GX_BRANCH"' EXIT

  # Safety-net teardown (requirement 2): find and close/relabel ANY real
  # for-chuck bead naming $1, regardless of whether the assertions above it
  # passed or failed. Called after EVERY hook invocation below, not only on
  # a happy path — a cleanup that only fires on success is the exact trap
  # that let the incident's stray beads go unnoticed.
  gx7p3_cleanup_stray_forchuck() {
    local branch="$1" ids
    ids=$(bd list --status open,in_progress --label for-chuck --title-contains "$branch" --json 2>/dev/null \
      | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = []
for i in d:
    if isinstance(i, dict):
        print(i.get('id', ''))
" 2>/dev/null)
    for id in $ids; do
      [ -z "$id" ] && continue
      echo "SABLE-gx7p3 integration safety net: stray for-chuck bead $id named branch $branch — closing as test artifact"
      bd update "$id" --sandbox --notes "[no-test] SABLE-gx7p3 integration-test safety net: this for-chuck bead names a scratch branch ($branch) that only ever existed in a test fixture. Closing immediately as test pollution." 2>/dev/null || true
      bd close "$id" --sandbox --reason "SABLE-gx7p3 integration-test safety-net cleanup (test-fixture branch, never real work)" 2>/dev/null || true
    done
  }

  git init -q --bare "$GX_BARE"
  git clone -q "$GX_BARE" "$GX_REPO"
  cd_fixture "$GX_REPO"
  git -C "$GX_REPO" config user.email "gx@gx"; git -C "$GX_REPO" config user.name "gx"
  echo base > base.txt; git add base.txt; git commit -q -m base
  GX_MAIN=$(git symbolic-ref --short HEAD)
  git push -q "$GX_BARE" "HEAD:refs/heads/$GX_MAIN" 2>/dev/null
  git update-ref "refs/remotes/origin/$GX_MAIN" HEAD
  git push -q "$GX_BARE" HEAD:refs/heads/main 2>/dev/null
  git update-ref refs/remotes/origin/main HEAD
  GX_BRANCH="wk-gx7p3-inttest-$$"
  git checkout -q -b "$GX_BRANCH"
  echo change > gx_change.txt; git add gx_change.txt; git commit -q -m "worker change"
  git push -q "$GX_BARE" "HEAD:refs/heads/$GX_BRANCH" 2>/dev/null
  git update-ref "refs/remotes/origin/$GX_BRANCH" HEAD
  cd - >/dev/null

  GX_SCRATCH_ID=$(bd create --sandbox \
    --title="[int-test] SABLE-gx7p3 scratch bead for ${GX_BRANCH}" \
    --description="Scratch bead created by hooks/test/test-post-push-merge-notify.sh (SABLE-gx7p3 integration test) to verify the worker-landing auto-notify states only the bead's OBSERVED status. [no-test] — safe to close immediately, no code of its own." \
    --type=task \
    --metadata "{\"branch\": \"${GX_BRANCH}\"}" 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$GX_SCRATCH_ID" ]; then
    echo "SKIP (integration SABLE-gx7p3): could not create scratch bead — bd create output did not match ID pattern"
  else
    echo "Integration SABLE-gx7p3: created scratch bead $GX_SCRATCH_ID for branch $GX_BRANCH"
    bd update "$GX_SCRATCH_ID" --sandbox --notes "[no-test] integration test scratch — safe to close" 2>/dev/null || true

    # sable-msg/tmux stubs model a REACHABLE chuck who CONFIRMS delivery
    # (rc=0) — the message-first path this hook prefers — so the real
    # durable for-chuck `bd create` fallback is never reached. This is the
    # opposite of disabling the notify: both the worker-land AND chuck
    # messages still fire for real (asserted below); only the transport is
    # stubbed.
    cat > "$GX_INT_STUB/sable-msg" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${GX_MSG_LOG:-/dev/null}"
exit 0
EOF
    chmod +x "$GX_INT_STUB/sable-msg"
    cat > "$GX_INT_STUB/tmux" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  [ "$a" = "list-panes" ] && { echo chuck; exit 0; }
  [ "$a" = "display-message" ] && { echo "worker"; exit 0; }
done
exit 0
EOF
    chmod +x "$GX_INT_STUB/tmux"

    GX_MSG_LOG="$GX_INT_STUB/msg.log"
    : > "$GX_MSG_LOG"

    # (1) Bead is genuinely in_progress (its natural post-create state): the
    # delivered message must carry the AUTO-NOTIFY tag and must NOT assert
    # closure.
    INT_GX_INPUT=$(make_post_input "git push" "$GX_REPO")
    CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager TMUX_PANE=%gxinttest \
      PATH="$GX_INT_STUB:$PATH" GX_MSG_LOG="$GX_MSG_LOG" \
      bash "$HOOK" <<< "$INT_GX_INPUT" >/dev/null 2>&1
    gx7p3_cleanup_stray_forchuck "$GX_BRANCH"

    if grep -qE '^optimus .*\[AUTO-NOTIFY' "$GX_MSG_LOG" 2>/dev/null; then
      pass "integration SABLE-gx7p3: in_progress bead — notify carries the AUTO-NOTIFY tag"
    else
      fail "integration SABLE-gx7p3: in_progress bead — notify carries the AUTO-NOTIFY tag" "MSG_LOG: $(cat "$GX_MSG_LOG" 2>/dev/null)"
    fi
    if grep -qiE 'bead closed|closed bead' "$GX_MSG_LOG" 2>/dev/null; then
      fail "integration SABLE-gx7p3: in_progress bead — notify must NOT assert closure" "MSG_LOG: $(cat "$GX_MSG_LOG" 2>/dev/null)"
    else
      pass "integration SABLE-gx7p3: in_progress bead — notify does NOT assert closure"
    fi
    if grep -q "$GX_SCRATCH_ID" "$GX_MSG_LOG" 2>/dev/null; then
      pass "integration SABLE-gx7p3: notify names the resolved real bead id ($GX_SCRATCH_ID)"
    else
      fail "integration SABLE-gx7p3: notify names the resolved real bead id" "MSG_LOG: $(cat "$GX_MSG_LOG" 2>/dev/null)"
    fi
    # NEGATIVE CONTROL (requirement 3): the notify must still genuinely FIRE
    # to chuck too (proving the fix is not "pass by silencing the notify")
    # while leaving NOTHING behind in the real pool for this fake branch.
    if grep -qE '^chuck .*\[AUTO-NOTIFY' "$GX_MSG_LOG" 2>/dev/null; then
      pass "integration SABLE-gx7p3 NEGATIVE CONTROL: chuck handoff message still genuinely fires (notify not silenced)"
    else
      fail "integration SABLE-gx7p3 NEGATIVE CONTROL: chuck handoff message still genuinely fires (notify not silenced)" "MSG_LOG: $(cat "$GX_MSG_LOG" 2>/dev/null)"
    fi
    GX_STRAY_CHECK=$(bd list --status all --label for-chuck --title-contains "$GX_BRANCH" --json 2>/dev/null || echo "")
    if [ -z "$GX_STRAY_CHECK" ] || [ "$GX_STRAY_CHECK" = "[]" ] || [ "$GX_STRAY_CHECK" = "null" ]; then
      pass "integration SABLE-gx7p3 NEGATIVE CONTROL: no stray for-chuck bead was created for this fake branch (no pool pollution)"
    else
      fail "integration SABLE-gx7p3 NEGATIVE CONTROL: no stray for-chuck bead was created for this fake branch (no pool pollution)" "pool still shows: $GX_STRAY_CHECK"
    fi

    # (2) POSITIVE CONTROL: close the bead for real, push a second commit on
    # the SAME branch (metadata association unchanged), and confirm the
    # notify DOES state closure once bd actually shows it closed.
    bd close "$GX_SCRATCH_ID" --sandbox --reason "integration test complete" 2>/dev/null || true
    : > "$GX_MSG_LOG"
    cd_fixture "$GX_REPO"
    git checkout -q "$GX_BRANCH"
    echo change2 > gx_change2.txt; git add gx_change2.txt; git commit -q -m "worker change 2"
    git push -q "$GX_BARE" "HEAD:refs/heads/$GX_BRANCH" 2>/dev/null
    git update-ref "refs/remotes/origin/$GX_BRANCH" HEAD
    cd - >/dev/null

    INT_GX_INPUT2=$(make_post_input "git push" "$GX_REPO")
    CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager TMUX_PANE=%gxinttest \
      PATH="$GX_INT_STUB:$PATH" GX_MSG_LOG="$GX_MSG_LOG" \
      bash "$HOOK" <<< "$INT_GX_INPUT2" >/dev/null 2>&1
    gx7p3_cleanup_stray_forchuck "$GX_BRANCH"

    if grep -qE '^optimus .*\[AUTO-NOTIFY' "$GX_MSG_LOG" 2>/dev/null \
       && grep -qiE 'closed bead' "$GX_MSG_LOG" 2>/dev/null; then
      pass "integration SABLE-gx7p3 POSITIVE CONTROL: genuinely closed bead — notify states closure"
    else
      fail "integration SABLE-gx7p3 POSITIVE CONTROL: genuinely closed bead — notify states closure" "MSG_LOG: $(cat "$GX_MSG_LOG" 2>/dev/null)"
    fi
    GX_STRAY_CHECK2=$(bd list --status all --label for-chuck --title-contains "$GX_BRANCH" --json 2>/dev/null || echo "")
    if [ -z "$GX_STRAY_CHECK2" ] || [ "$GX_STRAY_CHECK2" = "[]" ] || [ "$GX_STRAY_CHECK2" = "null" ]; then
      pass "integration SABLE-gx7p3 NEGATIVE CONTROL: no stray for-chuck bead after the positive-control push either"
    else
      fail "integration SABLE-gx7p3 NEGATIVE CONTROL: no stray for-chuck bead after the positive-control push either" "pool still shows: $GX_STRAY_CHECK2"
    fi
  fi
fi

# --------------------------------------------------------------------------
# SABLE-pfbjw — ground-truth branch-ref overlap scan.
#
# Replaces the prior mechanism entirely: the old scan intersected this push's
# files against `bd list --status=in_progress`'s wip_claims metadata, and
# both directions of that were wrong, measured live rather than theorized:
#   (A) FALSE POSITIVE — wip_claims is a claim, true only when written and
#       never invalidated. SABLE-23upx's (wk-sable-screen) own wip_claims was
#       byte-identical to its own pushed file list; SABLE-be4lo.4
#       (wk-trains-fold)'s wip_claims contained the exact file its own push
#       touched. Both beads were still in_progress at push time, so the
#       branch matched itself.
#   (B) FALSE NEGATIVE, the dangerous direction — `--status=in_progress` can
#       never see the CLOSED-BUT-UNLANDED population, which in this fleet is
#       the NORMAL end state. Live case (optimus): skrdj pushed with NO
#       warning while genuinely sharing a file with qwthx, which was CLOSED
#       and its branch still uncontained.
#
# One real bare origin hosts every branch below. Occupancy is derived purely
# from git refs (uncontained = not an ancestor of the integration branch);
# bd is consulted ONLY to label an overlap with a bead id, never to decide
# whether one exists — proven directly by giving the genuinely-overlapping
# branch a CLOSED bead and confirming the warning still fires.
# --------------------------------------------------------------------------

PFBJW_BARE=$(mktemp -d)
PFBJW_REPO=$(mktemp -d)
trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR" "$INT_BARE" "$INT_REPO" "$INTNOTIFY_REPO" "$INTNOTIFY_BARE" "$PZFK_BARE" "$PZFK_REPO" "$B06T_BARE" "$B06T_REPO" "$EMPTYDIFF_BARE" "$EMPTYDIFF_REPO" "$PFBJW_BARE" "$PFBJW_REPO"' EXIT

git init -q --bare "$PFBJW_BARE"
git clone -q "$PFBJW_BARE" "$PFBJW_REPO"
cd_fixture "$PFBJW_REPO"
git -C "$PFBJW_REPO" config user.email "pf@pf"; git -C "$PFBJW_REPO" config user.name "pf"
echo base > base.txt; git add base.txt; git commit -q -m base
git push -q "$PFBJW_BARE" HEAD:refs/heads/main 2>/dev/null
git update-ref refs/remotes/origin/main HEAD

# Integration branch (SABLE-pzfk default-base resolution picks this up via
# sable.integrationBranch, same as the PZFK fixture above).
git checkout -q -b tmux-only
git push -q "$PFBJW_BARE" HEAD:refs/heads/tmux-only 2>/dev/null
git update-ref refs/remotes/origin/tmux-only HEAD
git -C "$PFBJW_REPO" config sable.integrationBranch tmux-only

# Occupant A: genuinely uncontained (never merged into tmux-only), touches
# libfile.py. Its bead will be modeled as CLOSED via the bd stub below — the
# skrdj/qwthx shape this bead exists to fix.
git checkout -q tmux-only
git checkout -q -b wk-occupant-a
echo lib > libfile.py; git add libfile.py; git commit -q -m "occupant-a: libfile"
git push -q "$PFBJW_BARE" HEAD:refs/heads/wk-occupant-a 2>/dev/null
git update-ref refs/remotes/origin/wk-occupant-a HEAD

# Occupant B: all-new files that exist nowhere else in the tree (SABLE-23upx
# shape — the free positive control the dispatch highlighted). Genuinely
# uncontained, but structurally cannot share a path with anything.
git checkout -q tmux-only
git checkout -q -b wk-newfiles-only
echo n1 > brandnew1.txt; echo n2 > brandnew2.txt
git add brandnew1.txt brandnew2.txt; git commit -q -m "newfiles-only: two new files"
git push -q "$PFBJW_BARE" HEAD:refs/heads/wk-newfiles-only 2>/dev/null
git update-ref refs/remotes/origin/wk-newfiles-only HEAD

# The pushing branch: touches libfile.py (genuine overlap with occupant-a)
# plus a file unique to itself.
git checkout -q tmux-only
git checkout -q -b wk-pusher
echo lib2 > libfile.py; echo p > pusher_only.txt
git add libfile.py pusher_only.txt; git commit -q -m "pusher: touches libfile + own file"
git push -q "$PFBJW_BARE" HEAD:refs/heads/wk-pusher 2>/dev/null
git update-ref refs/remotes/origin/wk-pusher HEAD
cd - >/dev/null

# bd stub: ONLY occupant-a resolves to a bead, and it is CLOSED — proving
# label resolution works and that closure does not suppress detection. Every
# other `bd list` (wk-newfiles-only, wk-pusher itself) resolves to no bead,
# proving detection needs no bd record at all.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "list" ]; then
  for a in "$@"; do
    if [ "$a" = "branch=wk-occupant-a" ]; then
      cat <<'JSON'
[{"id": "SABLE-pfqwthx", "status": "closed"}]
JSON
      exit 0
    fi
  done
  echo "[]"
  exit 0
fi
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

# --- POSITIVE CONTROL, load-bearing (SABLE-pfbjw / optimus): genuinely
# overlapping CLOSED-but-unlanded branch MUST warn, and must NAME the bead
# and the shared file. SABLE_MSG_STUB_RC=0 so the live chuck message (not
# just the durable fallback bead) is what's asserted on. ---
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
PFBJW_PUSHER_INPUT=$(make_post_input "git push" "$PFBJW_REPO")
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV" "$PFBJW_PUSHER_INPUT" >/dev/null

if grep -q 'OVERLAP-WARNING' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-pfbjw: genuinely overlapping CLOSED-but-unlanded branch DOES warn (skrdj/qwthx shape)"
else
  fail "SABLE-pfbjw: genuinely overlapping CLOSED-but-unlanded branch DOES warn (skrdj/qwthx shape)" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi
if grep -q 'libfile.py' "$SABLE_MSG_LOG" 2>/dev/null && grep -qE 'wk-occupant-a|SABLE-pfqwthx' "$SABLE_MSG_LOG" 2>/dev/null; then
  pass "SABLE-pfbjw: warning names WHAT is shared (libfile.py) and WITH WHAT (wk-occupant-a / SABLE-pfqwthx)"
else
  fail "SABLE-pfbjw: warning names WHAT is shared and WITH WHAT" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
fi
if grep -qE 'brandnew1\.txt|brandnew2\.txt|wk-newfiles-only' "$SABLE_MSG_LOG" 2>/dev/null; then
  fail "SABLE-pfbjw: unrelated disjoint branch (wk-newfiles-only) is NOT named as an overlap" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
else
  pass "SABLE-pfbjw: unrelated disjoint branch (wk-newfiles-only) is NOT named as an overlap"
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG"

# --- NEGATIVE CONTROL, load-bearing: a push composed ONLY of files absent
# from the base tree everywhere else (SABLE-23upx shape) must NOT warn, even
# though it is genuinely uncontained real work. ---
cd_fixture "$PFBJW_REPO"
git checkout -q wk-newfiles-only
cd - >/dev/null
PFBJW_NEWFILES_INPUT=$(make_post_input "git push" "$PFBJW_REPO")
SABLE_MSG_STUB_RC=0 run_hook "$MGR_ENV" "$PFBJW_NEWFILES_INPUT" >/dev/null

if grep -q 'OVERLAP-WARNING' "$SABLE_MSG_LOG" 2>/dev/null; then
  fail "SABLE-pfbjw NEGATIVE CONTROL: only-new-files branch does NOT self-match (SABLE-23upx shape)" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
else
  pass "SABLE-pfbjw NEGATIVE CONTROL: only-new-files branch does NOT self-match (SABLE-23upx shape)"
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG"

# --------------------------------------------------------------------------
# PLANT-AND-FAIL (SABLE-5lli.7): remove the self-skip-by-branch-name line and
# confirm the SAME only-new-files push (wk-newfiles-only) STARTS warning
# against its own ref — this is the exact self-match failure mode measured
# live (SABLE-23upx), reproduced on demand to prove the exclusion is
# load-bearing rather than untested.
# --------------------------------------------------------------------------
PFBJW_PLANT_DIR="$STUB_DIR/plant-pfbjw"
mkdir -p "$PFBJW_PLANT_DIR"
for f in "$LIB_DIR"/*.sh; do
  ln -sf "$f" "$PFBJW_PLANT_DIR/$(basename "$f")"
done
PFBJW_MUTANT="$PFBJW_PLANT_DIR/post-push-merge-notify-PFBJW-PLANT.sh"
PFBJW_PLANT_ERR=$(python3 - "$HOOK" "$PFBJW_MUTANT" <<'PYEOF'
import sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = open(src_path).read()
needle = '    [ "$CAND_SHORT" = "$BRANCH" ] && continue  # never compare the push to itself\n'
if needle not in src:
    print(f"could not locate self-skip line", file=sys.stderr)
    sys.exit(1)
new_src = src.replace(needle, '', 1)
open(dst_path, "w").write(new_src)
PYEOF
)
PFBJW_PLANT_RC=$?
chmod +x "$PFBJW_MUTANT" 2>/dev/null || true

if [ "$PFBJW_PLANT_RC" -ne 0 ] || [ ! -s "$PFBJW_MUTANT" ]; then
  fail "PLANT-AND-FAIL SABLE-pfbjw: could not construct the self-skip-removed mutant hook" "rc=$PFBJW_PLANT_RC err=$PFBJW_PLANT_ERR"
else
  rm -f "$BD_LOG" "$SABLE_MSG_LOG"
  env -i PATH="$STUB_DIR:$PATH" BD_LOG="$BD_LOG" SABLE_MSG_LOG="$SABLE_MSG_LOG" SABLE_MSG_STUB_RC=0 \
    SABLE_HOOK_TRACE_LOG="$STUB_DIR/hook-trace-pfbjw-plant.log" \
    $MGR_ENV bash "$PFBJW_MUTANT" <<< "$PFBJW_NEWFILES_INPUT" >/dev/null 2>&1

  if grep -q 'CONFIRMED' "$STUB_DIR/hook-trace-pfbjw-plant.log" 2>/dev/null; then
    pass "PLANT-AND-FAIL SABLE-pfbjw: mutant hook actually ran (non-vacuity precondition)"
  else
    fail "PLANT-AND-FAIL SABLE-pfbjw: mutant hook actually ran (non-vacuity precondition)" "hook-trace-pfbjw-plant.log: $(cat "$STUB_DIR/hook-trace-pfbjw-plant.log" 2>/dev/null)"
  fi
  if grep -q 'OVERLAP-WARNING' "$SABLE_MSG_LOG" 2>/dev/null; then
    pass "PLANT-AND-FAIL SABLE-pfbjw: removing the self-skip DOES reintroduce self-match on the only-new-files branch (proves the exclusion is load-bearing)"
  else
    fail "PLANT-AND-FAIL SABLE-pfbjw: removing the self-skip DOES reintroduce self-match on the only-new-files branch" "MSG_LOG: $(cat "$SABLE_MSG_LOG" 2>/dev/null)"
  fi
fi
rm -f "$BD_LOG" "$SABLE_MSG_LOG"
rm -rf "$PFBJW_PLANT_DIR"

# Restore the plain bd stub for hermeticity if more tests are appended later.
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${BD_LOG:-/tmp/bd-stub.log}"
exit 0
EOF
chmod +x "$STUB_DIR/bd"

# --------------------------------------------------------------------------
# INTEGRATION SABLE-pfbjw — real bd, real git, no mocks of either. Creates a
# scratch bead in the real project Dolt db (--sandbox on every write, never
# pushed to the shared remote), closes it immediately (modeling the fleet's
# normal end state), and confirms a genuinely overlapping push against its
# still-uncontained branch warns anyway — the property no declaration-based
# implementation (bd status OR wip_claims) can pass, per optimus.
# --------------------------------------------------------------------------

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP (integration SABLE-pfbjw): bd not found on PATH"
else
  PFI_BARE=$(mktemp -d)
  PFI_REPO=$(mktemp -d)
  PFI_STUB=$(mktemp -d)
  PFI_MSG_LOG="$PFI_STUB/sable-msg-calls.log"
  PFI_BRANCH=""
  pfbjw_cleanup_stray_forchuck() {
    local branch="$1" ids
    [ -z "$branch" ] && return 0
    ids=$(bd list --status open,in_progress --label for-chuck --title-contains "$branch" --json 2>/dev/null \
      | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = []
for i in d:
    if isinstance(i, dict):
        print(i.get('id', ''))
" 2>/dev/null)
    for id in $ids; do
      [ -z "$id" ] && continue
      bd update "$id" --sandbox --notes "[no-test] SABLE-pfbjw integration-test safety net: scratch branch ($branch), test fixture only." 2>/dev/null || true
      bd close "$id" --sandbox --reason "SABLE-pfbjw integration-test safety-net cleanup" 2>/dev/null || true
    done
  }
  trap 'rm -rf "$FIXTURE_REPO" "$BARE_ORIGIN" "$STUB_DIR" "$INT_BARE" "$INT_REPO" "$INTNOTIFY_REPO" "$INTNOTIFY_BARE" "$PZFK_BARE" "$PZFK_REPO" "$B06T_BARE" "$B06T_REPO" "$EMPTYDIFF_BARE" "$EMPTYDIFF_REPO" "$PFBJW_BARE" "$PFBJW_REPO" "$PFI_BARE" "$PFI_REPO" "$PFI_STUB"; [ -n "${PFI_BRANCH:-}" ] && command -v pfbjw_cleanup_stray_forchuck >/dev/null 2>&1 && pfbjw_cleanup_stray_forchuck "$PFI_BRANCH"' EXIT

  cat > "$PFI_STUB/sable-msg" <<'EOF'
#!/usr/bin/env bash
echo "$@" >> "${PFI_MSG_LOG:-/dev/null}"
exit 0
EOF
  chmod +x "$PFI_STUB/sable-msg"
  cat > "$PFI_STUB/tmux" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "list-panes" ]; then echo "chuck"; exit 0; fi
  if [ "$a" = "display-message" ]; then echo ""; exit 0; fi
done
exit 0
EOF
  chmod +x "$PFI_STUB/tmux"

  git init -q --bare "$PFI_BARE"
  git clone -q "$PFI_BARE" "$PFI_REPO"
  cd_fixture "$PFI_REPO"
  git -C "$PFI_REPO" config user.email "pfi@pfi"; git -C "$PFI_REPO" config user.name "pfi"
  echo base > base.txt; git add base.txt; git commit -q -m base
  git push -q "$PFI_BARE" HEAD:refs/heads/main 2>/dev/null
  git update-ref refs/remotes/origin/main HEAD
  git checkout -q -b tmux-only
  git push -q "$PFI_BARE" HEAD:refs/heads/tmux-only 2>/dev/null
  git update-ref refs/remotes/origin/tmux-only HEAD
  git -C "$PFI_REPO" config sable.integrationBranch tmux-only

  PFI_SUFFIX="$$-${RANDOM}"
  PFI_OCC_BRANCH="wk-pfbjwreal-occ-${PFI_SUFFIX}"
  git checkout -q tmux-only
  git checkout -q -b "$PFI_OCC_BRANCH"
  echo shared > shared_real.txt; git add shared_real.txt; git commit -q -m "occupant: shared_real.txt"
  git push -q "$PFI_BARE" "HEAD:refs/heads/$PFI_OCC_BRANCH" 2>/dev/null
  git update-ref "refs/remotes/origin/$PFI_OCC_BRANCH" HEAD

  PFI_BRANCH="wk-pfbjwreal-pusher-${PFI_SUFFIX}"
  git checkout -q tmux-only
  git checkout -q -b "$PFI_BRANCH"
  echo shared2 > shared_real.txt; git add shared_real.txt; git commit -q -m "pusher: shared_real.txt"
  git push -q "$PFI_BARE" "HEAD:refs/heads/$PFI_BRANCH" 2>/dev/null
  git update-ref "refs/remotes/origin/$PFI_BRANCH" HEAD
  cd - >/dev/null

  # Real bd: create a bead for the occupant branch, then CLOSE it immediately
  # (the fleet's normal end state) BEFORE the pusher's push runs. If the scan
  # were bd-status-gated in any way, a closed occupant would be invisible.
  PFI_SCRATCH_ID=$(bd create --sandbox \
    --title="[int-test] SABLE-pfbjw scratch bead for ${PFI_OCC_BRANCH}" \
    --description="Scratch bead created by hooks/test/test-post-push-merge-notify.sh (SABLE-pfbjw integration test) to verify the overlap scan warns on a genuinely overlapping CLOSED-but-unlanded branch (the skrdj/qwthx shape). [no-test] — safe to close immediately, no code of its own." \
    --type=task \
    --metadata "{\"branch\": \"${PFI_OCC_BRANCH}\"}" 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)
  if [ -z "$PFI_SCRATCH_ID" ]; then
    echo "SKIP (integration SABLE-pfbjw): could not create scratch bead — bd create output did not match ID pattern"
  else
    bd close "$PFI_SCRATCH_ID" --sandbox --reason "SABLE-pfbjw integration test: modeling closed-but-unlanded" 2>/dev/null || true

    PFI_INPUT=$(make_post_input "git push" "$PFI_REPO")
    CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
      PATH="$PFI_STUB:$PATH" PFI_MSG_LOG="$PFI_MSG_LOG" \
      bash "$HOOK" <<< "$PFI_INPUT" >/dev/null 2>&1
    pfbjw_cleanup_stray_forchuck "$PFI_BRANCH"
    pfbjw_cleanup_stray_forchuck "$PFI_OCC_BRANCH"

    if grep -q 'OVERLAP-WARNING' "$PFI_MSG_LOG" 2>/dev/null && grep -q 'shared_real.txt' "$PFI_MSG_LOG" 2>/dev/null; then
      pass "integration SABLE-pfbjw: real bd + real git — CLOSED-but-unlanded occupant STILL warns (skrdj/qwthx case, no declaration-based implementation can pass this)"
    else
      fail "integration SABLE-pfbjw: real bd + real git — CLOSED-but-unlanded occupant STILL warns" "MSG_LOG: $(cat "$PFI_MSG_LOG" 2>/dev/null)"
    fi
  fi
fi

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
