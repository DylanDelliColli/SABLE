#!/usr/bin/env bash
# pre-push-rebase-test.sh — Four-phase pre-push gate
# Trigger: PreToolUse:Bash matching `git push`
#
# Phases (in order):
#   1. REBASE   — always runs, never skippable. Fetch + rebase on $SABLE_BASE_BRANCH.
#   2. STATIC   — typecheck (and lint, if configured), plus the fixture
#                 tripwire (bin/sable-fixture-tripwire, SABLE-digiy) when this
#                 push touches a hooks/test/*.sh or bin/test_*.py file and the
#                 target repo ships that checker. Always runs, NEVER skippable.
#                 Auto-detected per project; if no typechecker found, phase no-ops.
#   3. BUILD    — the project's build command, if one auto-detects (or is
#                 configured). Always runs, NEVER skippable. Catches build-only
#                 failures (e.g. Next.js page-export errors) that pass both
#                 typecheck and the test suite (SABLE-rzsb S4 / SABLE-h07t).
#   4. TESTS    — runs if SABLE_PRE_PUSH_TEST_PHASE != "skip" and
#                 SABLE_SKIP_PRE_PUSH != "1". Bounded by SABLE_PRE_PUSH_TEST_TIMEOUT.
#
# **SABLE_SKIP_PRE_PUSH=1 only skips TESTS.** Rebase, static analysis, and the
# build phase still run. This is a deliberate weakening of the bypass to
# prevent typecheck/build regressions from sneaking through to CI. If you
# genuinely need to bypass everything (true emergency, e.g. CI infra outage),
# disable the hook entry in settings.json explicitly, or use `git push --force`
# which short-circuits this hook entirely.
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
#   $SABLE_PRE_PUSH_BUILD_COMMAND       — build invocation (override auto-detect)
#   $SABLE_PRE_PUSH_BUILD_TIMEOUT       — seconds for build phase (default: 120)
#   $SABLE_PRE_PUSH_TEST_PHASE          — "auto" (default) | "skip" (delegate to repo's git hooks)
#   $SABLE_TEST_COMMAND                 — test invocation (used when PHASE=auto; lowest-priority
#                                         source — see sable_resolve_test_command below)
#   $SABLE_PRE_PUSH_TEST_TIMEOUT        — seconds for test phase (default: 60; lowest-priority
#                                         source — see sable_resolve_test_timeout below. Repo-local
#                                         override: `git config sable.testTimeout <seconds>`, or a
#                                         checked-in `testTimeout=<seconds>` line in .sable)
#   $SABLE_SKIP_PRE_PUSH                — "1" to skip TESTS only (rebase+static+build still run)
#
# Auto-detect typechecker/build by project markers. Manifest search (SABLE-rzsb
# S4) is not cwd-only: it walks UPWARD from $CWD to the repo root, and — if
# nothing is found upward — INTO the subdirs of the files this push actually
# changed (walking upward from each changed file's directory), so a
# monorepo/subdir-manifest worktree (e.g. a manifest at location-briefing/
# rather than the worktree root) is still detected (SABLE-h07t). See
# sable_find_manifest_dir below.
#   tsconfig.json    → npx tsc --noEmit
#   pyproject.toml   → mypy . (only if [tool.mypy] section present)
#   Cargo.toml       → cargo check
#   go.mod           → go vet ./...
#   package.json with a "build" script → npm run build
#
# Lint is opt-in (no reliable auto-detect across linter ecosystems).

set -euo pipefail

# shellcheck source=lib-identity.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib-identity.sh"

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

# ---------------------------------------------------------------------------
# Manifest search + auto-detect helpers (SABLE-rzsb S4 / SABLE-h07t).
#
# These are pure functions (no stdin/global-state reads beyond their
# arguments) so they can be unit-tested directly: source this file (the
# `[ "${BASH_SOURCE[0]}" = "${0}" ]` guard at the bottom keeps `main` from
# auto-running on source) and call them with fixture directories.
# ---------------------------------------------------------------------------

