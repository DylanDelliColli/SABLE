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

# sable_parse_push_update <command>
#
# Classifies a git-push invocation for the exact-object gate (SABLE-j90ba).
# Pure function (no git/global reads) so it unit-tests by sourcing this file.
# Output, single line: kind|remote|src|dst|detail
#   kind = update       exactly one non-delete branch update (src/dst empty
#                       for the bare form — the caller resolves them against
#                       the current branch)
#   kind = delete       deletion-only push (no new object)
#   kind = unsupported  everything else; detail says why. FAIL CLOSED: a
#                       command this parser cannot prove to be a single
#                       update must not be certified as one — that includes
#                       compound shell commands (certifying only the first
#                       push of `a && b` certifies the wrong thing) and any
#                       unrecognized flag.
sable_parse_push_update() {
  local command="$1"
  case "$command" in
    *'&&'*|*'||'*|*';'*|*'|'*|*$'\n'*|*'$('*|*'`'*|*'>'*|*'<'*)
      printf 'unsupported||||compound command'
      return 0
      ;;
  esac
  # B4: the hook certifies the PRE-expansion text while Bash executes the
  # POST-expansion command. A branch may be literally named '{main,side}' —
  # the parser resolves one current branch, the shell expands to TWO
  # refspecs. Dollar refs and glob/tilde forms diverge the same way. Every
  # expansion-capable character fails closed; the pinned forms are literal.
  case "$command" in
    *'{'*|*'}'*|*'$'*|*'*'*|*'?'*|*'['*|*']'*|*'~'*)
      printf 'unsupported||||shell expansion syntax'
      return 0
      ;;
  esac
  local -a toks=()
  read -r -a toks <<< "$command"
  local i=0 n=${#toks[@]} tok
  # Leading environment assignments are REJECTED, not skipped (B3): the hook
  # validates git state WITHOUT these command-local assignments while the
  # actual push runs WITH them — GIT_DIR=/other/.git redirects the repo and
  # GIT_CONFIG_COUNT/KEY/VALUE can inject push.followTags for the real
  # command only. No env-prefixed form is in the pinned surface; broad
  # rejection fails closed.
  if [ "$n" -gt 0 ]; then
    case "${toks[0]}" in
      [A-Za-z_]*=*)
        printf 'unsupported||||leading environment assignment %s' "${toks[0]%%=*}"
        return 0
        ;;
    esac
  fi
  if [ "$i" -ge "$n" ] || [ "${toks[$i]}" != "git" ]; then
    printf 'unsupported||||unrecognized invocation'
    return 0
  fi
  i=$((i+1))
  # Pre-subcommand git-global flags: FAIL CLOSED on anything that can change
  # WHICH repo/config the eventual push acts on. -C is accepted because
  # sable_resolve_push_repo_dir resolves the gate's own repo from the same
  # flag (the validated repo IS the -C target); --no-pager is output-only.
  # Everything else is rejected: --git-dir/--work-tree/--bare redirect the
  # repo while the gate validates $CWD, and -c can inject
  # remote.<name>.push/push.default at push time, invisible to the gate's
  # repo-config soundness checks.
  while [ "$i" -lt "$n" ] && [ "${toks[$i]}" != "push" ]; do
    case "${toks[$i]}" in
      -C) i=$((i+2)) ;;
      --no-pager) i=$((i+1)) ;;
      -*)
        printf 'unsupported||||global flag %s' "${toks[$i]%%=*}"
        return 0
        ;;
      *)
        printf 'unsupported||||unrecognized invocation'
        return 0
        ;;
    esac
  done
  if [ "$i" -ge "$n" ] || [ "${toks[$i]}" != "push" ]; then
    printf 'unsupported||||unrecognized invocation'
    return 0
  fi
  i=$((i+1))

  local delete_flag=0 remote=""
  local -a refspecs=()
  while [ "$i" -lt "$n" ]; do
    tok="${toks[$i]}"
    case "$tok" in
      -u|--set-upstream|-q|--quiet|-v|--verbose|--porcelain|--progress|--no-progress|--no-verify) ;;
      -d|--delete) delete_flag=1 ;;
      -*)
        printf 'unsupported||||flag %s' "$tok"
        return 0
        ;;
      *)
        if [ -z "$remote" ] && [ ${#refspecs[@]} -eq 0 ]; then
          remote="$tok"
        else
          refspecs+=("$tok")
        fi
        ;;
    esac
    i=$((i+1))
  done

  local count=${#refspecs[@]} spec src dst
  if [ "$count" -gt 1 ]; then
    local has_delete=0 has_update=0
    for spec in "${refspecs[@]}"; do
      case "$spec" in
        :*) has_delete=1 ;;
        *) has_update=1 ;;
      esac
    done
    if [ "$has_delete" -eq 1 ] && [ "$has_update" -eq 1 ]; then
      printf 'unsupported||||mixed delete and update'
    else
      printf 'unsupported||||multiple refspecs'
    fi
    return 0
  fi
  if [ "$count" -eq 0 ]; then
    if [ "$delete_flag" -eq 1 ]; then
      printf 'unsupported||||delete with no refspec'
    else
      printf 'update|%s|||' "$remote"
    fi
    return 0
  fi
  spec="${refspecs[0]}"
  case "$spec" in
    +*)
      printf 'unsupported||||forced refspec'
      return 0
      ;;
  esac
  if [ "$delete_flag" -eq 1 ]; then
    case "$spec" in
      *:*)
        printf 'unsupported||||mixed delete and update'
        return 0
        ;;
    esac
    printf 'delete|%s||%s|' "$remote" "${spec#refs/heads/}"
    return 0
  fi
  case "$spec" in
    :*)
      # B1: a BARE colon is git's matching-push (every matching branch —
      # reproduced moving a remote branch), NOT a deletion. The empty-source
      # deletion carve-out is pinned to ':refs/heads/<nonempty>' only; every
      # other colon form fails closed ('--delete origin <branch>' covers
      # bare-name deletion).
      dst="${spec#:}"
      case "$dst" in
        refs/heads/?*)
          printf 'delete|%s||%s|' "$remote" "${dst#refs/heads/}"
          ;;
        *)
          printf 'unsupported||||colon refspec outside the pinned deletion form'
          ;;
      esac
      ;;
    *:*)
      src="${spec%%:*}"
      dst="${spec#*:}"
      printf 'update|%s|%s|%s|' "$remote" "$src" "${dst#refs/heads/}"
      ;;
    *)
      printf 'update|%s|%s|%s|' "$remote" "${spec}" "${spec#refs/heads/}"
      ;;
  esac
  return 0
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
# Read to EOF with Bash's builtin `read`, then reproduce command substitution's
# trailing-newline normalization. This avoids spawning `cat` for every hook
# observation; /dev/stdin cannot be slurped from a command substitution here
# because `main` is a function and Bash gives that substitution an empty stdin.
HOOK_INPUT=""
IFS= read -r -d '' HOOK_INPUT || true
while [[ "$HOOK_INPUT" == *$'\n' ]]; do
  HOOK_INPUT="${HOOK_INPUT%$'\n'}"
