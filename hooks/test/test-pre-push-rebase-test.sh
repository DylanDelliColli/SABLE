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
# Absolute repo root, resolved once up front — needed by the SABLE-digiy
# fixtures below, which `cd` into throwaway fixture repos and must not rely
# on a relative $0 resolving correctly after the CWD has moved.
REPO_ROOT="$(cd "$(dirname "$HOOK")/../.." && pwd)"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

# Per-invocation unique fixture root (SABLE-z776). Every fixture repo, bare
# origin, and agents.yaml this suite creates lives under TMPROOT — an mktemp -d
# dir unique to THIS process. The suite formerly hardcoded shared /tmp paths
# (/tmp/sable-test-pre-push-repo, /tmp/sable-test-4amz-repo, …); once the .sable
# testCommand went live (SABLE-hml) every SABLE-repo push runs this suite in the
# pre-push gate, so concurrent fleet pushes — and the nested case where the gate
# runs the suite while the suite invokes the gate — raced on those shared paths,
# clobbering each other's fixtures and flaking the gate nondeterministically.
# Scoping everything under a unique root, and tearing down ONLY TMPROOT, makes
# concurrent and nested runs collision-free. See test-pre-push-rebase-concurrency.sh.
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-pre-push.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

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
  cd "$dir" || return 1
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
  cd "$dir" || return 1
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

REPO_DIR="$TMPROOT/pre-push-repo"
BARE_DIR="$TMPROOT/pre-push-bare.git"
rm -rf "$REPO_DIR" "$BARE_DIR"

# Create a bare repo to serve as `origin` so `git fetch origin` succeeds.
git init -q --bare "$BARE_DIR"

# Clone from bare → working repo with origin remote configured.
git clone -q "$BARE_DIR" "$REPO_DIR"
# Guard the cd (SABLE-xydb): if it ever fails, ABORT before the destructive
# echo>README / git add -A / commit / push run — otherwise those side-effects
# execute in whatever CWD we landed in (the real worktree), which under the old
# shared-/tmp race truncated the real README and pushed it to the real origin.
cd "$REPO_DIR" || { echo "FATAL: cd to fixture repo $REPO_DIR failed — aborting so fixture git ops never touch the real worktree"; exit 2; }
git config user.email "test@test"
git config user.name "Test"
echo "x" > README.md
git add -A
git commit -q -m "init"
# Push by EXPLICIT bare path, never the remote name 'origin', so a misrouted
# invocation can never reach a real upstream (defense-in-depth for SABLE-xydb).
git push -q "$BARE_DIR" HEAD:refs/heads/main 2>/dev/null
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

# ---------- .sable testCommand resolution tests (SABLE-hml) ----------
# detect_test_cmd previously only checked $SABLE_TEST_COMMAND then a fixed
# manifest list (package.json/pyproject.toml/Cargo.toml/go.mod); a bash/hook
# repo like SABLE itself matches none of those, so the TEST phase silently
# no-op'd (live incident: chuck, 2026-07-07 — the TDD-enforcement hooks
# batch itself shipped untested). sable_resolve_test_command (lib-identity.sh)
# now also honors a checked-in .sable file / repo-local git config — these
# tests prove the HOOK actually wires that resolution into phase 3 end to
# end, not just that the lib function works in isolation (covered separately
# in test-lib-identity.sh).
SABLE_TESTCMD_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true"

# Test 11b: .sable testCommand that fails → phase 3 denies, message names the
# RESOLVED command (proves detect_test_cmd read .sable, not env/manifest)
echo "testCommand=exit 42" > "$REPO_DIR/.sable"
assert_deny "«.sable» testCommand resolved and enforced → failing command denies phase 3" \
  "$SABLE_TESTCMD_ENV" "git push" "$REPO_DIR" "exit 42"

# Test 11c: .sable testCommand that passes → phase 3 runs clean, no deny and
# no "no test command detected" fallback (proves manifest auto-detect was
# bypassed in favor of the resolved .sable value)
echo "testCommand=true" > "$REPO_DIR/.sable"
assert_allow "«.sable» testCommand resolved and enforced → passing command allows push" \
  "$SABLE_TESTCMD_ENV" "git push" "$REPO_DIR"

# Test 11d: repo-local git config wins over the .sable file (precedence
# mirrors sable_resolve_integration_branch's config > .sable ordering)
git -C "$REPO_DIR" config sable.testCommand "exit 43"
assert_deny "repo-local git config testCommand wins over .sable file" \
  "$SABLE_TESTCMD_ENV" "git push" "$REPO_DIR" "exit 43"
