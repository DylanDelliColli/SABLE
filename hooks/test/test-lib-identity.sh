#!/usr/bin/env bash
# test-lib-identity.sh — unit tests for hooks/multi-manager/lib-identity.sh
# (SABLE-uz9.3). Crafted hook-input JSON + a fixture registry; asserts the
# resolution matrix including the dual-mode (env legacy) guarantee and the
# subagent-contamination fix (env ignored when agent_id present).
#
# Run with:
#   bash hooks/test/test-lib-identity.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LIB="$REPO/hooks/multi-manager/lib-identity.sh"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# Fixture registry (minimal mirror of templates/multi-manager/agents.yaml shapes)
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
cat > "$FIXTURE_DIR/agents.yaml" <<'YAML'
agents:
  optimus:
    type: epic_manager
    inbox_label: for-optimus
  tarzan:
    type: one_off_manager
  chuck:
    type: integrator
  lincoln:
    type: strategist
  cockpit:
    type: cockpit
  sherlock:
    type: auditor
  victor:
    type: bead_validator
YAML
export SABLE_AGENTS_YAML="$FIXTURE_DIR/agents.yaml"

# run_case <case-name> <json> <env_name> <env_role> <expect: name|type|source|sub|mgr|reg>
run_case() {
  local label="$1" json="$2" env_name="$3" env_role="$4" expect="$5"
  local got
  got=$(
    unset CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE
    [ -n "$env_name" ] && export CLAUDE_AGENT_NAME="$env_name"
    [ -n "$env_role" ] && export CLAUDE_AGENT_ROLE="$env_role"
    # shellcheck disable=SC1090
    source "$LIB"
    sable_resolve_identity "$json"
    printf '%s|%s|%s|%s|%s|%s' "$SABLE_ID_NAME" "$SABLE_ID_TYPE" "$SABLE_ID_SOURCE" \
      "$SABLE_ID_IS_SUBAGENT" "$SABLE_ID_IS_MANAGER" "$SABLE_ID_IS_REGISTERED"
  )
  if [ "$got" = "$expect" ]; then
    pass "$label"
  else
    fail "$label" "expected [$expect] got [$got]"
  fi
}

# 1. Manager-typed subagent (v2 path): agent_type=optimus
run_case "subagent optimus resolves as manager via agent_type" \
  '{"agent_id":"abc123","agent_type":"optimus","tool_name":"Bash"}' \
  "" "" \
  "optimus|epic_manager|agent_type|1|1|1"

# 2. Worker subagent: unregistered type stands down (case-folded)
run_case "subagent Explore is an unregistered worker, never a manager" \
  '{"agent_id":"abc124","agent_type":"Explore","tool_name":"Bash"}' \
  "" "" \
  "explore||agent_type|1|0|0"

# 3. Contamination fix: agent_id present => parent env IGNORED
run_case "worker inside optimus terminal is NOT optimus (env ignored)" \
  '{"agent_id":"abc125","agent_type":"general-purpose"}' \
  "optimus" "manager" \
  "general-purpose||agent_type|1|0|0"

# 4. Legacy terminal launch (Chuck holdout): env-sourced, registry-typed
run_case "env chuck resolves as manager (integrator) — dual-mode" \
  '{"tool_name":"Bash","session_id":"s1"}' \
  "chuck" "manager" \
  "chuck|integrator|env|0|1|1"

# 5. Legacy custom alias not in registry: role=manager honored
run_case "unregistered env name with role=manager keeps manager behavior" \
  '{"tool_name":"Bash"}' \
  "megatron" "manager" \
  "megatron||env|0|1|0"

# 6. Registered planning agent: identified but not a manager (existing no-op)
run_case "subagent sherlock is registered non-manager" \
  '{"agent_id":"abc126","agent_type":"sherlock"}' \
  "" "" \
  "sherlock|auditor|agent_type|1|0|1"

# 7. Plain main session: nothing set
run_case "anonymous main session resolves to none" \
  '{"tool_name":"Bash","session_id":"s2"}' \
  "" "" \
  "||none|0|0|0"

# 8. Env identity without manager role: named but not manager
run_case "env sherlock (legacy shell function) is registered non-manager" \
  '{"tool_name":"Bash"}' \
  "sherlock" "auditor" \
  "sherlock|auditor|env|0|0|1"

# 9. Subagent with agent_id but no agent_type (defensive): subagent, unnamed
run_case "agent_id without agent_type yields unnamed subagent (stand-down)" \
  '{"agent_id":"abc127"}' \
  "optimus" "manager" \
  "||none|1|0|0"

