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
#   $SABLE_INTEGRATION_BRANCH           — bare name of the integration branch
#                                         (default: $SABLE_BASE_BRANCH minus origin/).
#                                         Pushing this branch retargets Phase-1
#                                         rebase to origin/<branch>, or skips it when
#                                         unpublished (fofc); while it is local-only
#                                         a re-parent guard also arms (yz5y).
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

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

# Identity via lib-identity.sh (SABLE-uz9.3 / SABLE-404): the gated phases fire
# for ANY manager identity — legacy env terminals (Chuck holdout), the Lincoln
# main session in execution mode, and manager-typed subagents (Optimus/Tarzan
# pushing their OWN lane from a nested subagent context; v3 moved push authority
# to the managers). Worker subagents are mechanically DENIED below (they return
# their stopped-before-push results to the manager, who reviews and pushes the
# lane); anonymous main sessions stand down.
# shellcheck source=lib-identity.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib-identity.sh"
sable_resolve_identity "$HOOK_INPUT"

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

PARSED=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
cmd = d.get('tool_input', {}).get('command', '')
cwd = d.get('cwd', '')
print(f'{cwd}\n{cmd}')
" 2>/dev/null) || exit 0

CWD=$(echo "$PARSED" | sed -n '1p')
COMMAND=$(echo "$PARSED" | sed -n '2,$p')

# --- v3 worker-push deny leg (SABLE-404, locked Gaudi decision; consolidates
# SABLE-myg). A subagent that is NOT a manager (a worker type, or an unnamed
# agent_id) must not push: workers return their stopped-before-push results to
# the manager that dispatched them, and the manager reviews and pushes the lane.
# This is the mechanical backstop for the prompt-level "workers don't push" rule.
# It fires ONLY on a real git push (non-push git commands are untouched) and
# BEFORE the manager path's --force skip below, so --force cannot bypass it.
# SABLE_WORKER_PUSH_OVERRIDE=1 authorizes an exceptional worker push (mirrors the
# tree-claim override shape). Error mode: identity resolution must have succeeded
# to reach here on a clean worker resolution — fail open only on a resolution
# crash, never on a clean worker identity.
if [ "$SABLE_ID_IS_SUBAGENT" -eq 1 ] && [ "$SABLE_ID_IS_MANAGER" -eq 0 ]; then
  if sable_is_git_push "$COMMAND"; then
    if [ "${SABLE_WORKER_PUSH_OVERRIDE:-}" = "1" ]; then
      emit_context "Pre-push: worker-identity push (${SABLE_ID_NAME:-unnamed}) ALLOWED via SABLE_WORKER_PUSH_OVERRIDE=1. Workers normally return results to their manager, who reviews and pushes the lane; this explicit override was recorded."
      exit 0
    fi
    # 73t4: if this is an UNREGISTERED instance (tarzan-2) whose base IS a
    # registered manager, the real fault is a missing spawn-time registration —
    # say so, instead of implying the manager is a worker. Diagnostic only: the
    # deny stands (privilege comes from the registry, never the name pattern).
    INSTANCE_HINT=""
    if _BASE=$(sable_instance_base_manager "$SABLE_ID_NAME" 2>/dev/null); then
      INSTANCE_HINT=" NOTE: '${SABLE_ID_NAME}' looks like an unregistered instance of the registered manager '${_BASE}'. Privilege is not granted by the name — the spawn/respawn tooling must register this instance in agents.yaml ('sable-spawn-manager --register-instance ${SABLE_ID_NAME}'); once its entry exists it resolves as a manager and this push is allowed without an override."
    fi
    emit_deny "Pre-push denied: worker subagents do not push. This identity (${SABLE_ID_NAME:-unnamed agent}) is a worker, not a registered manager. Return your results to the manager that dispatched you; the manager reviews your stopped-before-push result and pushes the lane itself (git -C <worktree> push). If you genuinely must push from this worker, set SABLE_WORKER_PUSH_OVERRIDE=1 in the hook environment.${INSTANCE_HINT}"
    exit 0
  fi
  # Worker identity, but not a git push — leave untouched.
  exit 0
fi

# Only manager identities reach the gated phases below.
[ "$SABLE_ID_IS_MANAGER" -eq 1 ] || exit 0

# Use shared matcher so 'git -C <path> push' and other flag-interleaved forms
# are matched correctly; also prevents false-positives when "git push" appears
# only as a quoted argument in another command (SABLE-0u1)
sable_is_git_push "$COMMAND" || exit 0
echo "$COMMAND" | grep -qE '(\-\-force|\-f\b)' && exit 0

# Resolve the effective repo dir from the push command's `git -C <path>`
# target, falling back to the shell cwd. Managers push worktrees via
# `git -C <worktree> push` from the main checkout, so the dir git operates in
# is the -C target, not the shell cwd — rebase/static/test must run THERE
# (SABLE-041).
CWD=$(sable_resolve_push_repo_dir "$CWD" "$COMMAND")