done

# Identity via lib-identity.sh (SABLE-uz9.3 / SABLE-404): the gated phases fire
# for ANY manager identity — legacy env terminals (Chuck holdout), the Lincoln
# main session in execution mode, and manager-typed subagents (Optimus/Tarzan
# pushing their OWN lane from a nested subagent context; v3 moved push authority
# to the managers). Worker subagents are mechanically DENIED below (they return
# their stopped-before-push results to the manager, who reviews and pushes the
# lane); anonymous main sessions stand down.
sable_resolve_identity "$HOOK_INPUT"

PRE_PUSH_FIELDS=()
mapfile -d '' -t PRE_PUSH_FIELDS < <(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
cmd = d.get('tool_input', {}).get('command', '')
cwd = d.get('cwd', '')
# Preserve the former print -> command-substitution -> sed field semantics,
# including multiline commands, a newline-bearing cwd, and stripped trailing
# newlines. NUL delimiters then let Bash consume both fields without two sed
# processes at this per-hook boundary (SABLE-y4nom.7.6).
legacy = f'{cwd}\n{cmd}'.rstrip('\n')
if '\n' in legacy:
    cwd_field, command_field = legacy.split('\n', 1)
else:
    cwd_field, command_field = legacy, ''
sys.stdout.write(cwd_field + '\0' + command_field + '\0')
" 2>/dev/null)
[ "${#PRE_PUSH_FIELDS[@]}" -eq 2 ] || exit 0
CWD="${PRE_PUSH_FIELDS[0]}"
COMMAND="${PRE_PUSH_FIELDS[1]}"

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

# ---------------------------------------------------------------------------
# SABLE-j90ba exact-object gate, part A0: classify BEFORE repo resolution.
# Ordering is load-bearing: sable_resolve_push_repo_dir selects the FIRST
# `git -C <path>` in the command, so a compound like
# `git -C /tmp status && git -C <repo> push ...` used to resolve /tmp, hit
# the non-repo early exit below, and let the trailing push run UNGATED
# (reproduced 2026-08-01). Unsupported syntax must fail closed before any
# repo/dir logic gets a chance to exit quietly; only a supported SINGLE push
# proceeds to repo resolution.
# ---------------------------------------------------------------------------
GATE_ACTIVE=0
GATE_PARSE=$(sable_parse_push_update "$COMMAND")
GATE_KIND="${GATE_PARSE%%|*}"
GATE_REST="${GATE_PARSE#*|}"
GATE_REMOTE="${GATE_REST%%|*}"
GATE_REST="${GATE_REST#*|}"
GATE_SRC="${GATE_REST%%|*}"
GATE_REST="${GATE_REST#*|}"
GATE_DST="${GATE_REST%%|*}"
GATE_DETAIL="${GATE_PARSE##*|}"

if [ "$GATE_KIND" = "unsupported" ]; then
  emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): this invocation is not certifiable as exactly one branch update ($GATE_DETAIL). The gate validates the exact object being pushed, so the initial surface accepts exactly one supported non-delete refspec update per command. Accepted forms: 'git push'; 'git push origin <current-branch>'; 'git push -u origin <current-branch>'; 'git push origin HEAD:refs/heads/<current-branch>'. Deletion-only cleanup: 'git push --delete origin <branch>' or 'git push origin :refs/heads/<branch>'. Split compound commands into one push per Bash command; --all/--mirror/--tags/multiple refspecs and unrecognized flags fail closed rather than being certified by proxy."
  exit 0
fi

if [ "$GATE_KIND" = "delete" ]; then
  emit_context "Pre-push: deletion-only push of '${GATE_DST}' — no new object is published, so object validation not claimed and the rebase/static/build/test phases do not run (SABLE-j90ba deletion carve-out; remote branch cleanup is a legitimate session-close operation)."
  exit 0
fi

# Resolve the effective repo dir from the push command's `git -C <path>`
# target, falling back to the shell cwd. Managers push worktrees via
# `git -C <worktree> push` from the main checkout, so the dir git operates in
# is the -C target, not the shell cwd — rebase/static/test must run THERE
# (SABLE-041). Safe to run only now: part A0 guaranteed this is a single
# supported push, so the first -C IS the push's -C.
CWD=$(sable_resolve_push_repo_dir "$CWD" "$COMMAND")