# sable_find_manifest_dir <cwd> <base_branch> <manifest1> [manifest2 ...]
#
# Locates the nearest project manifest. The old check was `[ -f
# "$cwd/<manifest>" ]` only — a no-op in any monorepo/subdir-manifest
# worktree (h07t: a worktree root with no manifest but a subdir like
# location-briefing/ holding pyproject.toml). Search order:
#   1. UPWARD from <cwd> to the repo root (inclusive) — covers a cwd nested
#      below the manifest.
#   2. INTO SUBDIRS reachable from the files this push changed
#      (`git diff --name-only <base_branch>...HEAD`), walking upward from
#      each changed file's directory to the repo root — covers the monorepo
#      case where <cwd> IS the repo root but the manifest lives under a
#      package subdir the push actually touched. Skipped when <base_branch>
#      is empty (nothing to diff against).
# Prints the absolute directory containing the nearest manifest (nothing if
# not found either way). Always returns 0 — never fails the caller under
# `set -e`; callers test `[ -n "$dir" ]`.
sable_find_manifest_dir() {
  local cwd="$1" base_branch="$2"
  shift 2
  local manifests=("$@")
  local repo_root
  repo_root=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null) || repo_root="$cwd"

  local dir="$cwd" m
  while :; do
    for m in "${manifests[@]}"; do
      if [ -f "$dir/$m" ]; then
        printf '%s' "$dir"
        return 0
      fi
    done
    if [ "$dir" = "$repo_root" ] || [ "$dir" = "/" ] || [ -z "$dir" ]; then
      break
    fi
    dir=$(dirname "$dir")
  done

  if [ -n "$base_branch" ]; then
    local changed_file
    while IFS= read -r changed_file; do
      [ -z "$changed_file" ] && continue
      dir="$repo_root/$(dirname "$changed_file")"
      while :; do
        for m in "${manifests[@]}"; do
          if [ -f "$dir/$m" ]; then
            printf '%s' "$dir"
            return 0
          fi
        done
        if [ "$dir" = "$repo_root" ] || [ "$dir" = "/" ] || [ -z "$dir" ]; then
          break
        fi
        dir=$(dirname "$dir")
      done
    done < <(git -C "$cwd" diff --name-only "${base_branch}...HEAD" 2>/dev/null || true)
  fi

  return 0
}

# sable_cmd_in_dir <cwd> <resolved-dir> <cmd>
#
# Wraps <cmd> to `cd` into <resolved-dir> first when it differs from <cwd> —
# sable_find_manifest_dir can resolve a manifest upward or into a
# changed-file's subdir, and the command must run there, not at the phase's
# own $CWD.
sable_cmd_in_dir() {
  local cwd="$1" dir="$2" cmd="$3"
  if [ "$dir" = "$cwd" ]; then
    printf '%s\n' "$cmd"
  else
    printf 'cd %q && %s\n' "$dir" "$cmd"
  fi
}

# sable_tail_chars <text> <n>
#
# Prints the last <n> characters of <text>. Bash's own negative-offset
# substring expansion (`${VAR: -N}`) returns EMPTY — not the full value —
# whenever <text> is SHORTER than N (confirmed bash 5.2.21: `X=short; echo
# ${X: -1500}` prints nothing), which silently dropped the actual command
# output from every deny message whose failure was under 1500 chars, the
# common case (SABLE-5y1en). `tail -c` has no such short-input special case.
sable_tail_chars() {
  printf '%s' "$1" | tail -c "$2"
}

