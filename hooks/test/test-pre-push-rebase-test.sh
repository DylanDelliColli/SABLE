#!/usr/bin/env bash
# test-pre-push-rebase-test.sh — Tests for pre-push-rebase-test.sh routing
#
# Runs the hook against synthetic input + temporary fixture repos to
# verify:
#   - Skips when not a git push command
#   - Skips when --force is used (manager path only — the worker deny precedes it)
#   - GATES manager-typed subagent pushes; mechanically DENIES worker/unnamed
#     subagent pushes (SABLE-404), with a SABLE_WORKER_PUSH_OVERRIDE=1 hatch
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

# Test 4 (v3 SABLE-404): a bare agent_id (no agent_type) is an unnamed worker —
# its push is DENIED, not skipped (inverts the v2 stand-down). env identity is
# ignored when agent_id is present, so MGR_ENV does not make it a manager.
assert_deny "subagent without agent_type → worker push DENIED" "$MGR_ENV" "git push" "/tmp" "worker subagents do not push" "subagent-123"

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

# ---------- Shared matcher tests (SABLE-jpr / SABLE-0u1) ----------
# The pre-push gate must fire for real git push variants (positives) and
# must NOT fire for commands where "git push" only appears as a quoted string
# argument or in a different word like "git pushd" (negatives).
#
# We use PHASE=skip + passing typecheck so a positive match produces an
# additionalContext response (phase skipped), while a negative match
# produces no output at all (hook exits 0 silently).
MATCHER_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"

# Test 12: 'git -C <path> push' reaches the gate (SABLE-jpr)
assert_context "matcher: 'git -C <path> push' reaches gate" \
  "$MATCHER_ENV" "git -C $REPO_DIR push origin main" "$REPO_DIR" "phase skipped"

# Test 13: 'git -c a=b push origin main' reaches the gate
assert_context "matcher: 'git -c a=b push origin main' reaches gate" \
  "$MATCHER_ENV" "git -c http.extraheader=Authorization:bearer push origin main" "$REPO_DIR" "phase skipped"

# Test 14: 'git --no-pager push' reaches the gate
assert_context "matcher: 'git --no-pager push' reaches gate" \
  "$MATCHER_ENV" "git --no-pager push" "$REPO_DIR" "phase skipped"

# Test 15: 'bd create --description="mentions git push"' does NOT reach gate (SABLE-0u1)
# This should exit 0 silently — no additionalContext, no deny
assert_allow "matcher: text mention 'git push' in description does NOT trigger gate" \
  "$MGR_ENV" 'bd create --description="Please git push to deploy"' "$REPO_DIR"

# Test 16: 'echo git pushed' does NOT reach gate
assert_allow "matcher: 'echo git pushed' does NOT trigger gate" \
  "$MGR_ENV" "echo 'done, git pushed'" "$REPO_DIR"

# Test 17: 'git pushd' does NOT reach gate
assert_allow "matcher: 'git pushd' does NOT trigger gate" \
  "$MGR_ENV" "git pushd" "$REPO_DIR"

# Test 18: 'SABLE_SKIP_PRE_PUSH=1 git push' reaches gate (env-assignment prefix, SABLE-531)
# The env-assignment is part of the command string seen by the hook; the hook itself is
# invoked normally (no SABLE_SKIP_PRE_PUSH in the env-i context).  With TEST_PHASE=skip
# and a passing typecheck the hook should produce additionalContext (phase skipped),
# confirming it passed the matcher and entered the gate.
assert_context "matcher: 'SABLE_SKIP_PRE_PUSH=1 git push' reaches gate (env-assignment prefix)" \
  "$MATCHER_ENV" "SABLE_SKIP_PRE_PUSH=1 git push" "$REPO_DIR" "phase skipped"

# ===================================================================
# v3 identity gate — SABLE-404 / SABLE-yzl
# Manager-typed subagent pushes are GATED (phases run); worker-typed and
# unnamed-subagent pushes are mechanically DENIED, with a
# SABLE_WORKER_PUSH_OVERRIDE=1 escape hatch. Main-session and env-manager
# behavior is unchanged.
# ===================================================================