# B5: normalize to the work-tree TOPLEVEL exactly as git will. Git searches
# parent directories, so a push from repo/subdir (ambient cwd or -C target)
# really pushes the repo — the old `$CWD/.git` existence check quiet-exited
# there and the push ran UNGATED. Resolution failure (non-repo, bare repo,
# empty cwd) DENIES rather than exiting silently: for this known-push
# command, an unresolvable work tree means the gate cannot attribute a
# verdict, not that there is nothing to gate.
if [ -n "$CWD" ] && GATE_TOPLEVEL=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) \
   && [ -n "$GATE_TOPLEVEL" ]; then
  CWD="$GATE_TOPLEVEL"
else
  emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): could not resolve a work tree for this push (directory '${CWD:-<empty>}' is not inside a non-bare git work tree). A real 'git push' MAY still resolve a parent or bare repository by its own search rules, so the gate fails closed instead of exiting silently. Run the push from inside the repository work tree (or 'git -C <worktree> push')."
  exit 0
fi

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

# ---------------------------------------------------------------------------
# SABLE-j90ba exact-object gate, part A: resolve the pushed source object and
# validate remote/destination BEFORE any mutation. UNSCOPED — applies to
# every manager push traversing this hook (planning-pass ruling 2026-08-01;
# explicit-integration scoping belongs only to the provenance guard). The
# defect this closes: the gate used to validate the CHECKED-OUT tree, so a
# push-by-ref from a shared checkout, or a dirty/untracked overlay, produced
# a green attributed to an object the phases never touched. Classification
# itself already happened in part A0, before repo resolution.
# ---------------------------------------------------------------------------
GATE_SRC_REF="${GATE_SRC:-HEAD}"

# Remote rule. The gate's rebase, provenance, and base-object attribution are
# all computed against 'origin' (the fetch below, BASE_BRANCH=origin/<int>,
# the ci-verify provenance refs), so the initial surface requires that the
# branch's EFFECTIVE push remote (branch.<name>.pushRemote >
# remote.pushDefault > branch.<name>.remote > origin) IS origin, and that any
# explicit remote token names it. Certifying a push to another remote would
# attribute the verdict to lineage this gate never examined.
GATE_EFFECTIVE_REMOTE=$(git -C "$CWD" config --get "branch.${CURRENT_BRANCH}.pushRemote" 2>/dev/null || true)
[ -z "$GATE_EFFECTIVE_REMOTE" ] && GATE_EFFECTIVE_REMOTE=$(git -C "$CWD" config --get remote.pushDefault 2>/dev/null || true)
[ -z "$GATE_EFFECTIVE_REMOTE" ] && GATE_EFFECTIVE_REMOTE=$(git -C "$CWD" config --get "branch.${CURRENT_BRANCH}.remote" 2>/dev/null || true)
[ -z "$GATE_EFFECTIVE_REMOTE" ] && GATE_EFFECTIVE_REMOTE="origin"
if [ "$GATE_EFFECTIVE_REMOTE" != "origin" ]; then
  emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): the current branch's effective push remote is '$GATE_EFFECTIVE_REMOTE', but this gate proves rebase/provenance/base attribution against 'origin' only — multi-remote push flows are outside the initial surface. Push to origin, or remove the branch.${CURRENT_BRANCH}.pushRemote / remote.pushDefault override."
  exit 0
fi
if [ -n "$GATE_REMOTE" ] && [ "$GATE_REMOTE" != "origin" ]; then
  emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): push targets remote '$GATE_REMOTE', but this gate proves rebase/provenance/base attribution against 'origin' only, so a verdict for '$GATE_REMOTE' would be attributed to the wrong lineage. Push to origin; multi-remote flows are outside the initial surface."
  exit 0
fi

# Config-expansion soundness for EVERY update push (B2, real-git repros):
# push.followTags=true adds annotated tags to an explicit single-branch push
# (--dry-run --porcelain printed refs/heads/main AND refs/tags/v1), and
# remote.origin.mirror=true turns any push into a mirror of all refs. Either
# way 'exactly one update' is disproven by repo config the refspec never
# shows.
if [ "$(git -C "$CWD" config --type=bool push.followTags 2>/dev/null)" = "true" ]; then
  emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): push.followTags is enabled, so this push also publishes annotated tags beyond the single branch update — exactly-one is not proven. Unset push.followTags (git config --unset push.followTags) or push tags deliberately in a separate reviewed step."
  exit 0
fi
if [ "$(git -C "$CWD" config --type=bool remote.origin.mirror 2>/dev/null)" = "true" ]; then
  emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): remote.origin.mirror is enabled, so any push mirrors every ref — exactly-one is not proven and object validation cannot be attributed. Remove the mirror configuration for gated pushes."
  exit 0
fi

# Upstream branch name, needed by both the bare-push destination resolution
# and the explicit destination rule. origin/feat/x strips to feat/x.
# Output is kept ONLY on rev-parse success: on failure --abbrev-ref can echo
# the literal '@{upstream}' to stdout (observed with an upstream CONFIGURED
# but unborn — branch.<name>.merge set by an empty-repo clone while the
# remote-tracking ref does not exist), and '|| true' inside the substitution
# kept that echo as a phantom upstream name.
GATE_UPSTREAM_NAME=""
if ! GATE_UPSTREAM_SHORT=$(git -C "$CWD" rev-parse --abbrev-ref --symbolic-full-name "@{upstream}" 2>/dev/null); then
  GATE_UPSTREAM_SHORT=""
fi
[ -n "$GATE_UPSTREAM_SHORT" ] && GATE_UPSTREAM_NAME="${GATE_UPSTREAM_SHORT#*/}"