git -C "$REPO_DIR" config --unset sable.testCommand

rm -f "$REPO_DIR/.sable"

# ---------- testTimeout resolution tests (SABLE-pf0g) ----------
# pre-push-rebase-test.sh previously read TEST_TIMEOUT only from
# $SABLE_PRE_PUSH_TEST_TIMEOUT with no per-repo override — a genuinely
# passing test suite that legitimately needs more than the 60s default
# (e.g. under fleet contention) had no way to raise it for a single repo.
# sable_resolve_test_timeout (lib-identity.sh) now also honors a repo-local
# git config / checked-in .sable file; these tests prove the HOOK wires that
# resolution into phase 3 end to end (the lib function itself is covered
# separately in test-lib-identity.sh).
SABLE_TIMEOUT_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_TIMEOUT=1"
echo "testCommand=sleep 2 && exit 0" > "$REPO_DIR/.sable"

# Test 11e: with only the 1s env default in effect, a test command that takes
# 2s is killed by `timeout` → phase 3 denies citing the exceeded timeout.
assert_deny "no per-repo override → 1s env default kills a 2s test command" \
  "$SABLE_TIMEOUT_ENV" "git push" "$REPO_DIR" "exceeded the"

# Test 11f: repo-local git config sable.testTimeout=5 overrides the 1s env
# default → the same 2s test command now completes inside the window and the
# push is allowed (proves the config override reaches the hook's `timeout`
# call, not just the resolver function in isolation).
git -C "$REPO_DIR" config sable.testTimeout 5
assert_allow "repo-local git config testTimeout overrides 1s env default → 2s test command completes" \
  "$SABLE_TIMEOUT_ENV" "git push" "$REPO_DIR"
git -C "$REPO_DIR" config --unset sable.testTimeout

rm -f "$REPO_DIR/.sable"

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
V3_YAML="$TMPROOT/pre-push-agents.yaml"
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
# market-brief-package-73t4: instance-suffixed manager identity at the gate
# (Lincoln ruling mechanism b: EXPLICIT INSTANCE REGISTRATION).
#   (b1) An UNREGISTERED instance (tarzan-2; base tarzan IS a registered manager)
#        is still worker-DENIED, but the message now names the missing spawn-time
#        registration instead of implying the manager is a worker.
#   (b2) Once the instance is REGISTERED, the SAME tarzan-2 push is GATED as a
#        manager (reaches the static phase) — the acceptance, end-to-end, with
#        NO SABLE_WORKER_PUSH_OVERRIDE.
#   (b3) The gate never auto-registers: a denied instance's push attempt must not
#        write a registry entry (privilege comes from the registry, not the name).
# ===================================================================

# (b1) uses V3_YAML (has tarzan, NOT tarzan-2) → worker-deny + registration hint.
OUT=$(run_typed "" "git push" "/tmp" "inst-1" "tarzan-2")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' \
   && echo "$OUT" | grep -qF "$WORKER_MSG" \
   && echo "$OUT" | grep -qF "unregistered instance" \
   && echo "$OUT" | grep -qF "register-instance tarzan-2"; then
  v3pass "(73t4-b1) unregistered instance tarzan-2 is DENIED with a registration hint"
else
  v3fail "(73t4-b1) unregistered instance tarzan-2 is DENIED with a registration hint" "got: ${OUT:0:400}"
fi

# (b2) a registry WITH the instance registered → tarzan-2 is a manager → GATED.
T73_YAML="$TMPROOT/73t4-agents.yaml"
cat > "$T73_YAML" <<'YAML'
agents:
  tarzan:
    type: one_off_manager
  tarzan-2:
    type: one_off_manager
    instance_of: tarzan
YAML
OUT=$(make_typed_input "git push" "$REPO_DIR" "inst-2" "tarzan-2" | \
  env -i PATH="$PATH" SABLE_AGENTS_YAML="$T73_YAML" $GATE_ENV bash "$HOOK" 2>/dev/null || echo "RUN_ERR:$?")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "phase 2 (static)"; then
  v3pass "(73t4-b2) REGISTERED instance tarzan-2 push is GATED as a manager (reaches static phase)"
else
  v3fail "(73t4-b2) REGISTERED instance tarzan-2 push is GATED as a manager (reaches static phase)" "got: ${OUT:0:400}"
fi