v3pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
v3fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# Hermetic registry so tarzan/sherlock resolve deterministically; Explore is
# intentionally absent → resolves as an unregistered worker type.
V3_YAML="/tmp/sable-test-pre-push-agents.yaml"
cat > "$V3_YAML" <<'YAML'
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
  sherlock:
    type: auditor
YAML

# make_typed_input <cmd> <cwd> <agent_id> <agent_type>
make_typed_input() {
  python3 -c "
import json, sys
cmd, cwd, aid, atype = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
out = {'tool_input': {'command': cmd}, 'cwd': cwd}
if aid: out['agent_id'] = aid
if atype: out['agent_type'] = atype
print(json.dumps(out))
" "$1" "$2" "$3" "$4"
}

# run_typed <env_prefix> <cmd> <cwd> <agent_id> <agent_type>
run_typed() {
  local env_prefix="$1" cmd="$2" cwd="$3" aid="$4" atype="$5"
  make_typed_input "$cmd" "$cwd" "$aid" "$atype" | \
    env -i PATH="$PATH" SABLE_AGENTS_YAML="$V3_YAML" $env_prefix bash "$HOOK" 2>/dev/null || echo "RUN_ERR:$?"
}

GATE_ENV="SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=false SABLE_PRE_PUSH_TEST_PHASE=skip"
WORKER_MSG="worker subagents do not push"

# (a) manager subagent (tarzan) push is GATED: phases run → static deny proves it engaged.
OUT=$(run_typed "$GATE_ENV" "git push" "$REPO_DIR" "mgr-sub-1" "tarzan")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "phase 2 (static)"; then
  v3pass "(a) manager-subagent (tarzan) push is GATED — reaches static phase"
else
  v3fail "(a) manager-subagent (tarzan) push is GATED — reaches static phase" "got: ${OUT:0:300}"
fi

# (b) worker subagent (Explore) push is DENIED with the worker message.
OUT=$(run_typed "" "git push origin main" "/tmp" "wkr-1" "Explore")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "$WORKER_MSG"; then
  v3pass "(b) worker-subagent (Explore) push is DENIED with the worker message"
else
  v3fail "(b) worker-subagent (Explore) push is DENIED with the worker message" "got: ${OUT:0:300}"
fi

# (c) worker deny is overridable via SABLE_WORKER_PUSH_OVERRIDE=1 → allow + context.
OUT=$(run_typed "SABLE_WORKER_PUSH_OVERRIDE=1" "git push origin main" "/tmp" "wkr-1" "Explore")
if echo "$OUT" | grep -q '"additionalContext"' && echo "$OUT" | grep -qF "SABLE_WORKER_PUSH_OVERRIDE"; then
  v3pass "(c) worker deny overridable via SABLE_WORKER_PUSH_OVERRIDE=1 (allow + context)"
else
  v3fail "(c) worker deny overridable via SABLE_WORKER_PUSH_OVERRIDE=1 (allow + context)" "got: ${OUT:0:300}"
fi

# (d) agent_id without agent_type is DENIED (unnamed worker).
OUT=$(run_typed "" "git push" "/tmp" "wkr-2" "")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "$WORKER_MSG"; then
  v3pass "(d) agent_id without agent_type is DENIED (unnamed worker)"
else
  v3fail "(d) agent_id without agent_type is DENIED (unnamed worker)" "got: ${OUT:0:300}"
fi

# (e) main-session push with no identity stands down unchanged (allow).
OUT=$(run_typed "" "git push" "/tmp" "" "")
if [ -z "$OUT" ]; then
  v3pass "(e) main-session push (no identity) stands down unchanged"
else
  v3fail "(e) main-session push (no identity) stands down unchanged" "got: ${OUT:0:300}"
fi