if [ -z "$GATE_SRC" ] && [ -z "$GATE_DST" ]; then
  # Bare-push config-expansion soundness: with no explicit refspec, git
  # expands the push through remote.<name>.push refspecs and push.default.
  # 'Exactly one update of HEAD' is only PROVEN when no push refspecs are
  # configured and push.default names a single-update mode — and the
  # DESTINATION must be resolved from that mode, not assumed to be the
  # current branch name (push.default=upstream pushes to the upstream's
  # name, which may differ).
  GATE_REMOTE_PUSH=$(git -C "$CWD" config --get-all "remote.origin.push" 2>/dev/null || true)
  if [ -n "$GATE_REMOTE_PUSH" ]; then
    emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): remote.origin.push refspec(s) are configured, so a bare 'git push' expands through them and is not provably a single update of HEAD. Push explicitly ('git push origin $CURRENT_BRANCH') or remove the configured push refspec(s)."
    exit 0
  fi
  GATE_PUSH_DEFAULT=$(git -C "$CWD" config --get push.default 2>/dev/null || echo "simple")
  case "$GATE_PUSH_DEFAULT" in
    current)
      GATE_DST_NAME="$CURRENT_BRANCH"
      ;;
    upstream)
      if [ -z "$GATE_UPSTREAM_NAME" ]; then
        emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): push.default=upstream with no configured upstream for '$CURRENT_BRANCH' — the bare push's destination cannot be resolved, so the verdict cannot name it. Configure an upstream or push explicitly."
        exit 0
      fi
      GATE_DST_NAME="$GATE_UPSTREAM_NAME"
      ;;
    simple)
      if [ -n "$GATE_UPSTREAM_NAME" ] && [ "$GATE_UPSTREAM_NAME" != "$CURRENT_BRANCH" ]; then
        emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): push.default=simple with an upstream named '$GATE_UPSTREAM_NAME' differing from the current branch '$CURRENT_BRANCH' — git itself refuses this bare push, so there is no single update to certify. Push explicitly."
        exit 0
      fi
      GATE_DST_NAME="$CURRENT_BRANCH"
      ;;
    *)
      emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): push.default is '$GATE_PUSH_DEFAULT', under which a bare 'git push' can expand to multiple or differently-named updates, so exactly-one is not proven. Set 'git config push.default simple' or push explicitly ('git push origin $CURRENT_BRANCH')."
      exit 0
      ;;
  esac
else
  GATE_DST_NAME="${GATE_DST:-$CURRENT_BRANCH}"
fi

# Source rule: the pushed source must resolve, and must BE the current HEAD —
# resolved before any mutation. Pushing ref B while ref A is checked out was
# the original vacuous-green incident: the phases validated A's tree and the
# green was read as B's. (Re-checked against the FINAL object after the
# mandatory rebase in part B — a literal-SHA source can diverge there.)
GATE_SRC_SHA=$(git -C "$CWD" rev-parse --verify --quiet "${GATE_SRC_REF}^{commit}" 2>/dev/null || true)
GATE_HEAD_SHA=$(git -C "$CWD" rev-parse --verify --quiet "HEAD^{commit}" 2>/dev/null || true)
if [ -z "$GATE_SRC_SHA" ] || [ -z "$GATE_HEAD_SHA" ]; then
  emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): pushed source '$GATE_SRC_REF' does not resolve to a commit in this repo (fail closed rather than guessing)."
  exit 0
fi
if [ "$GATE_SRC_SHA" != "$GATE_HEAD_SHA" ]; then
  emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): pushed source '$GATE_SRC_REF' ($GATE_SRC_SHA) is not the current HEAD ($GATE_HEAD_SHA). The initial surface validates exactly the checked-out object — check out the branch you are pushing (workers push from their own worktrees; managers push via 'git -C <worktree> push'), so the gate rebases and validates the same object it certifies."
  exit 0
fi

# Destination rule: the single update's destination must be the current
# branch's own name or its configured upstream branch name. Without this,
# 'git push origin HEAD:refs/heads/<integration>' tests the exact object but
# publishes it under a destination the provenance guard keyed on
# CURRENT_BRANCH never examines.
if [ "$GATE_DST_NAME" != "$CURRENT_BRANCH" ] && { [ -z "$GATE_UPSTREAM_NAME" ] || [ "$GATE_DST_NAME" != "$GATE_UPSTREAM_NAME" ]; }; then
  emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): destination '$GATE_DST_NAME' is neither the current branch '$CURRENT_BRANCH' nor its configured upstream branch${GATE_UPSTREAM_NAME:+ ('$GATE_UPSTREAM_NAME')}. The initial surface certifies a branch only to its own destination; pushing HEAD under another branch name publishes a verdict against lineage this gate never examined."
  exit 0
fi
GATE_ACTIVE=1

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