# Auto-detect typecheck command from project markers.
# Echoes the command (or empty if no typechecker found).
detect_typecheck_cmd() {
  local cwd="$1" base_branch="${2:-}"
  if [ -n "${SABLE_PRE_PUSH_TYPECHECK_COMMAND:-}" ]; then
    echo "$SABLE_PRE_PUSH_TYPECHECK_COMMAND"
    return
  fi

  local dir
  dir=$(sable_find_manifest_dir "$cwd" "$base_branch" tsconfig.json)
  if [ -n "$dir" ]; then
    sable_cmd_in_dir "$cwd" "$dir" "npx --no-install tsc --noEmit"
    return
  fi

  dir=$(sable_find_manifest_dir "$cwd" "$base_branch" pyproject.toml)
  if [ -n "$dir" ] && grep -q '\[tool.mypy\]' "$dir/pyproject.toml" 2>/dev/null; then
    sable_cmd_in_dir "$cwd" "$dir" "mypy ."
    return
  fi

  dir=$(sable_find_manifest_dir "$cwd" "$base_branch" Cargo.toml)
  if [ -n "$dir" ]; then
    sable_cmd_in_dir "$cwd" "$dir" "cargo check --all-targets"
    return
  fi

  dir=$(sable_find_manifest_dir "$cwd" "$base_branch" go.mod)
  if [ -n "$dir" ]; then
    sable_cmd_in_dir "$cwd" "$dir" "go vet ./..."
    return
  fi

  echo ""
}

# Resolve the test command: repo-local config / checked-in .sable / env
# (sable_resolve_test_command, SABLE-hml), else the repo's CI-tier SSOT
# pre_push tier if it declares one (SABLE-cmar4.1 — .github/ci/test-tiers.sh,
# consumption seam only: this repo's own testCommand= now reads THAT tier
# rather than hardcoding the suite list, and any other repo that adopts the
# tier SSOT gets this fallback for free without configuring testCommand at
# all), else auto-detect from project markers (manifest search widened to
# upward+subdir, SABLE-rzsb S4), else empty (no test command — phase 4
# no-ops with a message).
detect_test_cmd() {
  local cwd="$1" base_branch="${2:-}"
  local resolved
  resolved=$(sable_resolve_test_command "$cwd")
  if [ -n "$resolved" ]; then
    echo "$resolved"
    return
  fi
  if [ -f "$cwd/.github/ci/test-tiers.sh" ]; then
    echo "bash .github/ci/test-tiers.sh --run pre_push"
    return
  fi

  local dir
  dir=$(sable_find_manifest_dir "$cwd" "$base_branch" package.json)
  if [ -n "$dir" ]; then
    sable_cmd_in_dir "$cwd" "$dir" "npm test"
    return
  fi

  dir=$(sable_find_manifest_dir "$cwd" "$base_branch" pyproject.toml setup.py)
  if [ -n "$dir" ]; then
    sable_cmd_in_dir "$cwd" "$dir" "pytest"
    return
  fi

  dir=$(sable_find_manifest_dir "$cwd" "$base_branch" Cargo.toml)
  if [ -n "$dir" ]; then
    sable_cmd_in_dir "$cwd" "$dir" "cargo test"
    return
  fi

  dir=$(sable_find_manifest_dir "$cwd" "$base_branch" go.mod)
  if [ -n "$dir" ]; then
    sable_cmd_in_dir "$cwd" "$dir" "go test ./..."
    return
  fi

  echo ""
}

# Auto-detect build command from project markers (SABLE-rzsb S4 / h07t
# addendum: Next.js App Router page-export errors pass tsc+vitest but fail
# the real build, and land red on the integration branch). Echoes the
# command (or empty if no build command found — the phase no-ops).
detect_build_cmd() {
  local cwd="$1" base_branch="${2:-}"
  if [ -n "${SABLE_PRE_PUSH_BUILD_COMMAND:-}" ]; then
    echo "$SABLE_PRE_PUSH_BUILD_COMMAND"
    return
  fi

  local dir
  dir=$(sable_find_manifest_dir "$cwd" "$base_branch" package.json)
  if [ -n "$dir" ] && grep -qE '"build"[[:space:]]*:' "$dir/package.json" 2>/dev/null; then
    sable_cmd_in_dir "$cwd" "$dir" "npm run build"
    return
  fi

  echo ""
}

