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
# Agent-Teams member identity (SABLE-amj.2). Capture-verified (SABLE-amj.1):
# a team member spawned with name=<registry name> produces a hook input whose
# agent_type field carries that NAME — the subagent_type (e.g. general-purpose)
# does NOT appear in agent_type, and agent_id is an opaque internal id. So a
# member named "optimus" resolves through the SAME agent_type path as a nested
# subagent; lib-identity needs no teams-specific branch. The load-bearing rule
# lives at spawn time: member name MUST equal the registry name (see SABLE-amj.6).
# --------------------------------------------------------------------------

# 11. Real captured team-member hook-input shape (name=optimus, opaque agent_id,
#     plus the session_id/cwd/permission_mode/effort fields a member actually
#     carries): resolves as the optimus manager via agent_type.
run_case "teams member optimus (captured shape) resolves as manager via agent_type" \
  '{"session_id":"shared-with-lead","cwd":"/repo","permission_mode":"auto","agent_id":"a9aeafeb6cf464770","agent_type":"optimus","effort":{"level":"xhigh"},"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"touch x"}}' \
  "" "" \
  "optimus|epic_manager|agent_type|1|1|1"

# 12. A member spawned with a NON-registry name stands down — this is precisely
#     why amj.6 must spawn members under their registry name (naming rationale).
run_case "teams member with non-registry name (optimus-probe) stands down" \
  '{"agent_id":"b2teamopq","agent_type":"optimus-probe","hook_event_name":"PreToolUse","tool_name":"Bash"}' \
  "" "" \
  "optimus-probe||agent_type|1|0|0"

# --------------------------------------------------------------------------
# market-brief-package-73t4 — instance-suffixed manager identity resolution.
#
# A respawned manager instance arrives with an instance-suffixed agent_type
# (e.g. 'tarzan-2') while only the bare name ('tarzan') is registered in
# agents.yaml. The exact-match registry lookup (line ~88, `$0 == "  " name ":"`)
# misses, so a genuine one_off_manager resolves as an unregistered worker and
# the pre-push gate denies its push with "worker subagents do not push".
#
# STATUS: RED tests only. The FIX (how a -N instance maps to its base registry
# entry) is DEFERRED pending a Lincoln ruling on the approach: a BLANKET strip
# of a trailing -[0-9]+ before the lookup would let ANY '<registered>-<n>'
# self-elevate to manager — a push-gate escalation — so the mechanism is not
# settled. The desired-behavior assertions are gated behind SABLE_PENDING_73T4=1
# and are RED today by design. Capture the red with:
#     SABLE_PENDING_73T4=1 bash hooks/test/test-lib-identity.sh
#
# The three cases that run in the default suite pin TODAY's behavior (so the
# fix is DETECTED when it lands) and lock the security boundary that every
# candidate fix must preserve (a non-numeric suffix is not an instance suffix
# and must never elevate).
# --------------------------------------------------------------------------

# Characterization (passes today): tarzan-2 currently resolves as an
# unregistered, non-manager worker — this IS the bug. Flip this expectation to
# the PENDING one when the approved fix lands.
run_case "73t4 characterization: tarzan-2 currently resolves unregistered/non-manager (the bug)" \
  '{"agent_id":"t73-char","agent_type":"tarzan-2"}' \
  "" "" \
  "tarzan-2||agent_type|1|0|0"

# Security boundary (must hold before AND after any fix): a non-numeric suffix
# is NOT an instance suffix — it must never resolve to the base manager entry.
run_case "73t4 boundary: tarzan-abc (non-numeric suffix) must NOT elevate to manager" \
  '{"agent_id":"t73-bound","agent_type":"tarzan-abc"}' \
  "" "" \
  "tarzan-abc||agent_type|1|0|0"

# Regression guard: the bare registered name is unchanged by any fix.
run_case "73t4 regression: bare tarzan subagent still resolves one_off_manager" \
  '{"agent_id":"t73-reg","agent_type":"tarzan"}' \
  "" "" \
  "tarzan|one_off_manager|agent_type|1|1|1"

