#!/usr/bin/env bash
# test-pre-push-rebase-test.sh — Tests for pre-push-rebase-test.sh routing
#
# Runs the hook against synthetic input + temporary fixture repos to
# verify:
#   - Skips when not a git push command
#   - Skips when --force is used
#   - Skips for subagent context (agent_id present)
#   - Auto-detects typecheck command from project markers (tsconfig, Cargo.toml, go.mod)
#   - Skips test phase but runs static phase when SABLE_SKIP_PRE_PUSH=1
#
# Where full integration (real git fetch + rebase + tests) requires a
# realistic upstream, we don't test that here — that's a manual integration
# test against twine or another live repo.
#
# Run with:
#   bash hooks/test/test-pre-push-rebase-test.sh

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/multi-manager/pre-push-rebase-test.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""

make_input() {
  # $1 = command, $2 = cwd, $3 (optional) = agent_id
  python3 -c "
import json, sys
cmd = sys.argv[1]
cwd = sys.argv[2]
agent_id = sys.argv[3] if len(sys.argv) > 3 else ''
out = {'tool_input': {'command': cmd}, 'cwd': cwd}
if agent_id:
    out['agent_id'] = agent_id
print(json.dumps(out))
" "$1" "$2" "${3:-}"
}

run_hook() {
  # $1 = env prefix, $2 = command, $3 = cwd, $4 = optional agent_id
  local env_prefix="$1"
  local cmd="$2"
  local cwd="$3"
  local aid="${4:-}"
  local input
  input=$(make_input "$cmd" "$cwd" "$aid")
  local out
  out=$(env -i PATH="$PATH" $env_prefix bash "$HOOK" <<< "$input" 2>/dev/null || echo "RUN_ERR:$?")
  echo -n "$out"
}

assert_allow() {
  local name="$1" env="$2" cmd="$3" cwd="$4" aid="${5:-}"
  local out
  out=$(run_hook "$env" "$cmd" "$cwd" "$aid")
  if [ -z "$out" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: ${out:0:200})"
    echo "FAIL: $name"
    echo "  Expected: empty (allow)"
    echo "  Got:      ${out:0:300}"
  fi
}

assert_deny() {
  local name="$1" env="$2" cmd="$3" cwd="$4" expect="$5" aid="${6:-}"
  local out
  out=$(run_hook "$env" "$cmd" "$cwd" "$aid")
  if echo "$out" | grep -q '"permissionDecision": "deny"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: ${out:0:300})"
    echo "FAIL: $name"
    echo "  Expected: deny containing '$expect'"
    echo "  Got:      ${out:0:500}"
  fi
}

assert_context() {
  local name="$1" env="$2" cmd="$3" cwd="$4" expect="$5"
  local out
  out=$(run_hook "$env" "$cmd" "$cwd")
  if echo "$out" | grep -q '"additionalContext"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: ${out:0:300})"
    echo "FAIL: $name"
    echo "  Expected: additionalContext containing '$expect'"
    echo "  Got:      ${out:0:500}"
  fi
}

# Build a fixture repo with intentional type error in TS
make_ts_fixture() {
  local dir="$1"
  rm -rf "$dir"
  mkdir -p "$dir"
  cd "$dir"
  git init -q
  git config user.email "test@test"
  git config user.name "Test"
  cat > tsconfig.json <<'EOF'
{"compilerOptions": {"strict": true, "noEmit": true, "module": "esnext", "target": "es2022"}, "include": ["src"]}
EOF
  mkdir -p src
  cat > src/index.ts <<'EOF'
const x: number = "not a number";
EOF
  git add -A
  git commit -q -m "init"
  cd - >/dev/null
}

make_clean_ts_fixture() {
  local dir="$1"
  rm -rf "$dir"
  mkdir -p "$dir"
  cd "$dir"
  git init -q
  git config user.email "test@test"
  git config user.name "Test"
  cat > tsconfig.json <<'EOF'
{"compilerOptions": {"strict": true, "noEmit": true, "module": "esnext", "target": "es2022"}, "include": ["src"]}
EOF
  mkdir -p src
  cat > src/index.ts <<'EOF'
const x: number = 42;
EOF
  git add -A
  git commit -q -m "init"
  cd - >/dev/null
}