# --- SABLE-rrn6r: provenance guard for a direct push of the branch that IS
# the integration branch. bin/sable-merge-gate promotes by pushing a python
# subprocess (sable_gate_git_lib._git), never a literal `git push` Bash
# command, so a legitimate gate promotion NEVER reaches this hook at all —
# this guard exists for the OTHER path: an agent typing `git push` directly
# against the integration branch, which is exactly how commit 2b1a15b landed
# with no ci-verify ref and no merge-preview provenance (SABLE-qhjq3):
# single-parent, CI ran only AFTER it was already on the branch instead of
# before. The gate's own promotion pushes the previewed object (byte-
# identical, I2 in bin/sable-merge-gate) to `refs/heads/ci-verify/<bead>-
# <sha7>` on origin BEFORE it ever promotes (sable_gate_preview_lib.py
# materialize_preview/kick_preview); the standard fetch refspec mirrors any
# refs/heads/* ref under refs/remotes/origin/*, so the `git fetch origin`
# above already pulled it down if it exists. A commit missing a matching ref
# was never previewed — deny it.
#
# Local-only leg (mechanism 2 of SABLE-rrn6r's two candidates): binds THIS
# fleet's hooked clients, not an unhooked client on the other side of a
# shared repo. Mechanism 1 (a GitHub ruleset on tmux-only requiring the
# ci-verify status check server-side, which WOULD bind an unhooked client
# too) is the binding fix and is DEFERRED pending operator-brokered
# cross-fleet coordination — see SABLE-rrn6r's notes for why it isn't done
# here.
#
# Scoped to the CURRENT_BRANCH == INTEGRATION_BRANCH self-push case (the same
# trigger the fofc guard below uses) and only when origin/<INT> is already
# published — an unpublished first-time publish has no remote history to
# check provenance against, matching SKIP_REBASE's own unpublished carve-out.
#
# Also scoped to an EXPLICITLY configured integration branch — repo-local
# `sable.integrationBranch`, a checked-in `.sable` integrationBranch= line, or
# $SABLE_INTEGRATION_BRANCH — NOT sable_resolve_integration_branch's own
# lower-priority fallbacks (deriving from $SABLE_BASE_BRANCH, or defaulting to
# "main"). Without this, a repo that never configured an integration branch
# at all still resolves one to "main" (the function's final fallback), which
# — for the overwhelmingly common case of a single-branch repo whose push
# target IS "main" — made CURRENT_BRANCH == INTEGRATION_BRANCH true for
# EVERY ordinary push and denied it for lacking a ci-verify ref no ordinary
# repo has ever heard of (caught by hooks/test/test-pre-push-rebase-test.sh
# going red across 8 unrelated fixtures on first run of this guard). The
# provenance requirement is meaningful only where a project has actually
# opted into the multi-manager integration-branch pattern.
INTEGRATION_BRANCH_EXPLICIT=$(git -C "$CWD" config --get sable.integrationBranch 2>/dev/null || true)
if [ -z "$INTEGRATION_BRANCH_EXPLICIT" ] && [ -f "$CWD/.sable" ]; then
  INTEGRATION_BRANCH_EXPLICIT=$(sed -n 's/^integrationBranch=//p' "$CWD/.sable" 2>/dev/null | head -1)
fi
[ -z "$INTEGRATION_BRANCH_EXPLICIT" ] && INTEGRATION_BRANCH_EXPLICIT="${SABLE_INTEGRATION_BRANCH:-}"

if [ -n "$INTEGRATION_BRANCH_EXPLICIT" ] && [ -n "$CURRENT_BRANCH" ] \
   && [ "$CURRENT_BRANCH" = "$INTEGRATION_BRANCH" ] \
   && git -C "$CWD" rev-parse --verify --quiet "origin/$INTEGRATION_BRANCH" >/dev/null 2>&1; then
  UNPROVEN=""
  while IFS= read -r new_sha; do
    [ -z "$new_sha" ] && continue
    if ! git -C "$CWD" for-each-ref "refs/remotes/origin/ci-verify/" --format='%(objectname)' 2>/dev/null \
         | grep -qx "$new_sha"; then
      UNPROVEN="$UNPROVEN ${new_sha:0:7}"
    fi
  done < <(git -C "$CWD" rev-list "origin/$INTEGRATION_BRANCH..HEAD" 2>/dev/null || true)

  if [ -n "$UNPROVEN" ]; then
    emit_deny "Pre-push denied (provenance guard): direct push to '$CURRENT_BRANCH' introduces commit(s)$UNPROVEN with no matching refs/heads/ci-verify/<bead>-<sha7> ref on origin — no evidence they were ever previewed by sable-merge-gate before landing (the SABLE-qhjq3 class: commit 2b1a15b landed the same way, single-parent, CI running only after the fact). Land this through 'sable-merge-gate' instead of a direct 'git push', so the exact object is previewed at a ci-verify/<bead>-<sha7> ref and CI-verified BEFORE it promotes."
    exit 0
  fi