[ -z "$CWD" ] && exit 0
[ ! -d "$CWD/.git" ] && [ ! -f "$CWD/.git" ] && exit 0

# Validate base ref and fall back gracefully when SABLE_BASE_BRANCH points to
# a ref that doesn't exist in this repo (SABLE-61n)
BASE_BRANCH=$(sable_validate_base_ref "$CWD" "${SABLE_BASE_BRANCH:-origin/main}")

# ---------------------------------------------------------------------------
# Helpers  (emit_deny / emit_context are defined above, before the worker-deny
# leg that needs them)
# ---------------------------------------------------------------------------

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

# The branch being pushed and the configured integration branch (bare names).
# INTEGRATION_BRANCH: explicit $SABLE_INTEGRATION_BRANCH override, else derived
# from $SABLE_BASE_BRANCH by stripping a leading origin/ (matches
# tripwire-watcher's detect_integration_branch convention).
CURRENT_BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
_INT_REF="${SABLE_INTEGRATION_BRANCH:-${SABLE_BASE_BRANCH:-origin/main}}"
INTEGRATION_BRANCH="${_INT_REF#origin/}"

# --- yz5y (market-brief-package-yz5y): re-parent guard. Runs BEFORE any fetch/
# rebase and is active ONLY for a LOCAL-ONLY integration branch (a local
# refs/heads/<INT> with NO published origin/<INT>) — the exact window in which a
# worker that ran 'git pull --rebase origin master' would silently re-parent its
# branch off the integration lineage onto origin/master, stranding the local-only
# stack and splicing base-branch-only commits into the lineage. A correctly-based
# branch contains the integration HEAD as an ancestor; a re-parented one does not.
# Once origin/<INT> is published the structural fix (workers rebase against
# origin/<INT>) applies and this guard goes dormant.
if [ -n "$INTEGRATION_BRANCH" ] && [ -n "$CURRENT_BRANCH" ] \
   && [ "$CURRENT_BRANCH" != "$INTEGRATION_BRANCH" ] \
   && git -C "$CWD" rev-parse --verify --quiet "refs/heads/$INTEGRATION_BRANCH" >/dev/null 2>&1 \
   && ! git -C "$CWD" rev-parse --verify --quiet "origin/$INTEGRATION_BRANCH" >/dev/null 2>&1; then
  if ! git -C "$CWD" merge-base --is-ancestor "refs/heads/$INTEGRATION_BRANCH" HEAD 2>/dev/null; then
    emit_deny "Pre-push denied (re-parent guard): branch '$CURRENT_BRANCH' does not contain the local integration branch '$INTEGRATION_BRANCH' HEAD as an ancestor — it looks re-parented (e.g. a 'git pull --rebase origin master' replayed it onto a different base, stranding the local-only integration stack and risking out-of-scope commits in the lineage). Re-cut from '$INTEGRATION_BRANCH' and cherry-pick your work, then push. (Active only while '$INTEGRATION_BRANCH' is unpublished; publishing origin/$INTEGRATION_BRANCH retires this guard.)"
    exit 0
  fi
fi

FETCH_OUT=$(git -C "$CWD" fetch origin 2>&1) || {
  emit_deny "Pre-push phase 1 (rebase): git fetch failed:
${FETCH_OUT:0:300}
Resolve network/auth and retry. This phase cannot be skipped — rebase is mandatory."
  exit 0
}

# --- fofc (market-brief-package-fofc): integration-branch self-push special
# case. Pushing the branch that IS the integration branch must NEVER rebase it
# onto a DIFFERENT base (e.g. origin/dev) — that replays the whole local-only
# integration stack onto the base and can silently rewrite history (or abort on
# conflict and block the push). Retarget the mandatory rebase to the branch's OWN
# published tip origin/<branch> (a fast-forward-safe no-op that still catches a
# teammate's push to the same branch); if origin/<branch> does not exist yet,
# skip Phase 1 (nothing fast-forward-safe to rebase onto).
SKIP_REBASE=0
if [ -n "$CURRENT_BRANCH" ] && [ "$CURRENT_BRANCH" = "$INTEGRATION_BRANCH" ]; then
  if git -C "$CWD" rev-parse --verify --quiet "origin/$CURRENT_BRANCH" >/dev/null 2>&1; then
    BASE_BRANCH="origin/$CURRENT_BRANCH"
  else
    SKIP_REBASE=1
  fi
fi

if [ "$SKIP_REBASE" -eq 0 ]; then
  BEHIND=$(git -C "$CWD" rev-list --count "HEAD..$BASE_BRANCH" 2>/dev/null || echo "0")

  if [ "$BEHIND" -gt 0 ]; then
    REBASE_OUT=$(git -C "$CWD" rebase "$BASE_BRANCH" 2>&1) || {
      git -C "$CWD" rebase --abort 2>/dev/null || true
      emit_deny "Pre-push phase 1 (rebase): rebase on $BASE_BRANCH failed (and was aborted). Resolve conflicts manually, then retry push.
${REBASE_OUT:0:500}"
      exit 0
    }
  fi
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