# (b3) the DENY path (b1 fixture) must not have written a tarzan-2 entry.
run_typed "" "git push" "/tmp" "inst-3" "tarzan-2" >/dev/null 2>&1
if grep -qF "tarzan-2" "$V3_YAML"; then
  v3fail "(73t4-b3) gate must NOT auto-register a denied instance" "V3_YAML gained a tarzan-2 entry"
else
  v3pass "(73t4-b3) gate does not auto-register a denied instance (registry unchanged)"
fi
rm -f "$T73_YAML"

# ===================================================================
# SABLE-041: the gate must act on the `git -C <repo>` target, not the
# shell cwd. cwd is a NON-repo; -C points at the real fixture repo. The
# buggy hook checks cwd/.git (absent) and no-ops (allow); the fixed hook
# resolves the -C dir, finds .git, and the forced static failure DENIES.
# ===================================================================
C041_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=false SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_deny "SABLE-041: 'git -C <repo> push' from a non-repo cwd resolves the -C dir (static gate runs)" \
  "$C041_ENV" "git -C $REPO_DIR push" "$TMPROOT/sable-041-nonrepo-cwd" "phase 2 (static)"

# ===================================================================
# market-brief-package-fofc: integration-branch self-push must NOT rebase onto
# a DIFFERENT base. Fixture: base branch 'dev' advances with a commit that
# CONFLICTS with the local-only integration stack; pushing the integration
# branch itself must retarget Phase-1 rebase to its own published tip
# (origin/llm-integration, a fast-forward-safe no-op) instead of origin/dev.
#   RED  (pre-fix): hook rebases llm-integration onto origin/dev → conflict →
#                   DENY "phase 1 (rebase)".
#   GREEN(post-fix): retarget → BEHIND=0 → no rebase → "phase skipped".
# ===================================================================
FOFC_BARE="$TMPROOT/fofc-bare.git"
FOFC_REPO="$TMPROOT/fofc-repo"
rm -rf "$FOFC_BARE" "$FOFC_REPO"
git init -q --bare "$FOFC_BARE"
git clone -q "$FOFC_BARE" "$FOFC_REPO" 2>/dev/null
(
  cd "$FOFC_REPO" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B dev
  echo base > shared.txt; git add shared.txt; git commit -q -m d1
  git push -q origin dev
  git checkout -q -b llm-integration
  echo L1 > stack.txt; git add stack.txt; git commit -q -m L1
  echo L2 >> stack.txt; git add stack.txt; git commit -q -m L2
  git push -q origin llm-integration
  git checkout -q dev
  echo devX > stack.txt; git add stack.txt; git commit -q -m devConflict
  git push -q origin dev
  git checkout -q llm-integration
)
FOFC_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/dev SABLE_INTEGRATION_BRANCH=llm-integration SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_context "fofc: pushing integration branch retargets rebase to origin/<branch> (no rebase onto origin/dev)" \
  "$FOFC_ENV" "git push origin llm-integration" "$FOFC_REPO" "phase skipped"
rm -rf "$FOFC_BARE" "$FOFC_REPO"

# ===================================================================
# market-brief-package-2u25: per-repo integration-branch resolution.
# Repo-local git config (sable.integrationBranch) must win over a foreign
# SABLE_BASE_BRANCH env value inherited from a DIFFERENT repo's session (the
# session-global-vs-per-repo bug). Fixture: this repo's own integration
# branch is 'tmux-only' (repo-local config), while env says
# SABLE_BASE_BRANCH=origin/llm-integration — a ref that does not exist in
# THIS repo, simulating the cross-repo env leak. main has a commit that
# CONFLICTS with the integration stack, so a misrouted rebase onto it is
# observable.
#   RED  (pre-fix): INTEGRATION_BRANCH derives from env → "llm-integration"
#                   != CURRENT_BRANCH "tmux-only" → fofc case never matches →
#                   BASE_BRANCH falls back to origin/main → conflicting
#                   rebase → DENY "phase 1 (rebase)".
#   GREEN(post-fix): INTEGRATION_BRANCH resolves via repo-local config →
#                   "tmux-only" == CURRENT_BRANCH → retarget to
#                   origin/tmux-only (fast-forward-safe no-op) → "phase skipped".
# ===================================================================
R2U25_BARE="$TMPROOT/2u25-bare.git"
R2U25_REPO="$TMPROOT/2u25-repo"
rm -rf "$R2U25_BARE" "$R2U25_REPO"
git init -q --bare "$R2U25_BARE"
git clone -q "$R2U25_BARE" "$R2U25_REPO" 2>/dev/null
(
  cd "$R2U25_REPO" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  echo base > shared.txt; git add shared.txt; git commit -q -m base
  git push -q origin main
  git checkout -q -b tmux-only
  echo I1 > stack.txt; git add stack.txt; git commit -q -m I1
  echo I2 >> stack.txt; git add stack.txt; git commit -q -m I2
  git push -q origin tmux-only
  git checkout -q main
  echo mainX > stack.txt; git add stack.txt; git commit -q -m mainConflict
  git push -q origin main
  git checkout -q tmux-only
  git config sable.integrationBranch tmux-only
)
R2U25_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/llm-integration SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_context "market-brief-package-2u25: repo-local sable.integrationBranch wins over foreign SABLE_BASE_BRANCH env (retarget, no conflicting rebase)" \
  "$R2U25_ENV" "git push origin tmux-only" "$R2U25_REPO" "phase skipped"