# PENDING (RED by design, gated): the acceptance — a respawned manager instance
# resolves as its base registry type/manager while keeping the full suffixed
# name for display. Mechanism-agnostic: it asserts the resolved outputs, not HOW
# the mapping is done (holds for a suffix-strip lookup OR for registering the
# instance). RED until the Lincoln-approved fix lands.
if [ "${SABLE_PENDING_73T4:-}" = "1" ]; then
  run_case "73t4 PENDING(RED): tarzan-2 resolves one_off_manager via base registry entry" \
    '{"agent_id":"t73-p1","agent_type":"tarzan-2"}' \
    "" "" \
    "tarzan-2|one_off_manager|agent_type|1|1|1"
  run_case "73t4 PENDING(RED): optimus-3 resolves epic_manager via base registry entry" \
    '{"agent_id":"t73-p2","agent_type":"optimus-3"}' \
    "" "" \
    "optimus-3|epic_manager|agent_type|1|1|1"
fi

# --------------------------------------------------------------------------
# sable_resolve_dispatch_lane unit tests (SABLE-uz9.9)
# Manager-subagents now dispatch workers natively (nested Agent, CC 2.1.177,
# SABLE-uz9.8) — governance must ACTIVATE for them where it previously stood
# down for all subagents. Workers (non-manager subagents) still stand down.
# --------------------------------------------------------------------------

# run_lane_case <case-name> <json> <env_name> <env_role> <expect: active|lane>
run_lane_case() {
  local label="$1" json="$2" env_name="$3" env_role="$4" expect="$5"
  local got
  got=$(
    unset CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE
    [ -n "$env_name" ] && export CLAUDE_AGENT_NAME="$env_name"
    [ -n "$env_role" ] && export CLAUDE_AGENT_ROLE="$env_role"
    # Pin mode-file to a nonexistent fixture so the live cockpit state cannot
    # contaminate main-session cases (cf. SABLE-wtv).
    export SABLE_MODE_STATE="$FIXTURE_DIR/nonexistent-mode-state.json"
    # shellcheck disable=SC1090
    source "$LIB"
    sable_resolve_dispatch_lane "$json"
    printf '%s|%s' "$SABLE_DISPATCH_ACTIVE" "$SABLE_DISPATCH_LANE"
  )
  if [ "$got" = "$expect" ]; then
    pass "$label"
  else
    fail "$label" "expected [$expect] got [$got]"
  fi
}

# NEW BEHAVIOR — manager-subagent dispatches a worker: governance active, lane=self
run_lane_case "manager-subagent optimus dispatching a worker: active, lane=optimus" \
  '{"agent_id":"d1","agent_type":"optimus","tool_name":"Agent","tool_input":{"subagent_type":"general-purpose","prompt":"work on SABLE-x"}}' \
  "" "" \
  "1|optimus"

run_lane_case "manager-subagent tarzan dispatching a worker: active, lane=tarzan" \
  '{"agent_id":"d2","agent_type":"tarzan","tool_name":"Agent","tool_input":{"subagent_type":"general-purpose","prompt":"fix SABLE-y"}}' \
  "" "" \
  "1|tarzan"

# GUARD — non-manager subagents (workers / planning agents) still stand down
run_lane_case "worker-subagent Explore does NOT trigger governance" \
  '{"agent_id":"d3","agent_type":"Explore","tool_name":"Agent","tool_input":{"subagent_type":"general-purpose"}}' \
  "" "" \
  "0|"

run_lane_case "planning-subagent sherlock does NOT dispatch (registered non-manager)" \
  '{"agent_id":"d4","agent_type":"sherlock","tool_name":"Agent"}' \
  "" "" \
  "0|"

# REGRESSION — env-launched legacy manager terminal still active
run_lane_case "env chuck manager terminal: active, lane=chuck (legacy dual-mode)" \
  '{"tool_name":"Agent","session_id":"s1"}' \
  "chuck" "manager" \
  "1|chuck"