# (f) registered non-manager subagent (sherlock) push is DENIED.
OUT=$(run_typed "" "git push" "/tmp" "aud-1" "sherlock")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "$WORKER_MSG"; then
  v3pass "(f) registered non-manager (sherlock) push is DENIED"
else
  v3fail "(f) registered non-manager (sherlock) push is DENIED" "got: ${OUT:0:300}"
fi

# (g) env-identified manager (chuck, no agent_id) push remains GATED.
OUT=$(run_typed "CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager $GATE_ENV" "git push" "$REPO_DIR" "" "")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "phase 2 (static)"; then
  v3pass "(g) env-manager (chuck) push remains GATED — reaches static phase"
else
  v3fail "(g) env-manager (chuck) push remains GATED — reaches static phase" "got: ${OUT:0:300}"
fi

# (h) worker deny takes precedence over --force skip.
OUT=$(run_typed "" "git push --force origin main" "/tmp" "wkr-3" "Explore")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "$WORKER_MSG"; then
  v3pass "(h) worker deny takes precedence over --force skip"
else
  v3fail "(h) worker deny takes precedence over --force skip" "got: ${OUT:0:300}"
fi

# (i) worker identity with a non-push command is untouched (allow).
OUT=$(run_typed "" "git status" "/tmp" "wkr-4" "Explore")
if [ -z "$OUT" ]; then
  v3pass "(i) worker non-push command (git status) is untouched"
else
  v3fail "(i) worker non-push command (git status) is untouched" "got: ${OUT:0:300}"
fi

# (j-i) infrastructure failure: malformed JSON, no env → fails open (allow).
OUT=$(printf 'not valid json {' | env -i PATH="$PATH" bash "$HOOK" 2>/dev/null || echo "RUN_ERR:$?")
if [ -z "$OUT" ]; then
  v3pass "(j-i) malformed hook input fails open (allow)"
else
  v3fail "(j-i) malformed hook input fails open (allow)" "got: ${OUT:0:300}"
fi

# (j-ii) clean worker resolution with an unreadable registry still DENIES
# (identity resolution itself succeeded: agent_type present, type unresolvable →
# IS_SUBAGENT=1, IS_MANAGER=0 → worker deny). The later SABLE_AGENTS_YAML wins.
OUT=$(run_typed "SABLE_AGENTS_YAML=/nonexistent/registry.yaml" "git push" "/tmp" "wkr-5" "Explore")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "$WORKER_MSG"; then
  v3pass "(j-ii) unreadable registry: clean worker resolution still DENIES"
else
  v3fail "(j-ii) unreadable registry: clean worker resolution still DENIES" "got: ${OUT:0:300}"
fi

# (k) SABLE_WORKER_PUSH_OVERRIDE does not weaken the manager gate.
OUT=$(run_typed "SABLE_WORKER_PUSH_OVERRIDE=1 $GATE_ENV" "git push" "$REPO_DIR" "mgr-sub-2" "tarzan")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "phase 2 (static)"; then
  v3pass "(k) SABLE_WORKER_PUSH_OVERRIDE does not weaken the manager gate"
else
  v3fail "(k) SABLE_WORKER_PUSH_OVERRIDE does not weaken the manager gate" "got: ${OUT:0:300}"
fi

# ===================================================================
# SABLE-041: the gate must act on the `git -C <repo>` target, not the
# shell cwd. cwd is a NON-repo; -C points at the real fixture repo. The
# buggy hook checks cwd/.git (absent) and no-ops (allow); the fixed hook
# resolves the -C dir, finds .git, and the forced static failure DENIES.
# ===================================================================
C041_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=false SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_deny "SABLE-041: 'git -C <repo> push' from a non-repo cwd resolves the -C dir (static gate runs)" \
  "$C041_ENV" "git -C $REPO_DIR push" "/tmp/sable-041-nonrepo-cwd" "phase 2 (static)"

# Cleanup
rm -rf "$REPO_DIR" "$BARE_DIR" "$V3_YAML"

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