rm -rf "$R2U25_BARE" "$R2U25_REPO"

# ===================================================================
# market-brief-package-yz5y: re-parent guard for a LOCAL-ONLY integration
# branch. A branch cut from integration HEAD passes; a re-parented branch
# (integration HEAD is NOT its ancestor) is DENIED. Guard is dormant once
# origin/<INT> is published (checked here by leaving llm-integration unpushed).
#   RED  (pre-fix): no guard → re-parented push is allowed ("phase skipped").
#   GREEN(post-fix): re-parented push is DENIED "re-parent guard".
# The good-branch case is a false-positive guard: it must pass before AND after.
# ===================================================================
YZ_BARE="$TMPROOT/yz5y-bare.git"
YZ_REPO="$TMPROOT/yz5y-repo"
rm -rf "$YZ_BARE" "$YZ_REPO"
git init -q --bare "$YZ_BARE"
git clone -q "$YZ_BARE" "$YZ_REPO" 2>/dev/null
(
  cd "$YZ_REPO" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  echo r0 > root.txt; git add root.txt; git commit -q -m root
  git checkout -q -b llm-integration
  echo i2 > int.txt; git add int.txt; git commit -q -m i2
  git checkout -q -b wk-good llm-integration
  echo w1 > w1.txt; git add w1.txt; git commit -q -m w1
  git checkout -q -b wk-reparented main
  echo w2 > w2.txt; git add w2.txt; git commit -q -m w2
)
# llm-integration is LOCAL-ONLY here (never pushed) → guard armed.
YZ_ENV="$MGR_ENV SABLE_INTEGRATION_BRANCH=llm-integration SABLE_BASE_BRANCH=origin/llm-integration SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"
( cd "$YZ_REPO" && git checkout -q wk-reparented )
assert_deny "yz5y: re-parented branch (missing integration HEAD) is DENIED by the guard" \
  "$YZ_ENV" "git push origin wk-reparented" "$YZ_REPO" "re-parent guard"
( cd "$YZ_REPO" && git checkout -q wk-good )
assert_context "yz5y: branch cut from integration HEAD passes the guard (not blocked)" \
  "$YZ_ENV" "git push origin wk-good" "$YZ_REPO" "phase skipped"
rm -rf "$YZ_BARE" "$YZ_REPO"

# ===================================================================
# SABLE-4amz: phase-1 rebase base must default to the RESOLVED integration
# branch when SABLE_BASE_BRANCH is unset — the old unconditional origin/main
# default re-parented worker branches on repos whose PUBLISHED integration
# branch is not main, rewriting every carried SHA at push time (manufactured
# the wk-tripwire-pytest corruption, 2026-07-09). Fixture: published non-main
# integration branch 'tmux-only' (repo-local sable.integrationBranch), a
# worker branch cut from it, and origin/main advanced with a CONFLICTING
# commit so a misrouted rebase onto main is observable.
#   Case 1 RED (pre-fix): env unset → BASE=origin/main → conflicting rebase
#                → deny "phase 1"; GREEN: BASE=origin/tmux-only → clean
#                rebase over i3 → "phase skipped".
#   Case 2 (SABLE-1238): a leaked SABLE_BASE_BRANCH=origin/main must NOT force
#                the base or block the push — the repo's authoritative config
#                (sable.integrationBranch=tmux-only, published) wins, so phase 1
#                rebases cleanly onto origin/tmux-only and the push proceeds
#                ("phase skipped"). Pre-SABLE-1238 this DENIED via the wrong-base
#                guard, whose remediation ("unset SABLE_BASE_BRANCH and retry")
#                was unreachable: a PreToolUse hook can't read the push's env.
# ===================================================================
AMZ_BARE="$TMPROOT/4amz-bare.git"
AMZ_REPO="$TMPROOT/4amz-repo"
rm -rf "$AMZ_BARE" "$AMZ_REPO"
git init -q --bare "$AMZ_BARE"
git clone -q "$AMZ_BARE" "$AMZ_REPO" 2>/dev/null
(
  cd "$AMZ_REPO" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  echo base > stack.txt; git add stack.txt; git commit -q -m base
  git push -q origin main
  git checkout -q -b tmux-only
  echo I1 >> stack.txt; git add stack.txt; git commit -q -m I1
  git push -q origin tmux-only
  git checkout -q -b wk-4amz-w
  echo w1 > w.txt; git add w.txt; git commit -q -m w1
  git checkout -q tmux-only
  echo i3 > i3.txt; git add i3.txt; git commit -q -m i3
  git push -q origin tmux-only
  git checkout -q main
  echo mainX > stack.txt; git add stack.txt; git commit -q -m mainConflict
  git push -q origin main
  git checkout -q wk-4amz-w
  git config sable.integrationBranch tmux-only
)
AMZ_ENV="$MGR_ENV SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_context "4amz: unset SABLE_BASE_BRANCH → phase-1 rebases onto origin/<INT>, not origin/main (clean pass)" \
  "$AMZ_ENV" "git push origin wk-4amz-w" "$AMZ_REPO" "phase skipped"