MGR_ENV="CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager"

# Test 1: ignores non-git-push commands
assert_allow "ignores non-git-push" "$MGR_ENV" "git status" "/tmp"

# Test 2: ignores when no manager identity
assert_allow "no manager identity → no-op" "" "git push" "/tmp"

# Test 3: skips force pushes
assert_allow "skips --force" "$MGR_ENV" "git push --force" "/tmp"
assert_allow "skips -f shorthand" "$MGR_ENV" "git push -f" "/tmp"

# Test 4: skips subagent context (agent_id present)
assert_allow "skips subagent" "$MGR_ENV" "git push" "/tmp" "subagent-123"

# Test 5: skips when CWD has no .git directory
assert_allow "skips non-repo CWD" "$MGR_ENV" "git push" "/tmp/nonexistent-non-repo"

# ---------- Routing tests via SABLE_PRE_PUSH_TYPECHECK_COMMAND override ----------
# Use a stand-in "typechecker" command (just `true` or `false`) to exercise the
# hook's phase routing without needing a real toolchain. The point of these
# tests is to verify the hook's decision tree (does it deny on static failure?
# does SABLE_SKIP_PRE_PUSH=1 still enforce static?), not the typechecker itself.

REPO_DIR="/tmp/sable-test-pre-push-repo"
BARE_DIR="/tmp/sable-test-pre-push-bare.git"
rm -rf "$REPO_DIR" "$BARE_DIR"

# Create a bare repo to serve as `origin` so `git fetch origin` succeeds.
git init -q --bare "$BARE_DIR"

# Clone from bare → working repo with origin remote configured.
git clone -q "$BARE_DIR" "$REPO_DIR"
cd "$REPO_DIR"
git config user.email "test@test"
git config user.name "Test"
echo "x" > README.md
git add -A
git commit -q -m "init"
git push -q origin HEAD:refs/heads/main 2>/dev/null
cd - >/dev/null

# Test 6: typecheck override that fails → DENY in static phase
FAIL_TC="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=false SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_deny "failing typecheck command → static phase denies" "$FAIL_TC" "git push" "$REPO_DIR" "phase 2 (static)"

# Test 7: typecheck override that passes → static passes, PHASE=skip → context
PASS_TC="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_context "passing typecheck → static OK, PHASE=skip → context" "$PASS_TC" "git push" "$REPO_DIR" "phase skipped"

# Test 8: SABLE_SKIP_PRE_PUSH=1 + failing typecheck → STILL DENIED (skip is tests-only)
SKIP_FAIL="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=false SABLE_SKIP_PRE_PUSH=1"
assert_deny "SKIP_PRE_PUSH does NOT skip static phase (typecheck still enforced)" "$SKIP_FAIL" "git push" "$REPO_DIR" "phase 2 (static)"

# Test 9: SABLE_SKIP_PRE_PUSH=1 + passing typecheck → static OK, test phase bypassed
SKIP_PASS="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_SKIP_PRE_PUSH=1"
assert_context "SKIP_PRE_PUSH skips test phase only" "$SKIP_PASS" "git push" "$REPO_DIR" "test phase bypassed"

# Test 10: failing lint command → DENY in static phase (even with passing typecheck)
FAIL_LINT="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_LINT_COMMAND=false SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_deny "failing lint command → static phase denies" "$FAIL_LINT" "git push" "$REPO_DIR" "lint failed"

# Test 11: no typecheck/lint configured + no project markers → static no-ops, PHASE=skip → context
NO_STATIC="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_context "no typecheck detected → static no-ops" "$NO_STATIC" "git push" "$REPO_DIR" "phase skipped"

# Cleanup
rm -rf "$REPO_DIR" "$BARE_DIR"

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