main() {
HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

# Identity via lib-identity.sh (SABLE-uz9.3 / SABLE-404): the gated phases fire
# for ANY manager identity — legacy env terminals (Chuck holdout), the Lincoln
# main session in execution mode, and manager-typed subagents (Optimus/Tarzan
# pushing their OWN lane from a nested subagent context; v3 moved push authority
# to the managers). Worker subagents are mechanically DENIED below (they return
# their stopped-before-push results to the manager, who reviews and pushes the
# lane); anonymous main sessions stand down.
sable_resolve_identity "$HOOK_INPUT"

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

# BASE_BRANCH is resolved below, AFTER the integration branch is known
# (SABLE-4amz) — the old unconditional origin/main default here re-parented
# worker branches on non-main integration repos.

# ---------------------------------------------------------------------------
# Phase 1: REBASE (never skippable)
# ---------------------------------------------------------------------------

# The branch being pushed and the configured integration branch (bare names).
# INTEGRATION_BRANCH: explicit $SABLE_INTEGRATION_BRANCH override, else derived
# from $SABLE_BASE_BRANCH by stripping a leading origin/ (matches
# tripwire-watcher's detect_integration_branch convention).
CURRENT_BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
# market-brief-package-2u25: resolved PER REPO (repo-local git config /
# .sable file wins over session env) — a session's SABLE_BASE_BRANCH is
# configured once per project and otherwise leaks unchanged into every other
# repo that session's manager ever pushes (e.g. a companion SABLE-repo
# worktree pushed from a market-brief-package session).
INTEGRATION_BRANCH=$(sable_resolve_integration_branch "$CWD")

# --- yz5y (market-brief-package-yz5y): re-parent guard. Runs BEFORE any fetch/
# rebase and is active ONLY for a LOCAL-ONLY integration branch (a local
# refs/heads/<INT> with NO published origin/<INT>) — the exact window in which a
# worker that ran 'git pull --rebase origin master' would silently re-parent its
# branch off the integration lineage onto origin/master, stranding the local-only
# stack and splicing base-branch-only commits into the lineage. A correctly-based
# branch contains the integration HEAD as an ancestor; a re-parented one does not.
# Once origin/<INT> is published the structural fix (workers rebase against
# origin/<INT>) applies and this guard goes dormant — the published case is
# covered by the SABLE-4amz wrong-base guard below.
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

# --- SABLE-1238 (supersedes SABLE-4amz's inline derivation): resolve the
# phase-1 rebase base AUTHORITATIVELY from the target repo's own integration
# branch, never from the session env. When origin/<INT> is published that ref
# IS the base and a leaked session SABLE_BASE_BRANCH (or an origin/main
# fallback) cannot override it — sable_resolve_base_branch encodes the full
# rationale, including why a PreToolUse hook can never read SABLE_BASE_BRANCH
# from the push invocation. Resolved after the fetch so the origin/<INT>
# existence check sees fresh remote refs. The old inline
# `${SABLE_BASE_BRANCH:-$DEFAULT_BASE_BRANCH}` let the leaked env win, forcing
# origin/main and a wrong-base deny whose remediation was unreachable.
BASE_BRANCH=$(sable_resolve_base_branch "$CWD")

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

# --- SABLE-4amz / SABLE-1238: published-case wrong-base guard. The yz5y guard
# above covers only a LOCAL-ONLY integration branch; once origin/<INT> is
# published, phase 1 rebasing a worker branch onto any base other than
# origin/<INT> replays it onto foreign lineage and rewrites every carried SHA.
# With SABLE-1238 the base is now resolved authoritatively from repo config
# (sable_resolve_base_branch), so in the published case BASE_BRANCH already IS
# origin/<INT> and this guard is a defensive assertion that should not fire.
# Its remediation intentionally does NOT tell the operator to change
# SABLE_BASE_BRANCH: this is a PreToolUse hook running in the session env, so a
# SABLE_BASE_BRANCH prefixed onto the `git push` invocation is never read here
# (the exact dead end that made SABLE-4amz's deny impossible to satisfy). The
# only authoritative fix is the target repo's integration-branch config.
if [ -n "$INTEGRATION_BRANCH" ] && [ -n "$CURRENT_BRANCH" ] \
   && [ "$CURRENT_BRANCH" != "$INTEGRATION_BRANCH" ] \
   && git -C "$CWD" rev-parse --verify --quiet "origin/$INTEGRATION_BRANCH" >/dev/null 2>&1 \
   && [ "$BASE_BRANCH" != "origin/$INTEGRATION_BRANCH" ]; then
  emit_deny "Pre-push denied (wrong-base guard): phase 1 would rebase worker branch '$CURRENT_BRANCH' onto '$BASE_BRANCH', but this repo's integration branch is published at origin/$INTEGRATION_BRANCH. Rebasing onto a foreign base re-parents the branch and rewrites its carried SHAs (this corrupted wk-tripwire-pytest — SABLE-4amz). The authoritative base is this repo's integration branch, set via 'git config sable.integrationBranch <name>' or an 'integrationBranch=<name>' line in the repo's .sable file — correct that in the TARGET repo. NOTE: this pre-push gate is a PreToolUse hook and runs in the session environment, NOT the environment of your 'git push' command, so prefixing SABLE_BASE_BRANCH onto the push (or 'env -u SABLE_BASE_BRANCH git push') has no effect here."
  exit 0
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

TYPECHECK_CMD=$(detect_typecheck_cmd "$CWD" "$BASE_BRANCH")

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

$(sable_tail_chars "$TC_OUT" 1500)"
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

$(sable_tail_chars "$LINT_OUT" 1500)"
    exit 0
  fi
fi

# --- Fixture tripwire (SABLE-digiy, part of phase 2 STATIC, never skippable
# when it fires): bin/sable-fixture-tripwire (SABLE-0ssz.2) bans unsandboxed
# real-repo git ops in test fixtures — a bare, unguarded `cd` in a fixture
# setup, or a mutating git/dolt op scoped to the real repo root. It ran only
# at ci-verify (remote) until now: a violating test-file push passed the local
# shell suite AND local unit tests, then RED'd at the seat, wasting a full
# ci-verify cycle before the author learned (proven on jd5fj.14's 3c planted-
# poison control — local suite 22/22 green, caught only remotely). Gated on
# two conditions so it costs nothing outside its actual scope: (a) the target
# repo ships the checker at all (most repos this hook gates don't — it
# silently no-ops there), and (b) the diff this push carries actually touches
# a hooks/test/*.sh or bin/test_*.py file (a push touching neither pays no
# added cost — the acceptance criterion this bead is scoped to).
TRIPWIRE_BIN="$CWD/bin/sable-fixture-tripwire"

if [ -x "$TRIPWIRE_BIN" ] && [ -n "$BASE_BRANCH" ]; then
  TRIPWIRE_TARGETS=()
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      hooks/test/*.sh|bin/test_*.py) TRIPWIRE_TARGETS+=("$f") ;;
    esac
  done < <(git -C "$CWD" diff --name-only --diff-filter=ACMR "${BASE_BRANCH}...HEAD" 2>/dev/null || true)

  if [ ${#TRIPWIRE_TARGETS[@]} -gt 0 ]; then
    TW_EXIT=0
    TW_OUT=$(cd "$CWD" && timeout "$STATIC_TIMEOUT" "$TRIPWIRE_BIN" "${TRIPWIRE_TARGETS[@]}" 2>&1) || TW_EXIT=$?

    if [ "$TW_EXIT" -ne 0 ]; then
      emit_deny "Pre-push phase 2 (static): fixture-tripwire failed (\`bin/sable-fixture-tripwire ${TRIPWIRE_TARGETS[*]}\`).
This phase CANNOT be skipped via SABLE_SKIP_PRE_PUSH — it is structurally required (SABLE-0ssz.2: an unsandboxed fixture cd / real-repo git op that reaches ci-verify has already cost a full RED cycle — SABLE-digiy/jd5fj.14). Fix the exact file:line below (guard the cd, or scope the git op to a fixture dir), or add a KNOWN_VIOLATIONS entry with its tracking bead if this is a reviewed exception.

$(sable_tail_chars "$TW_OUT" 1500)"
      exit 0
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Phase 3: BUILD (never skippable) — SABLE-rzsb S4 / SABLE-h07t addendum.
# Catches build-only failures (e.g. Next.js page-export errors) that pass
# both typecheck and the test suite, and otherwise land red on the
# integration branch.
# ---------------------------------------------------------------------------

BUILD_TIMEOUT="${SABLE_PRE_PUSH_BUILD_TIMEOUT:-120}"

BUILD_CMD=$(detect_build_cmd "$CWD" "$BASE_BRANCH")

if [ -n "$BUILD_CMD" ]; then
  BUILD_EXIT=0
  BUILD_OUT=$(cd "$CWD" && timeout "$BUILD_TIMEOUT" sh -c "$BUILD_CMD" 2>&1) || BUILD_EXIT=$?

  if [ "$BUILD_EXIT" -ne 0 ]; then
    if [ "$BUILD_EXIT" -eq 124 ]; then
      SUFFIX="Build exceeded SABLE_PRE_PUSH_BUILD_TIMEOUT=${BUILD_TIMEOUT}s. Either narrow the build scope or raise the timeout."
    else
      SUFFIX="Build failed. This phase CANNOT be skipped via SABLE_SKIP_PRE_PUSH — it is structurally required (build-only errors, e.g. Next.js page-export errors, pass typecheck and the test suite but fail the real build)."
    fi
    emit_deny "Pre-push phase 3 (build): build failed (\`$BUILD_CMD\`).
${SUFFIX}

$(sable_tail_chars "$BUILD_OUT" 1500)"
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Phase 4: TESTS (skippable via SABLE_SKIP_PRE_PUSH=1 or PHASE=skip)
# ---------------------------------------------------------------------------

TEST_PHASE="${SABLE_PRE_PUSH_TEST_PHASE:-auto}"

if [ "$TEST_PHASE" = "skip" ]; then
  emit_context "Pre-push: rebase + static + build phases passed; test phase skipped (SABLE_PRE_PUSH_TEST_PHASE=skip). Repo git hooks handle test gating on the rebased state."
  exit 0
fi

if [ "${SABLE_SKIP_PRE_PUSH:-}" = "1" ]; then
  emit_context "Pre-push: rebase + static + build phases passed; test phase bypassed (SABLE_SKIP_PRE_PUSH=1). Note: typecheck/lint/build were still enforced — bypass is now scoped to the test phase only."
  exit 0
fi

TEST_CMD=$(detect_test_cmd "$CWD" "$BASE_BRANCH")

if [ -z "$TEST_CMD" ]; then
  emit_context "Pre-push: rebase + static + build phases passed; no test command detected (no package.json/pyproject.toml/Cargo.toml/go.mod found upward or in any changed-file subdir). Add a testCommand= line to .sable (checked in), or set sable.testCommand via git config, or set SABLE_TEST_COMMAND, to enforce tests before push."
  exit 0
fi

TEST_TIMEOUT=$(sable_resolve_test_timeout "$CWD")
TEST_EXIT=0
TEST_OUT=$(cd "$CWD" && timeout "$TEST_TIMEOUT" sh -c "$TEST_CMD" 2>&1) || TEST_EXIT=$?

if [ "$TEST_EXIT" -ne 0 ]; then
  if [ "$TEST_EXIT" -eq 124 ]; then
    SUFFIX="Tests exceeded the ${TEST_TIMEOUT}s test-phase timeout. Either scope the test command to a faster subset (recommended: smoke + changed units, <60s), or raise the timeout for this repo: \`git config sable.testTimeout <seconds>\` (repo-local), a \`testTimeout=<seconds>\` line in .sable (checked in), or \$SABLE_PRE_PUSH_TEST_TIMEOUT (legacy env, and raise the settings.json hook timeout together with it)."
  else
    SUFFIX="Tests failed. Fix before pushing, or set SABLE_SKIP_PRE_PUSH=1 with explicit intent (rebase + static + build still run)."
  fi
  emit_deny "Pre-push phase 4 (tests): \`$TEST_CMD\` failed.
${SUFFIX}

$(sable_tail_chars "$TEST_OUT" 1500)"
  exit 0
fi

exit 0
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main
fi