fi

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
# SABLE-j90ba exact-object gate, part B: re-resolve the FINAL object after
# the mandatory rebase (phase 1 may have moved HEAD — the verdict must name
# the object that will actually be pushed, not the pre-rebase SHA), then
# materialize it in a clean disposable detached worktree. Phases 2-4 execute
# THERE: a dirty tracked file, an untracked overlay, or any working-tree
# state can no longer alter the verdict, and — as a structural side effect —
# untracked scratch files stop amplifying test selection for push-gate runs.
#
# Checkout hooks are suppressed during materialization/removal: repos on this
# machine set core.hooksPath at the bd shims (post-checkout runs bd), and a
# validation step must not trigger costful mutable hook work.
#
# Cleanup runs from an EXIT trap, with INT/TERM routed through exit so the
# trap fires on signals too; a worktree-prune before materialization
# self-heals stale admin entries left by a SIGKILLed prior run (cf
# SABLE-fk157 — the signal-skips-finally leak class).
# ---------------------------------------------------------------------------
GATE_RECORD=""
PHASE_DIR="$CWD"
if [ "$GATE_ACTIVE" -eq 1 ]; then
  GATE_FINAL_OBJECT=$(git -C "$CWD" rev-parse --verify --quiet "HEAD^{commit}" 2>/dev/null || true)

  # Post-rebase source re-check: the branch ref and HEAD moved together
  # through the rebase, but a LITERAL SHA (or any alias frozen at the
  # pre-rebase object) now diverges — git would push the OLD object while
  # the gate validates the new one. That divergence is exactly the
  # 'source SHA changed by the rebase' negative control.
  GATE_SRC_SHA_FINAL=$(git -C "$CWD" rev-parse --verify --quiet "${GATE_SRC_REF}^{commit}" 2>/dev/null || true)
  if [ -z "$GATE_FINAL_OBJECT" ] || [ "$GATE_SRC_SHA_FINAL" != "$GATE_FINAL_OBJECT" ]; then
    emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): after the mandatory rebase, pushed source '$GATE_SRC_REF' no longer resolves to the final rebased HEAD (${GATE_FINAL_OBJECT:-<unresolved>}) — the push would publish the pre-rebase object while the gate validated the post-rebase one. Push the branch by name (or HEAD) so the source follows the rebase."
    exit 0
  fi

  GATE_BASE_OBJECT="(none)"
  if [ -n "${BASE_BRANCH:-}" ]; then
    GATE_BASE_OBJECT=$(git -C "$CWD" rev-parse --verify --quiet "${BASE_BRANCH}^{commit}" 2>/dev/null || echo "(unresolved)")
  fi

  GATE_COMMON_DIR=$(git -C "$CWD" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)

  # Self-heal ONLY THIS GATE'S stale admin entries. Scope of the heal,
  # stated precisely: a SIGKILLed run skips every trap and leaves BOTH its
  # temp tree and its admin entry; this sweep removes the admin entry only
  # AFTER external temp cleanup (tmp reaper/reboot) has made the recorded
  # gitdir path absent — an intact orphan persists until then. Never
  # 'git worktree prune': other agents keep legitimately-registered
  # worktrees (coverage-floor records among them), and a global prune
  # mutates records this gate does not own. Our entries are recognizable by
  # the unique wt-sable-exact-object.* basename.
  if [ -n "$GATE_COMMON_DIR" ] && [ -d "$GATE_COMMON_DIR/worktrees" ]; then
    for GATE_STALE in "$GATE_COMMON_DIR"/worktrees/wt-sable-exact-object.*; do
      [ -d "$GATE_STALE" ] || continue
      GATE_STALE_GITDIR=$(cat "$GATE_STALE/gitdir" 2>/dev/null || true)
      if [ -z "$GATE_STALE_GITDIR" ] || [ ! -e "$GATE_STALE_GITDIR" ]; then
        rm -rf "$GATE_STALE"
      fi
    done
  fi

  GATE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/sable-exact-object.XXXXXX" 2>/dev/null) || GATE_ROOT=""
  GATE_TREE="${GATE_ROOT}/wt-${GATE_ROOT##*/}"
  GATE_NOHOOKS="${GATE_ROOT}/nohooks"
  sable_gate_cleanup() {
    if [ -n "${GATE_ROOT:-}" ]; then
      git -C "$CWD" -c core.hooksPath="$GATE_NOHOOKS" worktree remove --force "$GATE_TREE" >/dev/null 2>&1 || true
      if [ -n "${GATE_COMMON_DIR:-}" ] && [ -d "$GATE_COMMON_DIR/worktrees/${GATE_TREE##*/}" ]; then
        rm -rf "$GATE_COMMON_DIR/worktrees/${GATE_TREE##*/}"
      fi
      rm -rf "$GATE_ROOT"
    fi
  }
  trap sable_gate_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  if [ -z "$GATE_ROOT" ] || ! mkdir -p "$GATE_NOHOOKS" 2>/dev/null || \
     ! git -C "$CWD" -c core.hooksPath="$GATE_NOHOOKS" worktree add --detach "$GATE_TREE" "$GATE_FINAL_OBJECT" >/dev/null 2>&1; then
    emit_deny "Pre-push denied (exact-object gate, SABLE-j90ba): could not materialize a clean detached worktree at final object ${GATE_FINAL_OBJECT:-<unresolved>} — the gate cannot validate the exact pushed object, so it fails closed rather than validating the working tree by proxy. Inspect 'git worktree list', check disk space under ${TMPDIR:-/tmp}, and retry."
    exit 0
  fi
  PHASE_DIR="$GATE_TREE"

  # Bridge AUDITED dependency dirs into the clean worktree by symlink.
  # Without this the global hook breaks every JS/venv repo it gates: their
  # auto-detected phases (npx --no-install tsc, npm test/build, pytest under
  # .venv) need dependency dirs that are git-ignored and so absent from the
  # materialized object; installing per push is not acceptable. A dir is
  # bridged ONLY when it exists in the live repo, is ABSENT from the
  # object's tree, and is IGNORED per the OBJECT's own gitignore rules
  # (check-ignore runs in the worktree) — tracked source can never be
  # shadowed, and an unrelated untracked source file stays excluded. The
  # symlink lives inside GATE_ROOT, so cleanup removes the link, never the
  # target.
  GATE_BRIDGED=""
  for GATE_DEP in node_modules .venv; do
    # check-ignore queried WITH the trailing slash: the conventional ignore
    # pattern 'node_modules/' is directory-only, and a slashless query for a
    # path that does not exist in the clean worktree never matches it.
    if [ -d "$CWD/$GATE_DEP" ] && [ ! -e "$GATE_TREE/$GATE_DEP" ] \
       && git -C "$GATE_TREE" check-ignore -q "$GATE_DEP/" 2>/dev/null; then
      if ln -s "$CWD/$GATE_DEP" "$GATE_TREE/$GATE_DEP" 2>/dev/null; then
        GATE_BRIDGED="${GATE_BRIDGED:+$GATE_BRIDGED }$GATE_DEP"
      fi
    fi
  done

  GATE_RECORD="exact-object gate: source ref '$GATE_SRC_REF' -> destination '$GATE_DST_NAME' on remote 'origin'; final object $GATE_FINAL_OBJECT; base object $GATE_BASE_OBJECT (${BASE_BRANCH:-no base}); phases executed in a clean detached worktree at the final object${GATE_BRIDGED:+; bridged ignored dependency dir(s): $GATE_BRIDGED}"
fi

# ---------------------------------------------------------------------------
# Phase 2: STATIC analysis (typecheck + optional lint) — never skippable
# ---------------------------------------------------------------------------