# REGRESSION — anonymous main session with no execution-mode file stands down
run_lane_case "anonymous main session, no cockpit mode file: stands down" \
  '{"tool_name":"Agent","session_id":"s2"}' \
  "" "" \
  "0|"

# --------------------------------------------------------------------------
# v3 lane contract (SABLE-how 7-row table delta + SABLE-dd1 fail-open / deletion)
# Identity is the ONLY lane source; Lincoln main-session exec lane = self;
# sable__parse_dispatch_for is deleted; infra errors fail open to ACTIVE=0.
# --------------------------------------------------------------------------

# Like run_lane_case but with a writable mode file and optional registry override.
# Args: label, json, mode_json (""=nonexistent mode file), yaml ("" = fixture), expect
run_lane_env() {
  local label="$1" json="$2" mode_json="$3" yaml="$4" expect="$5" got
  got=$(
    unset CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE
    if [ -n "$mode_json" ]; then
      printf '%s' "$mode_json" > "$FIXTURE_DIR/mode-case.json"
      export SABLE_MODE_STATE="$FIXTURE_DIR/mode-case.json"
    else
      export SABLE_MODE_STATE="$FIXTURE_DIR/nonexistent-mode-state.json"
    fi
    [ -n "$yaml" ] && export SABLE_AGENTS_YAML="$yaml"
    # shellcheck disable=SC1090
    source "$LIB"
    sable_resolve_dispatch_lane "$json"
    printf '%s|%s' "$SABLE_DISPATCH_ACTIVE" "$SABLE_DISPATCH_LANE"
  )
  if [ "$got" = "$expect" ]; then pass "$label"; else fail "$label" "expected [$expect] got [$got]"; fi
}

# Row 2 — anonymous main session in EXECUTION mode → lane=lincoln (NOT cockpit)
run_lane_env "lane row2: exec-mode main session → active, lane=lincoln (not cockpit)" \
  '{"tool_name":"Agent"}' '{"mode":"execution"}' "" "1|lincoln"
# Row 3a — planning mode stands down
run_lane_env "lane row3a: planning-mode main session stands down" \
  '{"tool_name":"Agent"}' '{"mode":"planning"}' "" "0|"
# dd1 — Dispatching-for prompt IGNORED for main-session exec lane (lincoln, not optimus)
run_lane_env "dd1: Dispatching-for prompt ignored for main-session lane (lincoln)" \
  '{"tool_name":"Agent","tool_input":{"prompt":"Dispatching-for: optimus\nwork on x"}}' '{"mode":"execution"}' "" "1|lincoln"
# dd1 — malformed mode-file JSON fails open
run_lane_env "dd1: malformed mode-file JSON stands down (fail open)" \
  '{"tool_name":"Agent"}' 'not-json{' "" "0|"
# dd1 — registry missing → manager subagent fails open (stands down)
run_lane_env "dd1: registry missing → manager subagent fails open" \
  '{"agent_id":"a1","agent_type":"tarzan","tool_name":"Agent"}' "" "/nonexistent/agents.yaml" "0|"
# dd1 — registry unreadable (chmod 000) fails open
UNREAD="$FIXTURE_DIR/unreadable.yaml"; cp "$FIXTURE_DIR/agents.yaml" "$UNREAD"; chmod 000 "$UNREAD" 2>/dev/null
run_lane_env "dd1: registry unreadable (chmod 000) fails open" \
  '{"agent_id":"a5","agent_type":"tarzan","tool_name":"Agent"}' "" "$UNREAD" "0|"
chmod 644 "$UNREAD" 2>/dev/null

# dd1 — empty agent_type stands down (missing/empty equivalence)
run_lane_case "dd1: empty agent_type stands down" \
  '{"agent_id":"a2","agent_type":""}' "" "" "0|"