AMZ_LEAK_ENV="$AMZ_ENV SABLE_BASE_BRANCH=origin/main"
assert_context "SABLE-1238: leaked SABLE_BASE_BRANCH=origin/main is IGNORED — authoritative config rebases onto origin/tmux-only (clean pass, no deny)" \
  "$AMZ_LEAK_ENV" "git push origin wk-4amz-w" "$AMZ_REPO" "phase skipped"
rm -rf "$AMZ_BARE" "$AMZ_REPO"

# ===================================================================
# SABLE-rzsb S4 / SABLE-h07t: manifest search widened to UPWARD +
# SUBDIR (not cwd-only), plus a new never-skippable BUILD phase.
# ===================================================================

# call_hook_fn <function-name> [args...]
# Sources the hook in a subshell (functions only — the
# `[ "${BASH_SOURCE[0]}" = "${0}" ]` guard keeps main() from auto-running on
# source) and invokes <function-name>, so the manifest-search helpers and
# detect_* auto-detectors can be unit-tested directly against fixture
# directories, without going through the full JSON-stdin hook harness.
call_hook_fn() {
  local fn="$1"; shift
  ( . "$HOOK"; "$fn" "$@" )
}

# ---------- UNIT: sable_find_manifest_dir ----------

# Test U1: upward search — cwd is nested BELOW the manifest (repo root).
U1_ROOT="$TMPROOT/unit-upward"
rm -rf "$U1_ROOT"
mkdir -p "$U1_ROOT/nested/deep"
git init -q "$U1_ROOT"
git -C "$U1_ROOT" config user.email t@t
git -C "$U1_ROOT" config user.name t
echo '{}' > "$U1_ROOT/package.json"
GOT=$(call_hook_fn sable_find_manifest_dir "$U1_ROOT/nested/deep" "" package.json)
if [ -n "$GOT" ] && [ -f "$GOT/package.json" ]; then
  PASS=$((PASS+1)); echo "PASS: sable_find_manifest_dir resolves UPWARD from a nested cwd to a parent-dir manifest"
else
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  sable_find_manifest_dir upward search"
  echo "FAIL: sable_find_manifest_dir resolves UPWARD from a nested cwd to a parent-dir manifest (got: [$GOT])"
fi
rm -rf "$U1_ROOT"

# Test U2: subdir search — repo root has NO manifest, but a file changed on
# this push (relative to base_branch) lives under a subdir that does.
U2_ROOT="$TMPROOT/unit-subdir"
rm -rf "$U2_ROOT"
git init -q "$U2_ROOT"
git -C "$U2_ROOT" config user.email t@t
git -C "$U2_ROOT" config user.name t
(
  cd "$U2_ROOT" || exit 1
  echo root > README.md
  git add -A; git commit -q -m base
  BASE_REF=$(git rev-parse --abbrev-ref HEAD)
  git checkout -q -b feature
  mkdir -p sub/src
  echo '{"scripts":{"test":"exit 0"}}' > sub/package.json
  echo x > sub/src/foo.js
  git add -A; git commit -q -m "add subdir manifest"
  echo "$BASE_REF" > "$TMPROOT/u2-base-ref"
)
U2_BASE=$(cat "$TMPROOT/u2-base-ref")
GOT=$(call_hook_fn sable_find_manifest_dir "$U2_ROOT" "$U2_BASE" package.json)
if [ "$GOT" = "$U2_ROOT/sub" ] || { [ -n "$GOT" ] && [ -f "$GOT/package.json" ] && [ "$GOT" != "$U2_ROOT" ]; }; then
  PASS=$((PASS+1)); echo "PASS: sable_find_manifest_dir resolves INTO a subdir via a changed file when repo root has no manifest"