STATIC_TIMEOUT="${SABLE_PRE_PUSH_STATIC_TIMEOUT:-90}"

# SABLE-j90ba: commands, manifests, .sable, and timeouts all RESOLVE from the
# clean worktree — the OBJECT is the configuration surface, so a dirty
# .sable edit (invisible to any diff) cannot weaken the gate; a committed
# weakening is at least reviewable and still faces the full ci-verify suite.
# Repo-local git config is shared by linked worktrees, so sable.testCommand
# resolution is unchanged.
TYPECHECK_CMD=$(detect_typecheck_cmd "$PHASE_DIR" "$BASE_BRANCH")

if [ -n "$TYPECHECK_CMD" ]; then
  TC_EXIT=0
  TC_OUT=$(cd "$PHASE_DIR" && timeout "$STATIC_TIMEOUT" sh -c "$TYPECHECK_CMD" 2>&1) || TC_EXIT=$?

  if [ "$TC_EXIT" -ne 0 ]; then
    if [ "$TC_EXIT" -eq 124 ]; then
      SUFFIX="Typecheck exceeded SABLE_PRE_PUSH_STATIC_TIMEOUT=${STATIC_TIMEOUT}s. Either narrow the typecheck scope or raise the timeout."
    else
      SUFFIX="Typecheck reported errors. This phase CANNOT be skipped via SABLE_SKIP_PRE_PUSH — it is structurally required. Fix the type errors before pushing."
    fi
    emit_deny "Pre-push phase 2 (static): typecheck failed (\`$TYPECHECK_CMD\`).
${SUFFIX}${GATE_RECORD:+

$GATE_RECORD}

$(sable_tail_chars "$TC_OUT" 1500)"
    exit 0
  fi
fi

# Lint phase: opt-in only, no auto-detect
LINT_CMD="${SABLE_PRE_PUSH_LINT_COMMAND:-}"

if [ -n "$LINT_CMD" ]; then
  LINT_EXIT=0
  LINT_OUT=$(cd "$PHASE_DIR" && timeout "$STATIC_TIMEOUT" sh -c "$LINT_CMD" 2>&1) || LINT_EXIT=$?

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
TRIPWIRE_BIN="$PHASE_DIR/bin/sable-fixture-tripwire"