# dd1 — Dispatching-for IGNORED for manager subagent (lane=tarzan, not optimus)
run_lane_case "dd1: Dispatching-for prompt ignored for manager subagent (tarzan)" \
  '{"agent_id":"a3","agent_type":"tarzan","tool_name":"Agent","tool_input":{"prompt":"Dispatching-for: optimus\nx"}}' "" "" "1|tarzan"
# dd1 — contamination: env=tarzan manager with agent_type=Explore child stands down
run_lane_case "dd1: env=tarzan + child agent_type=Explore stands down (agent_id wins)" \
  '{"agent_id":"a4","agent_type":"Explore","tool_name":"Agent"}' "tarzan" "manager" "0|"
# dd1 — malformed hook JSON + env chuck → lane=chuck (legacy path intact)
run_lane_case "dd1: malformed hook JSON + env chuck → active, lane=chuck" \
  'not-json-at-all' "chuck" "manager" "1|chuck"

# dd1 — sable__parse_dispatch_for is DELETED (no dead relay helper survives)
if ( unset CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE; source "$LIB"; declare -F sable__parse_dispatch_for >/dev/null 2>&1 ); then
  fail "dd1: sable__parse_dispatch_for is deleted" "function still defined after sourcing"
else
  pass "dd1: sable__parse_dispatch_for is deleted"
fi

# --------------------------------------------------------------------------
# SABLE-d50.4 — unified mode-state override env var (SABLE_MODE_STATE).
# lib-identity must read the SAME override name as bin/sable-mode and
# mode-interlock.sh. The retired SABLE_MODE_FILE name must NOT be honored.
# --------------------------------------------------------------------------

# Helper: resolve the lane with HOME pinned to an empty dir (no default mode
# file) and the named env var pointing at an execution-mode fixture.
run_mode_state_case() {
  local label="$1" var_name="$2" expect="$3" got
  got=$(
    unset CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE SABLE_MODE_STATE SABLE_MODE_FILE
    local home="$FIXTURE_DIR/d504-home"
    mkdir -p "$home"
    export HOME="$home"
    # Run from a NON-git dir so the no-override path exercises the HOME fallback
    # (post SABLE-5hck the resolver uses cwd's repo when in git; HOME is only the
    # non-git fallback). $home is under a mktemp dir, so it is not a git repo.
    cd "$home" || return 1
    printf '%s' '{"mode":"execution"}' > "$FIXTURE_DIR/d504-mode.json"
    export "$var_name=$FIXTURE_DIR/d504-mode.json"
    # shellcheck disable=SC1090
    source "$LIB"
    sable_resolve_dispatch_lane '{"tool_name":"Agent"}'
    printf '%s|%s' "$SABLE_DISPATCH_ACTIVE" "$SABLE_DISPATCH_LANE"
  )
  if [ "$got" = "$expect" ]; then pass "$label"; else fail "$label" "expected [$expect] got [$got]"; fi
}

# SABLE_MODE_STATE drives lib-identity (unified override name)
run_mode_state_case "d50.4: SABLE_MODE_STATE overrides lib-identity mode path → lane=lincoln" \
  "SABLE_MODE_STATE" "1|lincoln"
# Retired SABLE_MODE_FILE name is NOT honored (no default file at the pinned HOME → stands down)
run_mode_state_case "d50.4: retired SABLE_MODE_FILE is ignored by lib-identity → stands down" \
  "SABLE_MODE_FILE" "0|"

