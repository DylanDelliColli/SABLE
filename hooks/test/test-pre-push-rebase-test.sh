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

DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK="$DIR/../multi-manager/pre-push-rebase-test.sh"
# shellcheck source=lib-pre-push-fixture-root.sh
. "$DIR/lib-pre-push-fixture-root.sh"
# shellcheck source=lib-json-input-encoder.sh
. "$DIR/lib-json-input-encoder.sh"
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
# concurrent and nested runs collision-free. The allocation seam is raced
# directly by test-pre-push-rebase-concurrency.sh.
TMPROOT="$(sable_pre_push_fixture_root)"
cleanup_pre_push_fixture() {
  sable_json_encoder_stop >/dev/null 2>&1 || true
  rm -rf "$TMPROOT"
}
trap cleanup_pre_push_fixture EXIT

PASS=0
FAIL=0
FAIL_NAMES=""
ASSERT_DENY_CALLS=0
ASSERT_CONTEXT_OUTPUT_CALLS=0

# Both high-volume hook suites share this encoder so their JSON schemas and
# escaping cannot drift.  The suite is serial, so one request/response pair
# needs no request id.
sable_json_encoder_start "$TMPROOT"
SYNTHETIC_INPUT_ENCODER_PID="$SABLE_JSON_ENCODER_PID"
SYNTHETIC_INPUT_ENCODER_TRACE="$SABLE_JSON_ENCODER_TRACE"

encode_synthetic_input() {
  # $1 = command, $2 = cwd, $3 = agent_id, $4 = agent_type, $5 = typed flag
  sable_json_encoder_encode pre "$1" "$2" "" "" "$3" "$4" "$5"
}

make_input() {
  # $1 = command, $2 = cwd, $3 (optional) = agent_id
  encode_synthetic_input "$1" "$2" "${3:-}" "" 0
}

# Load-bearing escaping plant: this request crosses every boundary that makes a
# pure-shell JSON encoder risky. The exact expected json.dumps result is checked
# with the one-process/request counts at suite end.
SYNTHETIC_INPUT_ESCAPE_ACTUAL=$(make_input \
  $'git push "snowman ☃"\nnext\\tail\tend' \
  $'/tmp/space dir/é' \
  $'agent"id\\tail\n')
SYNTHETIC_INPUT_ESCAPE_EXPECTED='{"tool_input": {"command": "git push \"snowman \u2603\"\nnext\\tail\tend"}, "cwd": "/tmp/space dir/\u00e9", "agent_id": "agent\"id\\tail\n"}'

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
  ASSERT_DENY_CALLS=$((ASSERT_DENY_CALLS + 1))
  out=$(run_hook "$env" "$cmd" "$cwd" "$aid")
  if [[ "$out" == *'"permissionDecision": "deny"'* && "$out" == *"$expect"* ]]; then
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
  assert_context_output "$name" "$out" "$expect"
}

