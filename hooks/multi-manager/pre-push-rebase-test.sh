#!/usr/bin/env bash
# pre-push-rebase-test.sh — Three-phase pre-push gate
# Trigger: PreToolUse:Bash matching `git push`
#
# Phases (in order):
#   1. REBASE   — always runs, never skippable. Fetch + rebase on $SABLE_BASE_BRANCH.
#   2. STATIC   — typecheck (and lint, if configured). Always runs, NEVER skippable.
#                 Auto-detected per project; if no typechecker found, phase no-ops.
#   3. TESTS    — runs if SABLE_PRE_PUSH_TEST_PHASE != "skip" and
#                 SABLE_SKIP_PRE_PUSH != "1". Bounded by SABLE_PRE_PUSH_TEST_TIMEOUT.
#
# **SABLE_SKIP_PRE_PUSH=1 only skips TESTS.** Rebase and static analysis still run.
# This is a deliberate weakening of the bypass to prevent typecheck regressions
# from sneaking through to CI. If you genuinely need to bypass everything (true
# emergency, e.g. CI infra outage), disable the hook entry in settings.json
# explicitly, or use `git push --force` which short-circuits this hook entirely.
#
# Configuration:
#   $SABLE_BASE_BRANCH                  — branch to rebase against (default: origin/main)
#   $SABLE_PRE_PUSH_TYPECHECK_COMMAND   — typecheck invocation (override auto-detect)
#   $SABLE_PRE_PUSH_LINT_COMMAND        — lint invocation (no auto-detect; opt-in)
#   $SABLE_PRE_PUSH_STATIC_TIMEOUT      — seconds for static phase (default: 90)
#   $SABLE_PRE_PUSH_TEST_PHASE          — "auto" (default) | "skip" (delegate to repo's git hooks)
#   $SABLE_TEST_COMMAND                 — test invocation (used when PHASE=auto)
#   $SABLE_PRE_PUSH_TEST_TIMEOUT        — seconds for test phase (default: 60)
#   $SABLE_SKIP_PRE_PUSH                — "1" to skip TESTS only (rebase+static still run)
#
# Auto-detect typechecker by project markers:
#   tsconfig.json    → npx tsc --noEmit
#   pyproject.toml   → mypy . (only if [tool.mypy] section present)
#   Cargo.toml       → cargo check
#   go.mod           → go vet ./...
#
# Lint is opt-in (no reliable auto-detect across linter ecosystems).

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

[ -n "$NESTED_AGENT_ID" ] && exit 0
echo "$COMMAND" | grep -qE '\bgit\s+push\b' || exit 0
echo "$COMMAND" | grep -qE '(\-\-force|\-f\b)' && exit 0

[ -z "$CWD" ] && exit 0
[ ! -d "$CWD/.git" ] && [ ! -f "$CWD/.git" ] && exit 0

BASE_BRANCH="${SABLE_BASE_BRANCH:-origin/main}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

emit_deny() {
  # $1 = reason text
  REASON="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': os.environ.get('REASON', '')
    }
}))
"
}

emit_context() {
  # $1 = additional context text
  CTX="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': os.environ.get('CTX', '')
    }
}))
"
}

# Auto-detect typecheck command from project markers.
# Echoes the command (or empty if no typechecker found).
detect_typecheck_cmd() {
  local cwd="$1"
  if [ -n "${SABLE_PRE_PUSH_TYPECHECK_COMMAND:-}" ]; then
    echo "$SABLE_PRE_PUSH_TYPECHECK_COMMAND"
    return
  fi
  if [ -f "$cwd/tsconfig.json" ]; then
    echo "npx --no-install tsc --noEmit"
    return
  fi
  if [ -f "$cwd/pyproject.toml" ]; then
    if grep -q '\[tool.mypy\]' "$cwd/pyproject.toml" 2>/dev/null; then
      echo "mypy ."
      return
    fi
  fi
  if [ -f "$cwd/Cargo.toml" ]; then
    echo "cargo check --all-targets"
    return
  fi
  if [ -f "$cwd/go.mod" ]; then
    echo "go vet ./..."
    return
  fi
  echo ""
}

# Auto-detect test command from project markers.
detect_test_cmd() {
  local cwd="$1"
  if [ -n "${SABLE_TEST_COMMAND:-}" ]; then
    echo "$SABLE_TEST_COMMAND"
    return
  fi
  if [ -f "$cwd/package.json" ]; then
    echo "npm test"
    return
  fi
  if [ -f "$cwd/pyproject.toml" ] || [ -f "$cwd/setup.py" ]; then
    echo "pytest"
    return
  fi
  if [ -f "$cwd/Cargo.toml" ]; then
    echo "cargo test"
    return
  fi
  if [ -f "$cwd/go.mod" ]; then
    echo "go test ./..."
    return
  fi
  echo ""
}

# ---------------------------------------------------------------------------
# Phase 1: REBASE (never skippable)
# ---------------------------------------------------------------------------

FETCH_OUT=$(git -C "$CWD" fetch origin 2>&1) || {
  emit_deny "Pre-push phase 1 (rebase): git fetch failed:
${FETCH_OUT:0:300}
Resolve network/auth and retry. This phase cannot be skipped — rebase is mandatory."
  exit 0
}