# --------------------------------------------------------------------------
# SABLE-5hck.4 — per-repo dispatch-lane mode resolution.
# With NO SABLE_MODE_STATE override, lib-identity must resolve the mode from the
# repo the call runs in (hook-input cwd, else process cwd), so the lincoln
# main-session exec lane keys off the right repo when sessions run in several
# repos at once. The hook-input cwd takes precedence over the process cwd.
# --------------------------------------------------------------------------
mk_mode_repo() {
  # $1 = mode word; echoes a fresh git repo carrying that in-repo mode-state file
  local d; d="$(mktemp -d)"
  git -C "$d" init -q
  git -C "$d" -c user.email=t@t -c user.name=t commit --allow-empty -m init -q
  mkdir -p "$d/.claude/sable/state"
  printf '%s' "{\"mode\":\"$1\"}" > "$d/.claude/sable/state/mode-state.json"
  printf '%s\n' "$d"
}
run_lane_inrepo() {
  # label, json, process_cwd, expect
  local label="$1" json="$2" pcwd="$3" expect="$4" got
  got=$(
    unset CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE SABLE_MODE_STATE SABLE_MODE_FILE
    cd "$pcwd" || exit 1
    # shellcheck disable=SC1090
    source "$LIB"
    sable_resolve_dispatch_lane "$json"
    printf '%s|%s' "$SABLE_DISPATCH_ACTIVE" "$SABLE_DISPATCH_LANE"
  )
  if [ "$got" = "$expect" ]; then pass "$label"; else fail "$label" "expected [$expect] got [$got]"; fi
}

LANE_RX="$(mk_mode_repo execution)"
LANE_RY="$(mk_mode_repo planning)"
# cwd-in-json: execution repo → lane=lincoln; planning repo → stands down
run_lane_inrepo "5hck.4: exec repo via cwd-in-json → lane=lincoln" \
  "{\"tool_name\":\"Agent\",\"cwd\":\"$LANE_RX\"}" "$LANE_RX" "1|lincoln"
run_lane_inrepo "5hck.4: planning repo via cwd-in-json → stands down" \
  "{\"tool_name\":\"Agent\",\"cwd\":\"$LANE_RY\"}" "$LANE_RY" "0|"
# no cwd field → falls back to process cwd
run_lane_inrepo "5hck.4: exec repo via process cwd (no cwd field) → lane=lincoln" \
  '{"tool_name":"Agent"}' "$LANE_RX" "1|lincoln"
# hook-input cwd (planning) takes precedence over process cwd (execution)
run_lane_inrepo "5hck.4: json cwd (planning) wins over process cwd (exec) → stands down" \
  "{\"tool_name\":\"Agent\",\"cwd\":\"$LANE_RY\"}" "$LANE_RX" "0|"
rm -rf "$LANE_RX" "$LANE_RY"

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

# --------------------------------------------------------------------------
# sable_resolve_push_repo_dir unit tests (SABLE-041)
# Resolves the effective git dir from a push command's `git -C <path>`,
# applied to the shell cwd with git semantics; falls back to cwd when absent.
# --------------------------------------------------------------------------
resolve_dir_test() {
  local label="$1" cwd="$2" cmd="$3" expect="$4" got
  got=$(sable_resolve_push_repo_dir "$cwd" "$cmd")
  if [ "$got" = "$expect" ]; then
    pass "$label"
  else
    fail "$label" "expected [$expect] got [$got]"
  fi
}

resolve_dir_test "resolve_push_repo_dir: no -C falls back to cwd" \
  "/main" "git push" "/main"
resolve_dir_test "resolve_push_repo_dir: absolute -C overrides cwd" \
  "/main" "git -C /wt push" "/wt"
resolve_dir_test "resolve_push_repo_dir: absolute -C with push args" \
  "/main" "git -C /wt push -u origin wk-x" "/wt"
resolve_dir_test "resolve_push_repo_dir: relative -C joins cwd" \
  "/main" "git -C sub push" "/main/sub"
resolve_dir_test "resolve_push_repo_dir: -c before -C is skipped, -C wins" \
  "/main" "git -c http.x=y -C /wt push" "/wt"
resolve_dir_test "resolve_push_repo_dir: env-assignment prefix + -C" \
  "/main" "SABLE_SKIP_PRE_PUSH=1 git -C /wt push" "/wt"
resolve_dir_test "resolve_push_repo_dir: double -C composes (git semantics)" \
  "/main" "git -C /a -C b push" "/a/b"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