else
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  sable_find_manifest_dir subdir search"
  echo "FAIL: sable_find_manifest_dir resolves INTO a subdir via a changed file when repo root has no manifest (got: [$GOT])"
fi
rm -rf "$U2_ROOT" "$TMPROOT/u2-base-ref"

# Test U3: no manifest anywhere (upward or subdir) — resolves cleanly to
# empty, does not crash the caller under `set -e`.
U3_ROOT="$TMPROOT/unit-none"
rm -rf "$U3_ROOT"
git init -q "$U3_ROOT"
git -C "$U3_ROOT" config user.email t@t
git -C "$U3_ROOT" config user.name t
(
  cd "$U3_ROOT" || exit 1
  echo root > README.md
  git add -A; git commit -q -m base
)
GOT="unset"
RC=0
GOT=$(call_hook_fn sable_find_manifest_dir "$U3_ROOT" "master" package.json pyproject.toml Cargo.toml go.mod) || RC=$?
TESTCMD_GOT=$(call_hook_fn detect_test_cmd "$U3_ROOT" "master") || RC=$?
if [ "$RC" -eq 0 ] && [ -z "$GOT" ] && [ -z "$TESTCMD_GOT" ]; then
  PASS=$((PASS+1)); echo "PASS: no manifest found anywhere (upward or subdir) — resolves empty, does not crash"
else
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  sable_find_manifest_dir no-manifest-found"
  echo "FAIL: no manifest found anywhere (upward or subdir) — resolves empty, does not crash (rc=$RC got=[$GOT] testcmd=[$TESTCMD_GOT])"
fi
rm -rf "$U3_ROOT"

# ---------- E2E: monorepo/subdir-manifest gate + build phase ----------

# Test E1: monorepo-shaped worktree (manifest in a subdir, not repo root) —
# the pre-push gate DETECTS and RUNS the real suite, not a no-op. The
# subdir test script exits 7 (a distinctive, non-npm-internal code) so the
# denial can only come from that script actually having been executed.
MONO_BARE="$TMPROOT/mono-bare.git"
MONO_REPO="$TMPROOT/mono-repo"
rm -rf "$MONO_BARE" "$MONO_REPO"
git init -q --bare "$MONO_BARE"
git clone -q "$MONO_BARE" "$MONO_REPO" 2>/dev/null
(
  cd "$MONO_REPO" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  echo root > README.md
  git add -A; git commit -q -m base
  git push -q origin main
  mkdir -p sub
  cat > sub/package.json <<'EOF'
{"scripts": {"test": "exit 7"}}
EOF
  git add -A; git commit -q -m "add subdir manifest + failing suite"
)
MONO_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true"
assert_deny "SABLE-rzsb S4: monorepo subdir-manifest worktree — gate DETECTS+RUNS the real suite (not a no-op)" \
  "$MONO_ENV" "git push" "$MONO_REPO" "exit 7"
rm -rf "$MONO_BARE" "$MONO_REPO"

# Test E2: regression — a manifest AT the worktree root (the pre-widening
# case) is still detected and its real suite still runs.
ROOTMAN_BARE="$TMPROOT/rootman-bare.git"
ROOTMAN_REPO="$TMPROOT/rootman-repo"
rm -rf "$ROOTMAN_BARE" "$ROOTMAN_REPO"
git init -q --bare "$ROOTMAN_BARE"
git clone -q "$ROOTMAN_BARE" "$ROOTMAN_REPO" 2>/dev/null
(
  cd "$ROOTMAN_REPO" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  echo root > README.md
  git add -A; git commit -q -m base
  git push -q origin main
  cat > package.json <<'EOF'
{"scripts": {"test": "exit 9"}}
EOF
  git add -A; git commit -q -m "add root manifest + failing suite"
)
ROOTMAN_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true"
assert_deny "SABLE-rzsb S4 regression: root-manifest worktree is still detected (upward+subdir widening doesn't break the base case)" \
  "$ROOTMAN_ENV" "git push" "$ROOTMAN_REPO" "exit 9"