if [ -x "$TRIPWIRE_BIN" ] && [ -n "$BASE_BRANCH" ]; then
  TRIPWIRE_TARGETS=()
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in
      hooks/test/*.sh|bin/test_*.py) TRIPWIRE_TARGETS+=("$f") ;;
    esac
  done < <(git -C "$PHASE_DIR" diff --name-only --diff-filter=ACMR "${BASE_BRANCH}...HEAD" 2>/dev/null || true)

  if [ ${#TRIPWIRE_TARGETS[@]} -gt 0 ]; then
    TW_EXIT=0
    TW_OUT=$(cd "$PHASE_DIR" && timeout "$STATIC_TIMEOUT" "$TRIPWIRE_BIN" "${TRIPWIRE_TARGETS[@]}" 2>&1) || TW_EXIT=$?

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

BUILD_CMD=$(detect_build_cmd "$PHASE_DIR" "$BASE_BRANCH")

if [ -n "$BUILD_CMD" ]; then
  BUILD_EXIT=0
  BUILD_OUT=$(cd "$PHASE_DIR" && timeout "$BUILD_TIMEOUT" sh -c "$BUILD_CMD" 2>&1) || BUILD_EXIT=$?

  if [ "$BUILD_EXIT" -ne 0 ]; then
    if [ "$BUILD_EXIT" -eq 124 ]; then
      SUFFIX="Build exceeded SABLE_PRE_PUSH_BUILD_TIMEOUT=${BUILD_TIMEOUT}s. Either narrow the build scope or raise the timeout."
    else
      SUFFIX="Build failed. This phase CANNOT be skipped via SABLE_SKIP_PRE_PUSH — it is structurally required (build-only errors, e.g. Next.js page-export errors, pass typecheck and the test suite but fail the real build)."
    fi
    emit_deny "Pre-push phase 3 (build): build failed (\`$BUILD_CMD\`).
${SUFFIX}${GATE_RECORD:+

$GATE_RECORD}

$(sable_tail_chars "$BUILD_OUT" 1500)"
    exit 0
  fi
fi

# ---------------------------------------------------------------------------
# Phase 4: TESTS (skippable via SABLE_SKIP_PRE_PUSH=1 or PHASE=skip)
# ---------------------------------------------------------------------------

TEST_PHASE="${SABLE_PRE_PUSH_TEST_PHASE:-auto}"

if [ "$TEST_PHASE" = "skip" ]; then
  emit_context "Pre-push: rebase + static + build phases passed; test phase skipped (SABLE_PRE_PUSH_TEST_PHASE=skip). Repo git hooks handle test gating on the rebased state.${GATE_RECORD:+
$GATE_RECORD}"
  exit 0
fi

if [ "${SABLE_SKIP_PRE_PUSH:-}" = "1" ]; then
  emit_context "Pre-push: rebase + static + build phases passed; test phase bypassed (SABLE_SKIP_PRE_PUSH=1). Note: typecheck/lint/build were still enforced — bypass is now scoped to the test phase only.${GATE_RECORD:+
$GATE_RECORD}"
  exit 0
fi

TEST_CMD=$(detect_test_cmd "$PHASE_DIR" "$BASE_BRANCH")

if [ -z "$TEST_CMD" ]; then
  emit_context "Pre-push: rebase + static + build phases passed; no test command detected (no package.json/pyproject.toml/Cargo.toml/go.mod found upward or in any changed-file subdir). Add a testCommand= line to .sable (checked in), or set sable.testCommand via git config, or set SABLE_TEST_COMMAND, to enforce tests before push.${GATE_RECORD:+
$GATE_RECORD}"
  exit 0
fi

TEST_TIMEOUT=$(sable_resolve_test_timeout "$PHASE_DIR")
TEST_EXIT=0
# The RECORDED command is captured HERE, at the invocation site, from the same
# variable the shell is about to execute — never re-resolved from config
# afterwards. SABLE-b99hy caught say-versus-do divergence in BOTH directions
# one drain apart: a worker reporting a config applied that was not, and a
# temporary override still in place after the push it was granted for. A
# report of what was enforced is not evidence of what was enforced, so the
# only string this gate is allowed to certify is the one it ran.
TEST_CMD_EXECUTED="$TEST_CMD"
TEST_OUT=$(cd "$PHASE_DIR" && timeout "$TEST_TIMEOUT" sh -c "$TEST_CMD_EXECUTED" 2>&1) || TEST_EXIT=$?

# Prior intent, if a dispatcher declared one. Evidence only — it never selects
# what runs, and when it disagrees with the executed command the DIVERGENCE is
# reported rather than reconciled away.
TEST_CMD_RECORD="enforced test command (executed): \`$TEST_CMD_EXECUTED\`"
if [ -n "${SABLE_TEST_COMMAND_INTENT:-}" ] && \
   [ "${SABLE_TEST_COMMAND_INTENT}" != "$TEST_CMD_EXECUTED" ]; then
  TEST_CMD_RECORD="$TEST_CMD_RECORD
DIVERGENCE: prior intent was \`${SABLE_TEST_COMMAND_INTENT}\`, which is NOT what ran. The executed command above is what this gate certifies; the intent value certifies nothing."
fi

# SABLE-y4nom.7.2: the INCOMPLETE-BY-TRIM marker DOMINATES the exit code.
# sable-dev-check emits one machine line naming every selected suite its
# plan omitted; a configured wrapper (`... || true`) can launder rc3 back
# to rc0, so gating on TEST_EXIT alone would print all-phases-passed over
# an unverified claim. The marker line is extracted SEPARATELY from the
# truncated output tail — truncation can never eat the omitted set.
INCOMPLETE_MARKER_LINE=$(printf '%s\n' "$TEST_OUT" \
  | grep '^SABLE_DEV_CHECK_INCOMPLETE_BY_TRIM=' | tail -1 || true)
if [ -n "$INCOMPLETE_MARKER_LINE" ]; then
  # rc-sensitive detail: the marker legitimately rides kept-FAILURE (rc1)
  # and internal-timeout (rc124) exits too, so "nothing failed" is only
  # claimed when it is true. The marker still dominates CLASSIFICATION in
  # every case — this deny is INCOMPLETE, never all-phases-passed.
  if [ "$TEST_EXIT" -eq 0 ]; then
    RC_NOTE="CONTRACT VIOLATION: the INCOMPLETE verdict was swallowed — the marker is present but the test command exited 0, which means a wrapper (e.g. \`|| true\`) laundered the exit code. Remove the wrapper; a gate command that cannot fail certifies nothing.
"
  elif [ "$TEST_EXIT" -eq 3 ]; then
    RC_NOTE="Nothing that ran failed; the plan itself was incomplete.
"
  elif [ "$TEST_EXIT" -eq 124 ]; then
    RC_NOTE="Additionally, the run TIMED OUT (exit 124) before finishing what it kept — the omissions below are on top of an unfinished run.
"
  else
    RC_NOTE="Additionally, the kept suites did not all pass (exit ${TEST_EXIT}) — fix the failure AND close the omissions below.
"
  fi
  emit_deny "Pre-push phase 4 (tests): INCOMPLETE — the run trimmed selected suite(s) to fit its budget, so the configured claim was NOT fully verified, and an unverified claim does not push.
${RC_NOTE}Publish fresh shared measurements with \`sable-dev-check --publish-cost-profile\` (one per host, coordinated quiet window — broad seat when a fleet exists, standalone runner otherwise; expensive) so the derived budgets fit the real cost, raise the bound, or reduce the suites' real runtime. Do NOT narrow \`sable.testCommand\` or the selected suite set to fit (SABLE-b99hy).

$INCOMPLETE_MARKER_LINE

${TEST_CMD_RECORD}${GATE_RECORD:+
$GATE_RECORD}

$(sable_tail_chars "$TEST_OUT" 1500)"
  exit 0
fi

if [ "$TEST_EXIT" -ne 0 ]; then
  if [ "$TEST_EXIT" -eq 124 ]; then
    SUFFIX="Tests exceeded the ${TEST_TIMEOUT}s test-phase timeout, so this push has NO verdict — nothing was verified, rather than something failing. Publish fresh shared measurements with \`sable-dev-check --publish-cost-profile\` (the one-per-host serial publisher — coordinated quiet window) so the derived per-command budgets in bin/sable-dev-check reflect current cost, or reduce the suites' real runtime. Do NOT narrow \`sable.testCommand\` or the selected suite set to fit: a smaller claim that passes is not the configured claim passing, and this gate records what it actually ran (SABLE-b99hy)."
  else
    SUFFIX="Tests failed. Fix before pushing, or set SABLE_SKIP_PRE_PUSH=1 with explicit intent (rebase + static + build still run)."
  fi
  emit_deny "Pre-push phase 4 (tests): \`$TEST_CMD_EXECUTED\` failed.
${SUFFIX}

${TEST_CMD_RECORD}${GATE_RECORD:+
$GATE_RECORD}

$(sable_tail_chars "$TEST_OUT" 1500)"
  exit 0
fi

emit_context "Pre-push: all phases passed.
${TEST_CMD_RECORD}${GATE_RECORD:+
$GATE_RECORD}"
exit 0
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main
fi