BEHIND=$(git -C "$CWD" rev-list --count "HEAD..$BASE_BRANCH" 2>/dev/null || echo "0")

if [ "$BEHIND" -gt 0 ]; then
  REBASE_OUT=$(git -C "$CWD" rebase "$BASE_BRANCH" 2>&1) || {
    git -C "$CWD" rebase --abort 2>/dev/null || true
    emit_deny "Pre-push phase 1 (rebase): rebase on $BASE_BRANCH failed (and was aborted). Resolve conflicts manually, then retry push.
${REBASE_OUT:0:500}"
    exit 0
  }
fi

# ---------------------------------------------------------------------------
# Phase 2: STATIC analysis (typecheck + optional lint) — never skippable
# ---------------------------------------------------------------------------

STATIC_TIMEOUT="${SABLE_PRE_PUSH_STATIC_TIMEOUT:-90}"

TYPECHECK_CMD=$(detect_typecheck_cmd "$CWD")

if [ -n "$TYPECHECK_CMD" ]; then
  TC_EXIT=0
  TC_OUT=$(cd "$CWD" && timeout "$STATIC_TIMEOUT" sh -c "$TYPECHECK_CMD" 2>&1) || TC_EXIT=$?

  if [ "$TC_EXIT" -ne 0 ]; then
    if [ "$TC_EXIT" -eq 124 ]; then
      SUFFIX="Typecheck exceeded SABLE_PRE_PUSH_STATIC_TIMEOUT=${STATIC_TIMEOUT}s. Either narrow the typecheck scope or raise the timeout."
    else
      SUFFIX="Typecheck reported errors. This phase CANNOT be skipped via SABLE_SKIP_PRE_PUSH — it is structurally required. Fix the type errors before pushing."
    fi
    emit_deny "Pre-push phase 2 (static): typecheck failed (\`$TYPECHECK_CMD\`).
${SUFFIX}

${TC_OUT: -1500}"
    exit 0
  fi
fi

# Lint phase: opt-in only, no auto-detect
LINT_CMD="${SABLE_PRE_PUSH_LINT_COMMAND:-}"

if [ -n "$LINT_CMD" ]; then
  LINT_EXIT=0
  LINT_OUT=$(cd "$CWD" && timeout "$STATIC_TIMEOUT" sh -c "$LINT_CMD" 2>&1) || LINT_EXIT=$?

  if [ "$LINT_EXIT" -ne 0 ]; then
    if [ "$LINT_EXIT" -eq 124 ]; then
      SUFFIX="Lint exceeded SABLE_PRE_PUSH_STATIC_TIMEOUT=${STATIC_TIMEOUT}s."
    else
      SUFFIX="Lint reported errors. This phase CANNOT be skipped via SABLE_SKIP_PRE_PUSH. Fix lint errors before pushing."
    fi
    emit_deny "Pre-push phase 2 (static): lint failed (\`$LINT_CMD\`).
${SUFFIX}

${LINT_OUT: -1500}"
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Phase 3: TESTS (skippable via SABLE_SKIP_PRE_PUSH=1 or PHASE=skip)
# ---------------------------------------------------------------------------

TEST_PHASE="${SABLE_PRE_PUSH_TEST_PHASE:-auto}"

if [ "$TEST_PHASE" = "skip" ]; then
  emit_context "Pre-push: rebase + static phases passed; test phase skipped (SABLE_PRE_PUSH_TEST_PHASE=skip). Repo git hooks handle test gating on the rebased state."
  exit 0
fi

if [ "${SABLE_SKIP_PRE_PUSH:-}" = "1" ]; then
  emit_context "Pre-push: rebase + static phases passed; test phase bypassed (SABLE_SKIP_PRE_PUSH=1). Note: typecheck/lint were still enforced — bypass is now scoped to the test phase only."
  exit 0
fi

TEST_CMD=$(detect_test_cmd "$CWD")

if [ -z "$TEST_CMD" ]; then
  emit_context "Pre-push: rebase + static phases passed; no test command detected (no package.json/pyproject.toml/Cargo.toml/go.mod). Set SABLE_TEST_COMMAND to enforce tests before push."
  exit 0
fi

TEST_TIMEOUT="${SABLE_PRE_PUSH_TEST_TIMEOUT:-60}"
TEST_EXIT=0
TEST_OUT=$(cd "$CWD" && timeout "$TEST_TIMEOUT" sh -c "$TEST_CMD" 2>&1) || TEST_EXIT=$?

if [ "$TEST_EXIT" -ne 0 ]; then
  if [ "$TEST_EXIT" -eq 124 ]; then
    SUFFIX="Tests exceeded SABLE_PRE_PUSH_TEST_TIMEOUT=${TEST_TIMEOUT}s. Either scope SABLE_TEST_COMMAND to a faster subset (recommended: smoke + changed units, <60s), or raise both SABLE_PRE_PUSH_TEST_TIMEOUT and the settings.json hook timeout together."
  else
    SUFFIX="Tests failed. Fix before pushing, or set SABLE_SKIP_PRE_PUSH=1 with explicit intent (rebase + static still run)."
  fi
  emit_deny "Pre-push phase 3 (tests): \`$TEST_CMD\` failed.
${SUFFIX}

${TEST_OUT: -1500}"
  exit 0
fi

exit 0