rm -rf "$ROOTMAN_BARE" "$ROOTMAN_REPO"

# Test E3: build-phase fixture — a stand-in "page-export error" (a failing
# build script) that a passing typecheck+test would NOT catch trips the new
# BUILD phase. Mirrors the existing typecheck-override style (Test 6/7):
# real npm auto-detection of the build script from package.json, not an
# override, is what's under test here.
BUILD_BARE="$TMPROOT/build-bare.git"
BUILD_REPO="$TMPROOT/build-repo"
rm -rf "$BUILD_BARE" "$BUILD_REPO"
git init -q --bare "$BUILD_BARE"
git clone -q "$BUILD_BARE" "$BUILD_REPO" 2>/dev/null
(
  cd "$BUILD_REPO" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  echo root > README.md
  git add -A; git commit -q -m base
  git push -q origin main
  cat > package.json <<'EOF'
{"scripts": {"build": "exit 42", "test": "exit 0"}}
EOF
  git add -A; git commit -q -m "add failing build script (page-export-error stand-in)"
)
BUILD_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_deny "SABLE-rzsb S4: build-phase fixture (page-export-error stand-in passing typecheck) trips the new BUILD phase" \
  "$BUILD_ENV" "git push" "$BUILD_REPO" "phase 3 (build)"

# Test E4: a passing build (script exits 0) does not block the push — the
# new phase isn't a false-positive gate.
(
  cd "$BUILD_REPO" || exit 1
  cat > package.json <<'EOF'
{"scripts": {"build": "exit 0", "test": "exit 0"}}
EOF
  git add -A; git commit -q -m "fix build script"
)
assert_context "SABLE-rzsb S4: passing build script does not block the push (build phase is not a false-positive gate)" \
  "$BUILD_ENV" "git push" "$BUILD_REPO" "phase skipped"
rm -rf "$BUILD_BARE" "$BUILD_REPO"

# ---------- SABLE-digiy: fixture-tripwire wired into pre-push STATIC phase ----------
# bin/sable-fixture-tripwire (SABLE-0ssz.2) previously ran ONLY at ci-verify
# (remote); a violating touched fixture-test file passed the local pre-push
# suite and only RED'd at the seat (jd5fj.14: 3c's planted-poison control was
# local-suite 22/22 green, caught only remotely). These fixtures exercise the
# new wiring: the checker fires only when (a) the target repo ships it AND
# (b) the diff actually touches a hooks/test/*.sh or bin/test_*.py file — and
# stays silent otherwise, so an unrelated push pays no added cost.

TW_BARE="$TMPROOT/tripwire-bare.git"
TW_REPO="$TMPROOT/tripwire-repo"
rm -rf "$TW_BARE" "$TW_REPO"
git init -q --bare "$TW_BARE"
git clone -q "$TW_BARE" "$TW_REPO" 2>/dev/null
(
  cd "$TW_REPO" || exit 1
  git config user.email "test@test"
  git config user.name "Test"
  git checkout -q -B main
  mkdir -p bin hooks/test
  # Ship the REAL checker so the "target repo has the checker" gate opens.
  cp "$REPO_ROOT/bin/sable-fixture-tripwire" bin/sable-fixture-tripwire
  chmod +x bin/sable-fixture-tripwire
  echo "root" > README.md
  git add -A
  git commit -q -m "init"
  git push -q origin main
)
TW_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TEST_PHASE=skip"

# Positive (unit-spec case 1): a commit ahead of origin adds a
# hooks/test/*.sh fixture with an unguarded `cd` — the exact SABLE-0ssz.2
# escape shape — and the static phase DENIES with the checker's own
# file:line, before any ci-verify cycle is spent.
(
  cd "$TW_REPO" || exit 1
  # Built via a variable, not a literal heredoc line: this suite's own file
  # lives under hooks/test/*.sh, so a literal unguarded `cd "$FIX"` line
  # here would trip THIS repo's fixture-tripwire on itself. UNGUARDED_CD's
  # single-quoted value masks to non-"cd" text under the checker's
  # quote-masking, so only the file written into TW_REPO carries the real
  # (deliberately un-excused) violation.
  UNGUARDED_CD='cd "$FIX"'
  cat > hooks/test/test-planted-tripwire.sh <<EOF
#!/usr/bin/env bash
FIX=\$(mktemp -d)
$UNGUARDED_CD
git config user.name "Evil"
EOF
  git add -A
  git commit -q -m "add fixture with unguarded cd"
)
assert_deny "SABLE-digiy: touched hooks/test/*.sh with unguarded cd → static phase DENIES via fixture-tripwire" \
  "$TW_ENV" "git push" "$TW_REPO" "cd-unguarded"