assert_context_output() {
  # <name> <already-captured-hook-output> <expected text>. Several assertions
  # intentionally inspect different claims in one immutable exact-object
  # verdict; reuse that observation instead of rebuilding/revalidating the
  # same detached worktree for each property (SABLE-y4nom.7.6).
  local name="$1" out="$2" expect="$3"
  ASSERT_CONTEXT_OUTPUT_CALLS=$((ASSERT_CONTEXT_OUTPUT_CALLS + 1))
  if [[ "$out" == *'"additionalContext"'* && "$out" == *"$expect"* ]]; then
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
INITIAL_PARSE_TOOL_TRACE="$TMPROOT/initial-parse-tools.trace"
INITIAL_PARSE_SPY_DIR="$TMPROOT/initial-parse-spies"
mkdir -p "$INITIAL_PARSE_SPY_DIR"
: > "$INITIAL_PARSE_TOOL_TRACE"
REAL_CAT="$(command -v cat)"
REAL_SED="$(command -v sed)"
cat > "$INITIAL_PARSE_SPY_DIR/cat" <<EOF
#!/usr/bin/env bash
printf '%s\n' cat >> "$INITIAL_PARSE_TOOL_TRACE"
exec "$REAL_CAT" "\$@"
EOF
cat > "$INITIAL_PARSE_SPY_DIR/sed" <<EOF
#!/usr/bin/env bash
printf '%s\n' sed >> "$INITIAL_PARSE_TOOL_TRACE"
exec "$REAL_SED" "\$@"
EOF
chmod +x "$INITIAL_PARSE_SPY_DIR/cat" "$INITIAL_PARSE_SPY_DIR/sed"
ORIGINAL_TEST_PATH="$PATH"
PATH="$INITIAL_PARSE_SPY_DIR:$PATH"
assert_allow "ignores non-git-push" "$MGR_ENV" "git status" "/tmp"
PATH="$ORIGINAL_TEST_PATH"
INITIAL_PARSE_TOOL_CALLS=0
while IFS= read -r tool_call; do
  [ -n "$tool_call" ] && INITIAL_PARSE_TOOL_CALLS=$((INITIAL_PARSE_TOOL_CALLS + 1))
done < "$INITIAL_PARSE_TOOL_TRACE"
if [ "$INITIAL_PARSE_TOOL_CALLS" -eq 0 ]; then
  PASS=$((PASS+1))
  echo "PASS: initial hook-input read and field extraction use builtins plus one JSON boundary (no cat/sed processes)"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  initial hook-input parsing spawned $INITIAL_PARSE_TOOL_CALLS cat/sed process(es): $(<"$INITIAL_PARSE_TOOL_TRACE")"
  echo "FAIL: initial hook-input read and field extraction use builtins plus one JSON boundary (no cat/sed processes)"
fi

# Test 2: ignores when no manager identity
assert_allow "no manager identity → no-op" "" "git push" "/tmp"

# Test 3: skips force pushes
assert_allow "skips --force" "$MGR_ENV" "git push --force" "/tmp"
assert_allow "skips -f shorthand" "$MGR_ENV" "git push -f" "/tmp"

# Test 4 (v3 SABLE-404): a bare agent_id (no agent_type) is an unnamed worker —
# its push is DENIED, not skipped (inverts the v2 stand-down). env identity is
# ignored when agent_id is present, so MGR_ENV does not make it a manager.
assert_deny "subagent without agent_type → worker push DENIED" "$MGR_ENV" "git push" "/tmp" "worker subagents do not push" "subagent-123"

# Test 5: non-repo CWD. REVISED under SABLE-j90ba B5: a known-push command
# whose work tree cannot be resolved DENIES (fail closed) instead of exiting
# silently — the silent exit was the same bypass shape git's parent-dir
# search exploited from repo subdirs.
assert_deny "non-repo CWD fails closed instead of silently allowing a known push" \
  "$MGR_ENV" "git push" "/tmp/nonexistent-non-repo" "work tree"

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
# Pin the local branch name to 'main': the clone of an EMPTY bare adopts the
# host's init.defaultBranch, and the exact-object destination rule
# (SABLE-j90ba) compares refspec destinations against the CURRENT branch
# name — 'git push origin main' cases must not depend on host git config.
git checkout -q -B main
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
# RESOLVED command (proves detect_test_cmd read .sable, not env/manifest).
# SABLE-j90ba: .sable is COMMITTED — the gate resolves configuration from the
# clean worktree at the pushed object, so an uncommitted .sable edit is
# deliberately invisible to it (a dirty edit could otherwise weaken the gate
# with no diff trace).
sable_commit_dotsable() {
  # $1 = content
  printf '%s\n' "$1" > "$REPO_DIR/.sable"
  git -C "$REPO_DIR" add .sable
  git -C "$REPO_DIR" commit -q -m "fixture: .sable testCommand"
}
sable_commit_dotsable "testCommand=exit 42"
assert_deny "«.sable» testCommand resolved and enforced → failing command denies phase 3" \
  "$SABLE_TESTCMD_ENV" "git push" "$REPO_DIR" "exit 42"

# Test 11c: .sable testCommand that passes → phase 3 runs clean, no deny and
# no "no test command detected" fallback (proves manifest auto-detect was
# bypassed in favor of the resolved .sable value).
#
# A passing push is no longer SILENT (SABLE-y4nom.4): it now carries a record
# of the command actually executed. SABLE-b99hy is why — a worker narrowed
# sable.testCommand to a two-file subset, pushed green, and restored it, so
# the gate certified a narrower claim than it was configured to enforce while
# printing "enforced". An allow that names what it ran is a strictly stronger
# assertion than an allow that says nothing.
sable_commit_dotsable "testCommand=true"
assert_context "«.sable» testCommand resolved and enforced → passing push RECORDS the executed command" \
  "$SABLE_TESTCMD_ENV" "git push" "$REPO_DIR" "enforced test command (executed): \`true\`"

# Test 11d: repo-local git config wins over the .sable file (precedence
# mirrors sable_resolve_integration_branch's config > .sable ordering)
git -C "$REPO_DIR" config sable.testCommand "exit 43"
assert_deny "repo-local git config testCommand wins over .sable file" \
  "$SABLE_TESTCMD_ENV" "git push" "$REPO_DIR" "exit 43"
git -C "$REPO_DIR" config --unset sable.testCommand

git -C "$REPO_DIR" rm -q -f .sable
git -C "$REPO_DIR" commit -q -m "fixture: drop .sable"

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
sable_commit_dotsable "testCommand=sleep 2 && exit 0"

# Test 11e: with only the 1s env default in effect, a test command that takes
# 2s is killed by `timeout` → phase 3 denies citing the exceeded timeout.
assert_deny "no per-repo override → 1s env default kills a 2s test command" \
  "$SABLE_TIMEOUT_ENV" "git push" "$REPO_DIR" "exceeded the"

# Test 11f: repo-local git config sable.testTimeout=5 overrides the 1s env
# default → the same 2s test command now completes inside the window and the
# push is allowed (proves the config override reaches the hook's `timeout`
# call, not just the resolver function in isolation).
git -C "$REPO_DIR" config sable.testTimeout 5
assert_context "repo-local git config testTimeout overrides 1s env default → 2s test command completes" \
  "$SABLE_TIMEOUT_ENV" "git push" "$REPO_DIR" "enforced test command (executed): \`sleep 2 && exit 0\`"
git -C "$REPO_DIR" config --unset sable.testTimeout

# SABLE-y4nom.4 recorded-command leg: when a declared prior intent disagrees
# with what actually ran, the gate reports the DIVERGENCE and certifies the
# EXECUTED command — never the intent. SABLE-b99hy measured say-versus-do
# divergence in both directions one drain apart, so a report of what was
# enforced is not evidence of what was enforced.
sable_commit_dotsable "testCommand=true"
assert_context "recorded-command: executed command is certified over a disagreeing prior intent" \
  "$SABLE_TESTCMD_ENV SABLE_TEST_COMMAND_INTENT=pytest_bin_full" "git push" "$REPO_DIR" \
  "DIVERGENCE: prior intent was \`pytest_bin_full\`"
assert_context "recorded-command: the record names the command that actually ran" \
  "$SABLE_TESTCMD_ENV SABLE_TEST_COMMAND_INTENT=pytest_bin_full" "git push" "$REPO_DIR" \
  "enforced test command (executed): \`true\`"

# Negative control: intent that AGREES must not manufacture a divergence.
assert_context "recorded-command: agreeing intent reports no divergence" \
  "$SABLE_TESTCMD_ENV SABLE_TEST_COMMAND_INTENT=true" "git push" "$REPO_DIR" \
  "enforced test command (executed): \`true\`"
DIVERGENCE_OUT=$(run_hook "$SABLE_TESTCMD_ENV SABLE_TEST_COMMAND_INTENT=true" "git push" "$REPO_DIR")
if echo "$DIVERGENCE_OUT" | grep -qF "DIVERGENCE"; then
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  recorded-command: agreeing intent must not report a divergence"
  echo "FAIL: recorded-command: agreeing intent must not report a divergence"
else
  PASS=$((PASS+1))
  echo "PASS: recorded-command: agreeing intent must not report a divergence"
fi

git -C "$REPO_DIR" rm -q -f .sable
git -C "$REPO_DIR" commit -q -m "fixture: drop .sable"

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

# Test 13: 'git -c a=b push origin main' reaches the gate. REVISED under
# SABLE-j90ba: it still must reach the gate (the matcher claim), but the
# exact-object leg now fail-closes on '-c' — a push-time config injection
# (e.g. -c remote.origin.push=...) can expand the push after the gate's
# repo-config soundness checks approved a single update. A deny proves reach
# just as a context did.
assert_deny "matcher: 'git -c a=b push origin main' reaches gate and is fail-closed on the -c global flag" \
  "$MATCHER_ENV" "git -c http.extraheader=Authorization:bearer push origin main" "$REPO_DIR" "global flag -c"

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

# Test 18: 'SABLE_SKIP_PRE_PUSH=1 git push' reaches gate (env-assignment prefix, SABLE-531).
# REVISED under SABLE-j90ba B3: the matcher must still SEE the push (that is
# the SABLE-531 claim, and a deny proves reach exactly as a context did), but
# the exact-object leg now fail-closes on EVERY leading assignment — the hook
# validates git state without command-local env while the real push runs with
# it (GIT_DIR redirects the repo; GIT_CONFIG_* injects push config). The
# hook's own SABLE_SKIP_PRE_PUSH is read from the SESSION env, where a
# command-prefix assignment never arrives anyway.
assert_deny "matcher: 'SABLE_SKIP_PRE_PUSH=1 git push' reaches gate and is fail-closed as a leading env assignment" \
  "$MATCHER_ENV" "SABLE_SKIP_PRE_PUSH=1 git push" "$REPO_DIR" "leading environment assignment"

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
  encode_synthetic_input "$1" "$2" "$3" "$4" 1
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

# ===================================================================
# SABLE-j90ba: exact-object push gate. The gate must validate the OBJECT
# being pushed, not the checked-out working tree. UNSCOPED: applies to every
# manager push traversing this hook (review ruling 2026-08-01 — explicit-
# integration scoping belongs only to the provenance guard; scoping this leg
# would preserve the vacuous-green defect in ordinary repos).
#
#   RED (pre-fix): R1 — a committed-failing test suite with a dirty
#     working-tree edit that makes it pass produces a GREEN attributed to an
#     object whose tests fail. R2 — pushing a ref that is not HEAD runs the
#     phases against HEAD's tree and greens a never-tested object.
#   GREEN (post-fix): R1 denies in phase 4 from a clean detached worktree at
#     the final object; R2 denies at the single-update source rule.
# ===================================================================

# ---------- UNIT: sable_parse_push_update ----------
assert_parse() {
  local name="$1" cmd="$2" expect="$3"
  local out
  out=$(call_hook_fn sable_parse_push_update "$cmd" 2>/dev/null)
  if [ "$out" = "$expect" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: ${out:0:200})"
    echo "FAIL: $name"
    echo "  Expected: $expect"
    echo "  Got:      ${out:0:200}"
  fi
}

assert_parse "parse: bare 'git push' is a single update of HEAD to the current branch" \
  "git push" "update||||"
assert_parse "parse: 'git push origin br' is an update br->br carrying the remote" \
  "git push origin br" "update|origin|br|br|"
assert_parse "parse: '-u origin br' is an update br->br (flag ignored)" \
  "git push -u origin br" "update|origin|br|br|"
assert_parse "parse: 'origin HEAD:refs/heads/br' is an update HEAD->br (dst normalized)" \
  "git push origin HEAD:refs/heads/br" "update|origin|HEAD|br|"
assert_parse "parse: a non-origin remote token is carried, not discarded" \
  "git push backup main" "update|backup|main|main|"
assert_parse "parse: '--delete origin br' is deletion-only" \
  "git push --delete origin br" "delete|origin||br|"
assert_parse "parse: 'origin :refs/heads/br' (empty source) is deletion-only" \
  "git push origin :refs/heads/br" "delete|origin||br|"
assert_parse "parse: bare ':' is git's MATCHING push, never deletion (B1)" \
  "git push origin :" "unsupported||||colon refspec outside the pinned deletion form"
assert_parse "parse: ':br' bare-name empty-source form is outside the pinned deletion surface" \
  "git push origin :br" "unsupported||||colon refspec outside the pinned deletion form"
assert_parse "parse: leading GIT_DIR assignment redirects the real push and is rejected (B3)" \
  "GIT_DIR=/other/.git git push origin br" "unsupported||||leading environment assignment GIT_DIR"
assert_parse "parse: leading GIT_CONFIG_* assignments inject push config and are rejected" \
  "GIT_CONFIG_COUNT=1 git push origin br" "unsupported||||leading environment assignment GIT_CONFIG_COUNT"
assert_parse "parse: brace expansion in a refspec is rejected (B4)" \
  "git push origin {main,side}" "unsupported||||shell expansion syntax"
assert_parse "parse: dollar refspec is rejected (pre-expansion text diverges from execution)" \
  'git push origin $BR' "unsupported||||shell expansion syntax"
assert_parse "parse: colon-dollar deletion form is rejected" \
  'git push origin :$BR' "unsupported||||shell expansion syntax"
assert_parse "parse: tilde -C path is rejected (resolver would treat it literally and quiet-exit)" \
  "git -C ~/somerepo push" "unsupported||||shell expansion syntax"
assert_parse "parse: '--config-env' reaches the parser (matcher fixed) and is rejected as a global flag" \
  "git --config-env=push.followTags=E push" "unsupported||||global flag --config-env"
assert_parse "parse: '--all' is unsupported" \
  "git push --all origin" "unsupported||||flag --all"
assert_parse "parse: '--mirror' is unsupported" \
  "git push --mirror origin" "unsupported||||flag --mirror"
assert_parse "parse: '--tags' is unsupported" \
  "git push --tags origin" "unsupported||||flag --tags"
assert_parse "parse: two refspecs are unsupported (multiple updates)" \
  "git push origin br1 br2" "unsupported||||multiple refspecs"
assert_parse "parse: mixed delete+update refspecs are unsupported" \
  "git push origin :old new" "unsupported||||mixed delete and update"
assert_parse "parse: forced '+src:dst' refspec is unsupported" \
  "git push origin +br:br" "unsupported||||forced refspec"
assert_parse "parse: unknown flag '--dry-run' is unsupported (fail closed)" \
  "git push --dry-run origin br" "unsupported||||flag --dry-run"
assert_parse "parse: 'git -C /x push origin br' accepts the resolved -C global flag" \
  "git -C /x/y push origin br" "update|origin|br|br|"
assert_parse "parse: '--git-dir' redirects the repo and is rejected (fail closed)" \
  "git --git-dir=/other/repo push origin br" "unsupported||||global flag --git-dir"
assert_parse "parse: '--work-tree' redirects the tree and is rejected" \
  "git --work-tree=/x push origin br" "unsupported||||global flag --work-tree"
assert_parse "parse: '-c' can inject push config at push time and is rejected" \
  "git -c remote.origin.push=refs/heads/a:refs/heads/b push" "unsupported||||global flag -c"
assert_parse "parse: '--bare' is rejected" \
  "git --bare push origin br" "unsupported||||global flag --bare"
assert_parse "parse: a different local source still parses (denial happens at resolution)" \
  "git push origin other:target" "update|origin|other|target|"
assert_parse "parse: compound command with two push invocations is unsupported" \
  "git push origin br && git push origin other" "unsupported||||compound command"
assert_parse "parse: compound command via semicolon is unsupported" \
  "git push origin br; echo done" "unsupported||||compound command"
assert_parse "parse: compound command via pipe is unsupported" \
  "git push origin br | cat" "unsupported||||compound command"

# ---------- INTEGRATION: the j90ba fixture ----------
# The committed runner is STABLE across branches: it checks a committed
# PRODUCTION file (app.txt must contain 'good') and rejects the untracked
# overlay file — one committed suite discriminates clean-object execution
# from working-tree execution in both directions, with the dirtied file
# being production code, never the test runner itself.
J90_BARE="$TMPROOT/j90ba-bare.git"
J90_REPO="$TMPROOT/j90ba-repo"
rm -rf "$J90_BARE" "$J90_REPO"
git init -q --bare "$J90_BARE"
git clone -q "$J90_BARE" "$J90_REPO" 2>/dev/null
(
  cd "$J90_REPO" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  printf 'test "$(cat app.txt)" = good && test ! -f scratch-note.txt\n' > run-tests.sh
  printf 'good\n' > app.txt
  printf 'testCommand=bash ./run-tests.sh\n' > .sable
  git add -A; git commit -q -m base
  git push -q "$J90_BARE" HEAD:refs/heads/main
  git checkout -q -b wk-dirty
  printf 'bad\n' > app.txt
  git add app.txt; git commit -q -m 'committed failing production file'
  git config sable.integrationBranch main
  # Dirty overlay on the PRODUCTION file: the WORKING TREE passes while the
  # pushed OBJECT fails.
  printf 'good\n' > app.txt
)
J90_ENV="$MGR_ENV SABLE_PRE_PUSH_TYPECHECK_COMMAND=true"

# R1 (RED pre-fix): a dirty tracked PRODUCTION file must not green an object
# whose committed state fails — the phases must run the OBJECT, not the
# working tree.
assert_deny "j90ba R1: dirty tracked production file cannot green a committed-failing object (clean-worktree phase 4)" \
  "$J90_ENV" "git push origin wk-dirty" "$J90_REPO" "phase 4"

# Failure-path admin cleanup: the deny above must leave no gate worktree
# behind (asserted on worktree ADMIN state, not file presence).
J90_WT_AFTER_DENY=$(git -C "$J90_REPO" worktree list 2>/dev/null | wc -l | tr -d ' ')
if [ "$J90_WT_AFTER_DENY" = "1" ]; then
  PASS=$((PASS+1))
  echo "PASS: j90ba cleanup: deny path leaves no gate worktree admin entry"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  j90ba cleanup after deny: expected 1 worktree, got $J90_WT_AFTER_DENY"
  echo "FAIL: j90ba cleanup after deny: expected 1 worktree, got $J90_WT_AFTER_DENY"
  git -C "$J90_REPO" worktree list
fi

# R2 (RED pre-fix): push-by-ref from a shared checkout — HEAD on main, an
# unrelated untracked file present (the exact shape of the 2026-07-30
# incident) — must be denied by the single-update source rule, never
# validated-by-proxy against HEAD's tree. checkout -f: R1's dirty overlay
# must not block the branch switch (it silently did on the first run).
( cd "$J90_REPO" && git checkout -q -f main )
echo scratch > "$J90_REPO/scratch-note.txt"
assert_deny "j90ba R2: shared-checkout push-by-ref of a non-HEAD object is denied (source rule), not greened against HEAD's tree" \
  "$J90_ENV" "git push origin wk-dirty:refs/heads/wk-dirty" "$J90_REPO" "not the current HEAD"

# Untracked overlay (RED pre-fix): with the unrelated untracked file still
# present, an accepted-form push of main must GREEN — the committed suite
# passes in the clean worktree; legacy working-tree execution fails it.
assert_context "j90ba untracked overlay: unrelated untracked file cannot alter the object's verdict" \
  "$J90_ENV" "git push" "$J90_REPO" "all phases passed"
rm -f "$J90_REPO/scratch-note.txt"

# A REAL shared-checkout push-by-ref, exercised THROUGH the guard decision
# (acceptance criterion 7). Instrument positive control first: the literal
# push-by-ref genuinely publishes the non-HEAD object to the bare remote.
# Then a consumer executes the SAME literal push only when the hook did not
# deny — pre-fix the hook greens, the bad push really lands, and the case is
# red; post-fix the gate denies and the destination stays absent.
J90_WKDIRTY_SHA=$(git -C "$J90_REPO" rev-parse wk-dirty)
git -C "$J90_REPO" push -q "$J90_BARE" wk-dirty:refs/heads/probe-instrument 2>/dev/null
J90_PROBE_SHA=$(git -C "$J90_BARE" rev-parse --verify --quiet probe-instrument 2>/dev/null || echo missing)
git -C "$J90_REPO" push -q "$J90_BARE" :refs/heads/probe-instrument 2>/dev/null
if [ "$J90_PROBE_SHA" = "$J90_WKDIRTY_SHA" ]; then
  PASS=$((PASS+1))
  echo "PASS: j90ba push-by-ref instrument: the literal push publishes the non-HEAD object (positive control)"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  j90ba push-by-ref instrument (got $J90_PROBE_SHA want $J90_WKDIRTY_SHA)"
  echo "FAIL: j90ba push-by-ref instrument: expected $J90_WKDIRTY_SHA got $J90_PROBE_SHA"
fi
J90_R2_CMD="git -C $J90_REPO push origin wk-dirty:refs/heads/probe-guarded"
J90_R2_OUT=$(run_hook "$J90_ENV" "$J90_R2_CMD" "$J90_REPO")
if ! echo "$J90_R2_OUT" | grep -q '"permissionDecision": "deny"'; then
  # Consumer honors the guard decision: no deny -> the push executes for real.
  git -C "$J90_REPO" push -q origin wk-dirty:refs/heads/probe-guarded 2>/dev/null
fi
J90_GUARDED_REF=$(git -C "$J90_BARE" rev-parse --verify --quiet probe-guarded 2>/dev/null || echo absent)
git -C "$J90_REPO" push -q "$J90_BARE" :refs/heads/probe-guarded 2>/dev/null || true
if echo "$J90_R2_OUT" | grep -q '"permissionDecision": "deny"' && [ "$J90_GUARDED_REF" = "absent" ]; then
  PASS=$((PASS+1))
  echo "PASS: j90ba real push-by-ref through the guard: denied, and the destination ref never appeared on the remote"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  j90ba real push-by-ref through the guard (ref=$J90_GUARDED_REF out: ${J90_R2_OUT:0:150})"
  echo "FAIL: j90ba real push-by-ref through the guard: ref=$J90_GUARDED_REF"
  echo "  Got: ${J90_R2_OUT:0:300}"
fi

# Accepted forms (positive controls): each pinned form passes on a clean
# checkout of main whose committed suite passes.
J90_BARE_PUSH_OUT=$(run_hook "$J90_ENV" "git push" "$J90_REPO")
assert_context_output "j90ba accepted form: bare 'git push'" \
  "$J90_BARE_PUSH_OUT" "all phases passed"
assert_context "j90ba accepted form: 'git push origin main'" \
  "$J90_ENV" "git push origin main" "$J90_REPO" "all phases passed"
assert_context "j90ba accepted form: 'git push -u origin main'" \
  "$J90_ENV" "git push -u origin main" "$J90_REPO" "all phases passed"
assert_context "j90ba accepted form: 'git push origin HEAD:refs/heads/main'" \
  "$J90_ENV" "git push origin HEAD:refs/heads/main" "$J90_REPO" "all phases passed"

# Named verdict: the green names source ref, destination, final object, and
# base object — LITERAL values, not presence-only.
J90_MAIN_SHA=$(git -C "$J90_REPO" rev-parse HEAD)
J90_BASE_SHA=$(git -C "$J90_REPO" rev-parse origin/main)
assert_context_output "j90ba named verdict: green names the final object" \
  "$J90_BARE_PUSH_OUT" "final object $J90_MAIN_SHA"
assert_context_output "j90ba named verdict: green names the exact source ref" \
  "$J90_BARE_PUSH_OUT" "source ref 'HEAD'"
assert_context_output "j90ba named verdict: green names the destination" \
  "$J90_BARE_PUSH_OUT" "destination 'main'"
assert_context_output "j90ba named verdict: green names the exact base object and base ref" \
  "$J90_BARE_PUSH_OUT" "base object $J90_BASE_SHA (origin/main)"

# Rebase-SHA-change: origin/main advances beneath a worker branch; phase 1
# rebases it, and the verdict must name the POST-rebase final object — never
# the pre-push SHA (which no longer exists on the branch).
(
  cd "$J90_REPO" || exit 1
  git checkout -q -f -b wk-behind
  echo w > w-behind.txt; git add w-behind.txt; git commit -q -m w-behind
  git checkout -q -f main
  echo adv > advance.txt; git add advance.txt; git commit -q -m advance
  git push -q "$J90_BARE" HEAD:refs/heads/main
  git checkout -q -f wk-behind
)
J90_PRE_REBASE_SHA=$(git -C "$J90_REPO" rev-parse wk-behind)
J90_REBASE_OUT=$(run_hook "$J90_ENV" "git push origin wk-behind" "$J90_REPO")
J90_POST_REBASE_SHA=$(git -C "$J90_REPO" rev-parse wk-behind)
if [ "$J90_POST_REBASE_SHA" != "$J90_PRE_REBASE_SHA" ] \
   && echo "$J90_REBASE_OUT" | grep -qF "final object $J90_POST_REBASE_SHA" \
   && echo "$J90_REBASE_OUT" | grep -qF "all phases passed"; then
  PASS=$((PASS+1))
  echo "PASS: j90ba rebase-SHA-change: verdict names the post-rebase final object"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  j90ba rebase-SHA-change (pre=$J90_PRE_REBASE_SHA post=$J90_POST_REBASE_SHA got: ${J90_REBASE_OUT:0:200})"
  echo "FAIL: j90ba rebase-SHA-change: verdict must name the post-rebase final object"
  echo "  pre=$J90_PRE_REBASE_SHA post=$J90_POST_REBASE_SHA"
  echo "  Got: ${J90_REBASE_OUT:0:300}"
fi
( cd "$J90_REPO" && git checkout -q -f main )

# Post-rebase source divergence: a LITERAL SHA source frozen at the
# pre-rebase object would make git push the OLD object while the gate
# validated the rebased one — deny (the 'source SHA changed by rebase'
# negative control, in its real form).
(
  cd "$J90_REPO" || exit 1
  git checkout -q -f -b wk-pinned
  echo p > p-pinned.txt; git add p-pinned.txt; git commit -q -m p-pinned
  git checkout -q -f main
  echo adv2 > advance2.txt; git add advance2.txt; git commit -q -m advance2
  git push -q "$J90_BARE" HEAD:refs/heads/main
  git checkout -q -f wk-pinned
)
J90_PINNED_SHA=$(git -C "$J90_REPO" rev-parse wk-pinned)
assert_deny "j90ba post-rebase source divergence: literal-SHA source frozen at the pre-rebase object is denied" \
  "$J90_ENV" "git push origin $J90_PINNED_SHA:refs/heads/wk-pinned" "$J90_REPO" "no longer resolves"
( cd "$J90_REPO" && git checkout -q -f main )

# Bare-push destination under push.default=upstream resolves the UPSTREAM
# branch name (which may differ from the current branch); under simple with
# a differing upstream, git itself refuses, so the gate fails closed.
(
  cd "$J90_REPO" || exit 1
  git checkout -q -f -b wk-up main
  git push -q "$J90_BARE" wk-up:refs/heads/upstream-dest
  git fetch -q origin
  git branch -q --set-upstream-to=origin/upstream-dest wk-up
)
git -C "$J90_REPO" config push.default upstream
assert_context "j90ba bare push under push.default=upstream certifies the upstream destination" \
  "$J90_ENV" "git push" "$J90_REPO" "destination 'upstream-dest'"
git -C "$J90_REPO" config push.default simple
assert_deny "j90ba bare push under push.default=simple with a differing upstream name is denied" \
  "$J90_ENV" "git push" "$J90_REPO" "differing"
git -C "$J90_REPO" config --unset push.default
( cd "$J90_REPO" && git checkout -q -f main && git branch -q -D wk-up )

# Deletion carve-out: allowed through WITHOUT claiming object validation.
assert_context "j90ba deletion-only: '--delete' is allowed without claiming object validation" \
  "$J90_ENV" "git push --delete origin wk-dirty" "$J90_REPO" "object validation not claimed"
assert_context "j90ba deletion-only: empty-source ':refs/heads/x' form is allowed" \
  "$J90_ENV" "git push origin :refs/heads/wk-dirty" "$J90_REPO" "object validation not claimed"

# Fail-closed denials at the parse/destination layer.
assert_deny "j90ba deny: '--all' is unsupported in the initial surface" \
  "$J90_ENV" "git push --all origin" "$J90_REPO" "exactly one"
assert_deny "j90ba deny: '--mirror' is unsupported" \
  "$J90_ENV" "git push --mirror origin" "$J90_REPO" "exactly one"
assert_deny "j90ba deny: multiple refspecs are unsupported" \
  "$J90_ENV" "git push origin main wk-dirty" "$J90_REPO" "exactly one"
assert_deny "j90ba deny: mixed delete+update is unsupported" \
  "$J90_ENV" "git push origin :wk-dirty main" "$J90_REPO" "exactly one"
assert_deny "j90ba deny: destination that is not the current branch's own name/upstream" \
  "$J90_ENV" "git push origin HEAD:refs/heads/other-dest" "$J90_REPO" "destination"
assert_deny "j90ba deny: compound command with a second push is not certified" \
  "$J90_ENV" "git push origin main && git push origin wk-dirty" "$J90_REPO" "exactly one"
assert_deny "j90ba deny: a push AFTER a non-push git command still reaches the gate and fails closed" \
  "$J90_ENV" "git status && git push" "$J90_REPO" "exactly one"
assert_deny "j90ba deny: 'git pull --rebase && git push origin main' is not an ungated push" \
  "$J90_ENV" "git pull --rebase && git push origin main" "$J90_REPO" "exactly one"
# Parse-order regression (reproduced 2026-08-01): a compound whose FIRST
# 'git -C' targets a NON-repo used to resolve that dir, hit the non-repo
# early exit, and let the trailing push run ungated (empty output).
# Classification must fail closed BEFORE repo resolution.
assert_deny "j90ba deny: nonrepo-leading compound cannot exit quietly before classification" \
  "$J90_ENV" "git -C $TMPROOT status && git -C $J90_REPO push origin wk-dirty" "$TMPROOT" "exactly one"

# Wrong-remote negative: a push to a remote that is not the branch's
# effective push remote must not be certified against origin's refs.
git -C "$J90_REPO" remote add backup "$J90_BARE"
assert_deny "j90ba deny: push to a non-effective remote ('backup') is not certified" \
  "$J90_ENV" "git push backup main" "$J90_REPO" "remote"
git -C "$J90_REPO" remote remove backup

# Config-expansion soundness: a bare push under push.default=matching (or any
# remote.<name>.push refspec) is NOT provably a single update — deny.
git -C "$J90_REPO" config push.default matching
assert_deny "j90ba deny: bare push under push.default=matching is not provably single-update" \
  "$J90_ENV" "git push" "$J90_REPO" "push.default"
git -C "$J90_REPO" config --unset push.default
git -C "$J90_REPO" config remote.origin.push "refs/heads/*:refs/heads/*"
assert_deny "j90ba deny: bare push with a remote.origin.push refspec is not provably single-update" \
  "$J90_ENV" "git push" "$J90_REPO" "remote.origin.push"
git -C "$J90_REPO" config --unset remote.origin.push

# B1 e2e: bare ':' must never take the deletion carve-out (real git: it is a
# MATCHING push that can move remote branches).
assert_deny "j90ba deny: bare ':' refspec is matching-push, not deletion — fails closed" \
  "$J90_ENV" "git push origin :" "$J90_REPO" "exactly one"

# B3 e2e: a GIT_DIR-prefixed push redirects the real git while the hook
# validates CWD — denied at the parse layer.
assert_deny "j90ba deny: GIT_DIR-prefixed push is rejected (env redirects the real command)" \
  "$J90_ENV" "GIT_DIR=$TMPROOT git push origin main" "$J90_REPO" "exactly one"

# B2 e2e with real-git semantic proof: push.followTags expands an explicit
# single-branch push with annotated tags; remote.origin.mirror expands any
# push to every ref. Prove the expansion with --dry-run --porcelain on THIS
# git, then assert the gate denies each.
git -C "$J90_REPO" tag -a v1-semantic -m v1 2>/dev/null
git -C "$J90_REPO" config push.followTags true
J90_FT_LINES=$(git -C "$J90_REPO" push --dry-run --porcelain origin main 2>/dev/null | grep -c "refs/")
if [ "$J90_FT_LINES" -ge 2 ]; then
  PASS=$((PASS+1))
  echo "PASS: j90ba semantic proof: push.followTags expands 'origin main' to $J90_FT_LINES ref updates on this git"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  j90ba followTags semantic proof (got $J90_FT_LINES ref lines)"
  echo "FAIL: j90ba semantic proof: expected >=2 ref updates under push.followTags, got $J90_FT_LINES"
fi
assert_deny "j90ba deny: push.followTags=true disproves exactly-one on an explicit update push" \
  "$J90_ENV" "git push origin main" "$J90_REPO" "push.followTags"
git -C "$J90_REPO" config --unset push.followTags
git -C "$J90_REPO" config remote.origin.mirror true
J90_MIR_LINES=$(git -C "$J90_REPO" push --dry-run --porcelain origin 2>/dev/null | grep -c "refs/")
if [ "$J90_MIR_LINES" -ge 2 ]; then
  PASS=$((PASS+1))
  echo "PASS: j90ba semantic proof: remote.origin.mirror expands a push to $J90_MIR_LINES ref updates on this git"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  j90ba mirror semantic proof (got $J90_MIR_LINES ref lines)"
  echo "FAIL: j90ba semantic proof: expected >=2 ref updates under remote.origin.mirror, got $J90_MIR_LINES"
fi
assert_deny "j90ba deny: remote.origin.mirror=true disproves exactly-one for any push" \
  "$J90_ENV" "git push origin main" "$J90_REPO" "remote.origin.mirror"
git -C "$J90_REPO" config --unset remote.origin.mirror
git -C "$J90_REPO" tag -d v1-semantic >/dev/null 2>&1

# Regression (near-miss during implementation): a DIRTY .sable cannot weaken
# the gate — the OBJECT's committed testCommand governs.
(
  cd "$J90_REPO" || exit 1
  git checkout -q -f -b wk-weaken main
  printf 'testCommand=exit 47\n' > .sable
  git add .sable; git commit -q -m 'committed failing testCommand'
  printf 'testCommand=true\n' > .sable
)
assert_deny "j90ba dirty .sable cannot weaken the gate (committed exit 47 governs)" \
  "$J90_ENV" "git push origin wk-weaken" "$J90_REPO" "exit 47"
( cd "$J90_REPO" && git checkout -q -f main && git branch -q -D wk-weaken )

# B4 e2e counterexample: a branch LITERALLY named '{main,side}' is valid to
# git; pre-fix the parser certifies it as one update (src resolves, ==HEAD,
# dst==CURRENT) while Bash expands the same text into TWO refspecs at
# execution. The gate must deny on the expansion-capable text itself.
( cd "$J90_REPO" && git checkout -q -f -b "{main,side}" main )
assert_deny "j90ba B4: literally-brace-named branch cannot be certified (shell would expand to two refspecs)" \
  "$J90_ENV" "git push origin {main,side}" "$J90_REPO" "expansion"
( cd "$J90_REPO" && git checkout -q -f main && git branch -q -D "{main,side}" )

# B4 -C variant: 'git -C ~/repo push' must deny at parse, NOT quiet-exit via
# the resolver treating tilde literally as a non-repo (same bypass class as
# the parse-order blocker) while Bash expands it and pushes for real.
assert_deny "j90ba B4: tilde -C path denies at parse instead of quiet-exiting as non-repo" \
  "$J90_ENV" "git -C ~/nonexistent-gate-probe push" "$TMPROOT" "expansion"

# B3 addendum e2e: --config-env now reaches the gate (matcher fixed) and the
# parser fails it closed as an unproved global flag.
assert_deny "j90ba B3: '--config-env' push reaches the gate and is denied, not bypassed as non-push" \
  "$J90_ENV" "git --config-env=push.followTags=E push" "$J90_REPO" "global flag --config-env"

# B5: git searches parent dirs — a push from repo/subdir (ambient cwd or -C
# target) gates the REPO, never quiet-exits; unresolvable/bare targets DENY.
mkdir -p "$J90_REPO/subdir-b5"
assert_context "j90ba B5: ambient-subdir push resolves the toplevel and is fully gated" \
  "$J90_ENV" "git push" "$J90_REPO/subdir-b5" "all phases passed"
assert_context "j90ba B5: '-C repo/subdir push' resolves the toplevel and is fully gated" \
  "$J90_ENV" "git -C $J90_REPO/subdir-b5 push" "$TMPROOT" "all phases passed"
assert_deny "j90ba B5: '-C <bare-repo> push' denies (no work tree), never silently allows" \
  "$J90_ENV" "git -C $J90_BARE push origin main" "$TMPROOT" "work tree"
rmdir "$J90_REPO/subdir-b5"

# Checkout-hook suppression: this machine's repos set core.hooksPath (bd
# shims include post-checkout); materializing the gate worktree must NOT run
# them (cost + mutable bd state inside a validation step).
J90_HOOKS_DIR="$TMPROOT/j90-hookspath"
mkdir -p "$J90_HOOKS_DIR"
printf '#!/usr/bin/env bash\necho fired > "%s/j90-hook-fired"\n' "$TMPROOT" > "$J90_HOOKS_DIR/post-checkout"
chmod +x "$J90_HOOKS_DIR/post-checkout"
git -C "$J90_REPO" config core.hooksPath "$J90_HOOKS_DIR"
J90_HOOKS_OUT=$(run_hook "$J90_ENV" "git push" "$J90_REPO")
git -C "$J90_REPO" config --unset core.hooksPath
if [ ! -f "$TMPROOT/j90-hook-fired" ] && echo "$J90_HOOKS_OUT" | grep -qF "all phases passed"; then
  PASS=$((PASS+1))
  echo "PASS: j90ba hooksPath: gate materialization does not run checkout hooks"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  j90ba hooksPath suppression (fired=$([ -f "$TMPROOT/j90-hook-fired" ] && echo yes || echo no) got: ${J90_HOOKS_OUT:0:200})"
  echo "FAIL: j90ba hooksPath: checkout hook fired during gate materialization or push not green"
fi
rm -f "$TMPROOT/j90-hook-fired"

# Signal cleanup: TERM delivered mid-phase-4 (slow test command) must still
# tear down the gate worktree ADMIN entry via the trap. setsid gives the hook
# its own process group so TERM reaches the in-flight child too (bash defers
# traps until the foreground child exits).
git -C "$J90_REPO" config sable.testCommand "sleep 30"
J90_SIG_INPUT=$(make_input "git push" "$J90_REPO")
env -i PATH="$PATH" CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
  SABLE_PRE_PUSH_TYPECHECK_COMMAND=true \
  setsid bash "$HOOK" <<< "$J90_SIG_INPUT" >/dev/null 2>&1 &
J90_SIG_PID=$!
J90_SIG_SEEN=0
for _ in $(seq 1 50); do
  if [ "$(git -C "$J90_REPO" worktree list 2>/dev/null | wc -l | tr -d ' ')" = "2" ]; then
    J90_SIG_SEEN=1
    break
  fi
  sleep 0.2
done
kill -TERM -- "-$J90_SIG_PID" 2>/dev/null
wait "$J90_SIG_PID" 2>/dev/null
sleep 0.5
J90_SIG_WT=$(git -C "$J90_REPO" worktree list 2>/dev/null | wc -l | tr -d ' ')
git -C "$J90_REPO" config --unset sable.testCommand
if [ "$J90_SIG_SEEN" = "1" ] && [ "$J90_SIG_WT" = "1" ]; then
  PASS=$((PASS+1))
  echo "PASS: j90ba signal cleanup: TERM mid-phase tears down the gate worktree admin entry"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  j90ba signal cleanup (materialized=$J90_SIG_SEEN worktrees-after=$J90_SIG_WT)"
  echo "FAIL: j90ba signal cleanup: materialized=$J90_SIG_SEEN worktrees-after-TERM=$J90_SIG_WT"
fi

# Dependency-bridge fixture (portability): committed test code requires an
# IGNORED untracked dependency dir (node_modules/gate-probe) AND asserts an
# unrelated untracked SOURCE file is absent. One committed suite proves both
# halves: the audited bridge makes ignored deps visible in the gate worktree,
# while untracked source stays excluded. Without the bridge this push denies
# (probe missing); with an over-broad bridge it also denies (stray present).
J90DEP_BARE="$TMPROOT/j90dep-bare.git"
J90DEP="$TMPROOT/j90dep-repo"
rm -rf "$J90DEP_BARE" "$J90DEP"
git init -q --bare "$J90DEP_BARE"
git clone -q "$J90DEP_BARE" "$J90DEP" 2>/dev/null
(
  cd "$J90DEP" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  printf 'node_modules/\n' > .gitignore
  printf 'test -f node_modules/gate-probe && test ! -f stray-source.txt\n' > run-tests.sh
  printf 'testCommand=bash ./run-tests.sh\n' > .sable
  git add -A; git commit -q -m base
  git push -q "$J90DEP_BARE" HEAD:refs/heads/main
  mkdir -p node_modules
  echo probe > node_modules/gate-probe
  echo stray > stray-source.txt
)
J90DEP_OUT=$(run_hook "$J90_ENV" "git push" "$J90DEP")
assert_context_output "j90ba dependency bridge: ignored node_modules is bridged into the gate worktree while untracked source stays excluded" \
  "$J90DEP_OUT" "all phases passed"
assert_context_output "j90ba dependency bridge: the verdict names what was bridged" \
  "$J90DEP_OUT" "bridged ignored dependency dir(s): node_modules"
rm -rf "$J90DEP_BARE" "$J90DEP"

# Unscoped control: a repo with NO explicit integration config is still
# governed — a multi-refspec push is denied there too.
J90_PLAIN_BARE="$TMPROOT/j90ba-plain-bare.git"
J90_PLAIN="$TMPROOT/j90ba-plain-repo"
rm -rf "$J90_PLAIN_BARE" "$J90_PLAIN"
git init -q --bare "$J90_PLAIN_BARE"
git clone -q "$J90_PLAIN_BARE" "$J90_PLAIN" 2>/dev/null
(
  cd "$J90_PLAIN" || exit 1
  git config user.email t@t; git config user.name t
  git checkout -q -B main
  echo x > f.txt; git add -A; git commit -q -m init
  git push -q "$J90_PLAIN_BARE" HEAD:refs/heads/main
  git checkout -q -b side
)
J90_PLAIN_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true SABLE_PRE_PUSH_TEST_PHASE=skip"
assert_deny "j90ba unscoped: multi-refspec push is denied even without explicit integration config" \
  "$J90_PLAIN_ENV" "git push origin main side" "$J90_PLAIN" "exactly one"
rm -rf "$J90_BARE" "$J90_REPO" "$J90_PLAIN_BARE" "$J90_PLAIN"

# ---------- SABLE-y4nom.7.2: INCOMPLETE-BY-TRIM verdict at the outer gate --
# The marker line DOMINATES the exit code (codex v2.1 C1): a wrapper like
# `sable-dev-check ... || true` launders rc3 to rc0 while the marker
# survives, so gating on rc alone would print all-phases-passed over an
# unverified claim. Marker present => deny INCOMPLETE regardless of
# TEST_EXIT; marker with rc0 additionally names the swallowed-verdict
# violation; a bare exit 3 WITHOUT the marker stays a generic test failure.
# The marker is built >1500 bytes (40 omitted suite names) so that the
# 1500-char output TAIL can only ever hold a TRUNCATED marker: a deny that
# carries the FULL byte-exact line has necessarily extracted it SEPARATELY
# from sable_tail_chars. (codex round-1: a short marker inside the tail let
# the extraction assertion green vacuously.)
VE2_MARKER='SABLE_DEV_CHECK_INCOMPLETE_BY_TRIM=['
for _i in $(seq 1 40); do
  VE2_MARKER="${VE2_MARKER}\"test-omitted-suite-number-${_i}-padding.sh\", "
done
VE2_MARKER="${VE2_MARKER}\"test-omitted-final.sh\"]"
ve2_fixture() {
  # $1 = the committed test script body
  VE2_BARE="$TMPROOT/ve2-bare.git"
  VE2_REPO="$TMPROOT/ve2-repo"
  rm -rf "$VE2_BARE" "$VE2_REPO"
  git init -q --bare "$VE2_BARE"
  git clone -q "$VE2_BARE" "$VE2_REPO" 2>/dev/null
  (
    cd "$VE2_REPO" || exit 1
    git config user.email t@t; git config user.name t
    git checkout -q -B main
    printf '%s\n' "$1" > run-tests.sh
    printf 'testCommand=bash ./run-tests.sh\n' > .sable
    git add -A; git commit -q -m base
    git push -q "$VE2_BARE" HEAD:refs/heads/main
    echo change > f.txt; git add -A; git commit -q -m change
  )
}
VE2_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true"

# ve2_deny_checks NAME OUT POS NEG1 NEG2: positive AND negative substring
# assertions against the DECODED permissionDecisionReason — the hook output
# is JSON, so a marker containing quotes is escaped in the raw stream and a
# raw grep -F could never byte-match it (codex). Python containment also
# sidesteps echo|grep SIGPIPE flakiness on the oversized value. Test names
# are not assertions, so the absences are checked explicitly.
ve2_deny_checks() {
  local name="$1" out="$2" pos="$3" neg1="$4" neg2="$5" verdict
  verdict=$(VE2_RAW="$out" VE2_POS="$pos" VE2_NEG1="$neg1" VE2_NEG2="$neg2" python3 - <<'PYEOF'
import json, os
raw = os.environ["VE2_RAW"]
pos, neg1, neg2 = (os.environ[k] for k in ("VE2_POS", "VE2_NEG1", "VE2_NEG2"))
why = []
reason = ""
try:
    data = json.loads(raw)
    hso = data.get("hookSpecificOutput", data)
    if hso.get("permissionDecision") != "deny":
        why.append("no-deny")
    reason = str(hso.get("permissionDecisionReason", ""))
except (ValueError, AttributeError) as exc:
    why.append(f"unparseable:{exc}")
if pos and pos not in reason:
    why.append(f"missing[{pos[:60]}]")
if neg1 and neg1 in reason:
    why.append(f"forbidden[{neg1[:40]}]")
if neg2 and neg2 in reason:
    why.append(f"forbidden[{neg2[:40]}]")
print("OK" if not why else ";".join(why))
PYEOF
)
  if [ "$verdict" = "OK" ]; then PASS=$((PASS+1)); echo "PASS: $name"; else
    FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $name ($verdict)"
    echo "FAIL: $name"; echo "  $verdict"; echo "  Got: ${out:0:400}"
  fi
}

# rc3 + oversized marker, with >1500 bytes of trailing filler AFTER it: the
# tail window holds only filler, so both the INCOMPLETE verdict and the
# byte-exact full marker must come from separate extraction.
ve2_fixture "echo '$VE2_MARKER'; head -c 3000 /dev/zero | tr '\\0' 'y'; echo; exit 3"
VE2_OUT=$(run_hook "$VE2_ENV" "git push" "$VE2_REPO")
ve2_deny_checks "y4nom.7.2: marker + rc3 denies INCOMPLETE (never all-phases-passed, never generic failure)" \
  "$VE2_OUT" "INCOMPLETE" "all phases passed" "Tests failed."
ve2_deny_checks "y4nom.7.2: the deny carries the FULL byte-exact marker despite tail truncation" \
  "$VE2_OUT" "$VE2_MARKER" "" ""

# rc0 + marker (the || true laundering wrapper) => still INCOMPLETE, naming
# the swallowed-verdict contract violation; never all-phases-passed.
ve2_fixture "echo '$VE2_MARKER'; exit 0"
VE2_OUT=$(run_hook "$VE2_ENV" "git push" "$VE2_REPO")
ve2_deny_checks "y4nom.7.2: marker + rc0 (laundered) still denies INCOMPLETE" \
  "$VE2_OUT" "INCOMPLETE" "all phases passed" ""
ve2_deny_checks "y4nom.7.2: marker + rc0 names the swallowed-verdict contract violation" \
  "$VE2_OUT" "swallowed" "" ""

# marker + rc1 (kept suites failed AND the plan omitted) => INCOMPLETE
# classification with the underlying failure ALSO named — "nothing failed"
# must not be claimed here.
ve2_fixture "echo '$VE2_MARKER'; exit 1"
VE2_OUT=$(run_hook "$VE2_ENV" "git push" "$VE2_REPO")
ve2_deny_checks "y4nom.7.2: marker + rc1 classifies INCOMPLETE and names the underlying failure" \
  "$VE2_OUT" "INCOMPLETE" "all phases passed" "Nothing that ran failed"
ve2_deny_checks "y4nom.7.2: marker + rc1 names the kept-suite failure alongside the omissions" \
  "$VE2_OUT" "did not all pass" "" ""

# marker + rc124 (round-1 B7): the command emits the oversized marker then
# stays alive past the outer testTimeout — TEST_EXIT becomes 124 WITH the
# marker in the captured output. Classification must be INCOMPLETE with the
# timeout named, the full marker separately extracted, and neither
# all-phases-passed nor the generic failure text present.
ve2_fixture "echo '$VE2_MARKER'; sleep 30"
(
  cd "$VE2_REPO" || exit 1
  printf 'testCommand=bash ./run-tests.sh\ntestTimeout=1\n' > .sable
  git add .sable; git commit -q -m timeout-marker-fixture
)
VE2_OUT=$(run_hook "$VE2_ENV" "git push" "$VE2_REPO")
ve2_deny_checks "y4nom.7.2: marker + rc124 classifies INCOMPLETE (decoded positive, not the test name)" \
  "$VE2_OUT" "INCOMPLETE" "all phases passed" "Tests failed."
ve2_deny_checks "y4nom.7.2: marker + rc124 names the timeout alongside the omissions" \
  "$VE2_OUT" "TIMED OUT" "" ""
ve2_deny_checks "y4nom.7.2: marker + rc124 still carries the FULL byte-exact marker" \
  "$VE2_OUT" "$VE2_MARKER" "" ""

# bare exit 3, NO marker => generic phase-4 failure; the INCOMPLETE verdict
# must be ABSENT (checked, not implied by the name).
ve2_fixture "exit 3"
VE2_OUT=$(run_hook "$VE2_ENV" "git push" "$VE2_REPO")
ve2_deny_checks "y4nom.7.2: exit 3 without the marker stays a generic test failure (no INCOMPLETE verdict)" \
  "$VE2_OUT" "Tests failed." "INCOMPLETE" ""

# timeout remediation names the shared publisher, not raw per-worktree
# reporter runs (v2.1 C5's second site).
ve2_fixture "sleep 30"
(
  cd "$VE2_REPO" || exit 1
  printf 'testCommand=bash ./run-tests.sh\ntestTimeout=1\n' > .sable
  git add .sable; git commit -q -m timeout-fixture
)
assert_deny "y4nom.7.2: the 124 remediation teaches sable-dev-check --publish-cost-profile" \
  "$VE2_ENV" "git push" "$VE2_REPO" "publish-cost-profile"
rm -rf "$VE2_BARE" "$VE2_REPO"

# The escaping plant is one request in addition to the suite's 105 real hook
# executions. Pinning one encoder start and all 106 serviced requests makes the
# optimization load-bearing: restoring either per-call python3 implementation,
# or bypassing the shared encoder for one case, fails this control.
SYNTHETIC_INPUT_ENCODER_STARTS=$(grep -c '^start$' "$SYNTHETIC_INPUT_ENCODER_TRACE")
SYNTHETIC_INPUT_ENCODER_REQUESTS=$(grep -c '^request$' "$SYNTHETIC_INPUT_ENCODER_TRACE")
if [ "$SYNTHETIC_INPUT_ESCAPE_ACTUAL" = "$SYNTHETIC_INPUT_ESCAPE_EXPECTED" ] \
   && [ "$SYNTHETIC_INPUT_ENCODER_STARTS" -eq 1 ] \
   && [ "$SYNTHETIC_INPUT_ENCODER_REQUESTS" -eq 106 ]; then
  PASS=$((PASS+1))
  echo "PASS: synthetic-input encoder preserves exact JSON escaping and serves all 106 payloads from one python3 process"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  synthetic-input encoder reuse/escaping (starts=$SYNTHETIC_INPUT_ENCODER_STARTS requests=$SYNTHETIC_INPUT_ENCODER_REQUESTS actual=$SYNTHETIC_INPUT_ESCAPE_ACTUAL)"
  echo "FAIL: synthetic-input encoder preserves exact JSON escaping and serves all 106 payloads from one python3 process"
fi

sable_json_encoder_stop
if kill -0 "$SYNTHETIC_INPUT_ENCODER_PID" 2>/dev/null; then
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  synthetic-input encoder process was not reaped (pid=$SYNTHETIC_INPUT_ENCODER_PID)"
  echo "FAIL: synthetic-input encoder is reaped before suite exit"
else
  PASS=$((PASS+1))
  echo "PASS: synthetic-input encoder is reaped before suite exit"
fi

# The two high-volume fixed-substring assertion helpers execute for nearly
# every guarded fixture. Keep their matching in Bash: restoring echo|grep
# pipelines would add hundreds of avoidable processes to this one suite.
ASSERT_MATCH_HELPERS="$(declare -f assert_deny assert_context_output)"
if [ "$ASSERT_DENY_CALLS" -gt 0 ] \
   && [ "$ASSERT_CONTEXT_OUTPUT_CALLS" -gt 0 ] \
   && [[ "$ASSERT_MATCH_HELPERS" != *grep* ]]; then
  PASS=$((PASS+1))
  echo "PASS: high-volume verdict assertions use Bash fixed-substring matching (no grep pipelines)"
else
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  verdict assertion helper structure (deny=$ASSERT_DENY_CALLS context=$ASSERT_CONTEXT_OUTPUT_CALLS)"
  echo "FAIL: high-volume verdict assertions use Bash fixed-substring matching (no grep pipelines)"
fi

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