# 10. Malformed JSON fails open to env
run_case "malformed hook JSON falls back to env identity" \
  'not-json-at-all' \
  "tarzan" "manager" \
  "tarzan|one_off_manager|env|0|1|1"

# --------------------------------------------------------------------------
# sable_is_git_push unit tests (SABLE-jpr / SABLE-0u1)
# --------------------------------------------------------------------------
# shellcheck disable=SC1090
source "$LIB"

is_push_test() {
  local label="$1" cmd="$2" expect_exit="$3"
  if sable_is_git_push "$cmd"; then
    local actual=0
  else
    local actual=1
  fi
  if [ "$actual" -eq "$expect_exit" ]; then
    pass "$label"
  else
    fail "$label" "expected exit $expect_exit, got $actual for cmd: $cmd"
  fi
}

is_push_test "sable_is_git_push: plain 'git push'" "git push" 0
is_push_test "sable_is_git_push: 'git push origin main'" "git push origin main" 0
is_push_test "sable_is_git_push: 'git -C /x push'" "git -C /x push" 0
is_push_test "sable_is_git_push: 'git -c a=b push origin main'" "git -c a=b push origin main" 0
is_push_test "sable_is_git_push: 'git --no-pager push'" "git --no-pager push" 0
is_push_test "sable_is_git_push: 'git -C /x -c a=b push'" "git -C /x -c a=b push" 0
is_push_test "sable_is_git_push: 'git pushd' is NOT push" "git pushd" 1
is_push_test "sable_is_git_push: 'git status' is NOT push" "git status" 1
is_push_test "sable_is_git_push: text mention only is NOT push" 'bd create --description="git push"' 1
is_push_test "sable_is_git_push: 'echo git push' is NOT push" "echo git push" 1
is_push_test "sable_is_git_push: 'git pull' is NOT push" "git pull" 1
is_push_test "sable_is_git_push: empty string is NOT push" "" 1
# env-assignment prefix cases (SABLE-531 regression fix)
is_push_test "sable_is_git_push: 'SABLE_SKIP_PRE_PUSH=1 git push'" "SABLE_SKIP_PRE_PUSH=1 git push" 0
is_push_test "sable_is_git_push: 'FOO=bar BAZ=qux git -C /x push'" "FOO=bar BAZ=qux git -C /x push" 0
is_push_test "sable_is_git_push: 'env FOO=bar git push'" "env FOO=bar git push" 0
is_push_test "sable_is_git_push: 'env -u GIT_DIR git push'" "env -u GIT_DIR git push" 0
is_push_test "sable_is_git_push: 'echo SABLE_SKIP_PRE_PUSH=1 git push' is NOT push" "echo SABLE_SKIP_PRE_PUSH=1 git push" 1
is_push_test "sable_is_git_push: 'bd create --description=FOO=1 git push' is NOT push" 'bd create --description="FOO=1 git push"' 1

# --------------------------------------------------------------------------
# sable_validate_base_ref unit tests (SABLE-61n)
# --------------------------------------------------------------------------

# Set up a minimal git repo for ref validation tests
VAL_REPO=$(mktemp -d)
VAL_BARE=$(mktemp -d)
trap 'rm -rf "$VAL_REPO" "$VAL_BARE"' EXIT
git init -q --bare "$VAL_BARE"
git clone -q "$VAL_BARE" "$VAL_REPO"
cd "$VAL_REPO"
git config user.email "v@test"
git config user.name "Validator"
echo "x" > f.txt
git add f.txt
git commit -q -m "init"
git push -q origin HEAD:refs/heads/main 2>/dev/null
cd - >/dev/null

validate_ref_test() {
  local label="$1" repo="$2" desired="$3" expected_pattern="$4"
  # shellcheck disable=SC1090
  source "$LIB"
  local result
  result=$(sable_validate_base_ref "$repo" "$desired")
  if echo "$result" | grep -qE "$expected_pattern"; then
    pass "$label"
  else
    fail "$label" "expected pattern '$expected_pattern', got '$result'"
  fi
}

validate_ref_test "sable_validate_base_ref: valid ref returned unchanged" \
  "$VAL_REPO" "origin/main" "^origin/main$"

validate_ref_test "sable_validate_base_ref: nonexistent ref falls back to origin/main" \
  "$VAL_REPO" "origin/nonexistent" "^origin/main$"

validate_ref_test "sable_validate_base_ref: empty repo path returns desired ref unchanged" \
  "" "origin/dev" "^origin/dev$"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
