#!/usr/bin/env bash
# pre-push-rebase-test.sh — Force rebase + tests before git push succeeds
# Trigger: PreToolUse:Bash matching `git push`
#
# Catches "branch is behind main" and regression cases LOCALLY before exposing
# to CI. Reduces Chuck's workload to genuine cross-PR conflicts only.
#
# Configuration:
#   $SABLE_BASE_BRANCH              — branch to rebase against (default: origin/main)
#   $SABLE_TEST_COMMAND             — test invocation (see notes below)
#   $SABLE_PRE_PUSH_TEST_TIMEOUT    — seconds allowed for tests (default: 60)
#   $SABLE_SKIP_PRE_PUSH            — set to "1" to bypass (for emergency push)
#
# IMPORTANT — timeout coupling:
#   The outer hook timeout in settings.json MUST exceed SABLE_PRE_PUSH_TEST_TIMEOUT
#   (plus ~30s buffer for fetch/rebase). If the outer timeout is lower, Claude Code
#   kills the hook before the tests complete regardless of the inner budget.
#
#   Default pairing: inner 60s, outer 90000ms (settings.json "timeout": 90000).
#   For a 5-minute test budget: inner 300, outer 330000.
#
# Recommended pattern: run a FAST SUBSET (smoke + changed unit tests) pre-push,
# not the full suite. Set SABLE_TEST_COMMAND to something like:
#   export SABLE_TEST_COMMAND="npm test -- --changed --run"
#   export SABLE_TEST_COMMAND="pytest tests/unit -x --lf"
#   export SABLE_TEST_COMMAND="bash ./scripts/pre-push-smoke.sh"
# Keep the full suite in CI. Pre-push under 60s keeps the human-visible pause
# tolerable and discourages SABLE_SKIP_PRE_PUSH escape hatches.
#
# Skips: subagent context (workers shouldn't push), --force pushes (let user bypass
# explicitly with their own intent), pushes that aren't to a feature branch.

set -euo pipefail

[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
agent_id = d.get('agent_id', '')
cwd = d.get('cwd', '')
print(f'{agent_id}\n{cwd}\n{cmd}')
" 2>/dev/null) || exit 0

NESTED_AGENT_ID=$(echo "$PARSED" | sed -n '1p')
CWD=$(echo "$PARSED" | sed -n '2p')
COMMAND=$(echo "$PARSED" | sed -n '3,$p')

# Skip subagent — workers don't push
[ -n "$NESTED_AGENT_ID" ] && exit 0

# Only act on git push commands
echo "$COMMAND" | grep -qE '\bgit\s+push\b' || exit 0

# Skip force pushes — explicit user/agent intent, bypass our enforcement
echo "$COMMAND" | grep -qE '(\-\-force|\-f\b)' && exit 0

# Skip if explicit override
[ "${SABLE_SKIP_PRE_PUSH:-}" = "1" ] && exit 0

[ -z "$CWD" ] && exit 0
[ ! -d "$CWD/.git" ] && [ ! -f "$CWD/.git" ] && exit 0

BASE_BRANCH="${SABLE_BASE_BRANCH:-origin/main}"

# Step 1: fetch
FETCH_OUT=$(git -C "$CWD" fetch origin 2>&1) || {
  python3 -c "
import json, os
out = os.environ.get('FETCH_OUT', '')[:300]
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': f'Pre-push: git fetch failed:\n{out}\nResolve network/auth and retry, or set SABLE_SKIP_PRE_PUSH=1 to bypass.'
    }
}))
" FETCH_OUT="$FETCH_OUT"
  exit 0
}

# Step 2: check if rebase is needed
BEHIND=$(git -C "$CWD" rev-list --count "HEAD..$BASE_BRANCH" 2>/dev/null || echo "0")

if [ "$BEHIND" -gt 0 ]; then
  REBASE_OUT=$(git -C "$CWD" rebase "$BASE_BRANCH" 2>&1) || {
    git -C "$CWD" rebase --abort 2>/dev/null || true
    python3 -c "
import json, os
out = os.environ.get('REBASE_OUT', '')[:500]
bb = os.environ.get('BASE_BRANCH', '')
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': f'Pre-push: rebase on {bb} failed (and was aborted). Resolve conflicts manually, then retry push.\n{out}'
    }
}))
" REBASE_OUT="$REBASE_OUT" BASE_BRANCH="$BASE_BRANCH"
    exit 0
  }
fi

# Step 3: run tests
TEST_CMD="${SABLE_TEST_COMMAND:-}"

if [ -z "$TEST_CMD" ]; then
  # Auto-detect from project
  if [ -f "$CWD/package.json" ]; then
    TEST_CMD="npm test"
  elif [ -f "$CWD/pyproject.toml" ] || [ -f "$CWD/setup.py" ]; then
    TEST_CMD="pytest"
  elif [ -f "$CWD/Cargo.toml" ]; then
    TEST_CMD="cargo test"
  elif [ -f "$CWD/go.mod" ]; then
    TEST_CMD="go test ./..."
  fi
fi

if [ -z "$TEST_CMD" ]; then
  # No test command detected — warn but allow
  python3 -c "
import json
print(json.dumps({
    'additionalContext': 'Pre-push: rebase complete, but no test command detected (no package.json/pyproject.toml/Cargo.toml/go.mod). Set SABLE_TEST_COMMAND to enforce tests before push.'
}))
"
  exit 0
fi

# Run tests with a configurable timeout (default: 60s fast-subset budget).
# Pattern: pre-seed TEST_EXIT then assign via ||, so `set -e` doesn't fire on
# a nonzero test command.
TEST_TIMEOUT="${SABLE_PRE_PUSH_TEST_TIMEOUT:-60}"
TEST_EXIT=0
TEST_OUT=$(cd "$CWD" && timeout "$TEST_TIMEOUT" sh -c "$TEST_CMD" 2>&1) || TEST_EXIT=$?

if [ "$TEST_EXIT" -ne 0 ]; then
  # Distinguish timeout (exit 124 from `timeout`) from test failure
  if [ "$TEST_EXIT" -eq 124 ]; then
    REASON_SUFFIX="Tests exceeded SABLE_PRE_PUSH_TEST_TIMEOUT=${TEST_TIMEOUT}s. Either scope SABLE_TEST_COMMAND to a faster subset (recommended: smoke + changed units, <60s), or raise both SABLE_PRE_PUSH_TEST_TIMEOUT and the settings.json hook timeout together."
  else
    REASON_SUFFIX="Tests failed. Fix before pushing, or set SABLE_SKIP_PRE_PUSH=1 with explicit intent."
  fi
  python3 -c "
import json, os
out = os.environ.get('TEST_OUT', '')[-1500:]
cmd = os.environ.get('TEST_CMD', '')
suffix = os.environ.get('REASON_SUFFIX', '')
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': f'Pre-push: command failed ({cmd}).\n{suffix}\n\n{out}'
    }
}))
" TEST_OUT="$TEST_OUT" TEST_CMD="$TEST_CMD" REASON_SUFFIX="$REASON_SUFFIX"
  exit 0
fi

exit 0