OUT=$(run_hook "$TW_ENV" "git push" "$TW_REPO")
if echo "$OUT" | grep -qF "test-planted-tripwire.sh:3"; then
  v3pass "SABLE-digiy: deny reason names the exact file:line"
else
  v3fail "SABLE-digiy: deny reason names the exact file:line" "got: ${OUT:0:500}"
fi

(
  cd "$TW_REPO" || exit 1
  git reset -q --hard origin/main
)

# Negative (unit-spec case 2): a commit ahead of origin that touches ONLY a
# non-fixture file → the checker is NOT invoked at all (not merely passing).
# This is the acceptance-criteria budget requirement, not a nicety: a push
# touching no test files must incur no new tripwire cost.
(
  cd "$TW_REPO" || exit 1
  echo "more" >> README.md
  git add -A
  git commit -q -m "unrelated change"
)
OUT=$(run_hook "$TW_ENV" "git push" "$TW_REPO")
if echo "$OUT" | grep -q '"additionalContext"' && echo "$OUT" | grep -qF "phase skipped" \
   && ! echo "$OUT" | grep -qF "fixture-tripwire"; then
  v3pass "SABLE-digiy: diff touching only non-test files → tripwire NOT invoked (no added cost)"
else
  v3fail "SABLE-digiy: diff touching only non-test files → tripwire NOT invoked (no added cost)" "got: ${OUT:0:400}"
fi

(
  cd "$TW_REPO" || exit 1
  git reset -q --hard origin/main
)
rm -rf "$TW_BARE" "$TW_REPO"

# Integration control (real git repo + real hook, positive control per the
# bead's INTEGRATION spec): the SAME violation shape, but guarded per the
# z776 pattern, PASSES — proving the wiring is not a blanket false-positive
# gate on every touched fixture file.
TWC_BARE="$TMPROOT/tripwire-clean-bare.git"
TWC_REPO="$TMPROOT/tripwire-clean-repo"
rm -rf "$TWC_BARE" "$TWC_REPO"
git init -q --bare "$TWC_BARE"
git clone -q "$TWC_BARE" "$TWC_REPO" 2>/dev/null
(
  cd "$TWC_REPO" || exit 1
  git config user.email "test@test"
  git config user.name "Test"
  git checkout -q -B main
  mkdir -p bin hooks/test
  cp "$REPO_ROOT/bin/sable-fixture-tripwire" bin/sable-fixture-tripwire
  chmod +x bin/sable-fixture-tripwire
  echo "root" > README.md
  git add -A
  git commit -q -m "init"
  git push -q origin main
  cat > hooks/test/test-clean-tripwire.sh <<'EOF'
#!/usr/bin/env bash
FIX=$(mktemp -d)
cd "$FIX" || exit 1
git -C "$FIX" config user.name "Test"
EOF
  git add -A
  git commit -q -m "add fixture with guarded cd"
)
assert_context "SABLE-digiy: touched hooks/test/*.sh with GUARDED cd → static phase passes (not a false-positive gate)" \
  "$TW_ENV" "git push" "$TWC_REPO" "phase skipped"
rm -rf "$TWC_BARE" "$TWC_REPO"

# Repo that does NOT ship bin/sable-fixture-tripwire at all (the common case
# for every OTHER repo this generic hook gates) → the checker gate never
# opens, even for a touched hooks/test/*.sh file carrying the exact violation
# shape. Reuses REPO_DIR from the typecheck fixtures above (already
# git-init'd, no bin/ dir at all).
(
  cd "$REPO_DIR" || exit 1
  mkdir -p hooks/test
  # See the UNGUARDED_CD comment above — same self-flagging avoidance.
  UNGUARDED_CD='cd "$FIX"'
  cat > hooks/test/test-no-checker.sh <<EOF
#!/usr/bin/env bash
FIX=\$(mktemp -d)
$UNGUARDED_CD
EOF
  git add -A
  git commit -q -m "touch a fixture-shaped file in a repo with no checker"
)
NOCHECKER_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_context "SABLE-digiy: repo without bin/sable-fixture-tripwire → checker gate never opens" \
  "$NOCHECKER_ENV" "git push" "$REPO_DIR" "phase skipped"
(
  cd "$REPO_DIR" || exit 1
  git reset -q --hard origin/main
)

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
