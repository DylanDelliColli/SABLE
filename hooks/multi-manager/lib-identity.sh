#!/usr/bin/env bash
# lib-identity.sh — shared identity resolution for multi-manager hooks (SABLE-uz9.3)
#
# Resolves WHO is acting from, in priority order:
#   1. Hook input JSON: agent_id present => subagent context. Identity is the
#      agent_type field (the agent-definition name; verified present on
#      CC 2.1.170, spike SABLE-uz9.1). Env vars are the PARENT session's
#      identity in this context and MUST be ignored — this closes the
#      "subagent contamination" hole documented in MULTI-MANAGER-PATTERN.md.
#      Agent-Teams members resolve here too (SABLE-amj.2): a member spawned with
#      name=<role> carries agent_type=<role> in its hook input (capture-verified,
#      SABLE-amj.1) — the team config's agentType field (e.g. general-purpose) is
#      a DIFFERENT field and is NOT what appears here. Members thus need no
#      special branch, but they MUST be spawned under their registry name.
#   2. CLAUDE_AGENT_NAME / CLAUDE_AGENT_ROLE env vars (legacy terminal
#      launches — Chuck's holdout terminal and any pre-v2 alias). Dual-mode
#      support is a hard requirement of SABLE-uz9.3.
#
# Usage (from a hook that already captured its stdin):
#   source "$(dirname "${BASH_SOURCE[0]}")/lib-identity.sh"
#   sable_resolve_identity "$HOOK_INPUT_JSON"
#
# Sets (always, possibly empty/zero):
#   SABLE_ID_NAME         lowercase agent name ("optimus", "sherlock", "explore", "")
#   SABLE_ID_TYPE         registry type from agents.yaml ("epic_manager", ...) or ""
#   SABLE_ID_SOURCE       agent_type | env | none
#   SABLE_ID_IS_SUBAGENT  1 if hook input carried agent_id, else 0
#   SABLE_ID_IS_MANAGER   1 if the identity should receive manager-hook behavior
#   SABLE_ID_IS_REGISTERED 1 if the name has an agents.yaml entry
#
# Manager-ness:
#   - registry type in: epic_manager one_off_manager integrator strategist cockpit
#   - OR legacy: env-sourced identity with CLAUDE_AGENT_ROLE=manager and no
#     registry entry (an adopter's custom alias keeps working unchanged)
#   Unregistered subagent types (Explore, general-purpose, code-reviewer, ...)
#   are workers: never managers, hooks stand down for them.
#
# Registry path: ~/.claude/sable/agents.yaml (override with SABLE_AGENTS_YAML,
# used by tests). Parsed with awk — no python-yaml dependency.

sable_resolve_identity() {
  local json="${1:-}"
  SABLE_ID_NAME=""
  SABLE_ID_TYPE=""
  SABLE_ID_SOURCE="none"
  SABLE_ID_IS_SUBAGENT=0
  SABLE_ID_IS_MANAGER=0
  SABLE_ID_IS_REGISTERED=0

  local parsed agent_id agent_type
  parsed=$(printf '%s' "$json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('agent_id', '') or '')
print(d.get('agent_type', '') or '')
" 2>/dev/null) || parsed=""
  agent_id=$(printf '%s\n' "$parsed" | sed -n '1p')
  agent_type=$(printf '%s\n' "$parsed" | sed -n '2p')

  if [ -n "$agent_id" ]; then
    SABLE_ID_IS_SUBAGENT=1
    if [ -n "$agent_type" ]; then
      SABLE_ID_NAME=$(printf '%s' "$agent_type" | tr '[:upper:]' '[:lower:]')
      SABLE_ID_SOURCE="agent_type"
    fi
    # NOTE: env deliberately not consulted — it belongs to the parent session.
  elif [ -n "${CLAUDE_AGENT_NAME:-}" ]; then
    SABLE_ID_NAME=$(printf '%s' "$CLAUDE_AGENT_NAME" | tr '[:upper:]' '[:lower:]')
    SABLE_ID_SOURCE="env"
  fi

  [ -z "$SABLE_ID_NAME" ] && return 0

  local yaml="${SABLE_AGENTS_YAML:-${HOME:-}/.claude/sable/agents.yaml}"
  if [ -f "$yaml" ]; then
    SABLE_ID_TYPE=$(awk -v name="$SABLE_ID_NAME" '
      $0 == "  " name ":" { found = 1; next }
      found && /^    type:/ { sub(/^    type:[ ]*/, ""); sub(/[ \t#].*$/, ""); print; exit }
      found && /^  [a-zA-Z0-9_-]+:/ { exit }
    ' "$yaml" 2>/dev/null)
  fi
  [ -n "$SABLE_ID_TYPE" ] && SABLE_ID_IS_REGISTERED=1

  case " epic_manager one_off_manager integrator strategist cockpit " in
    *" $SABLE_ID_TYPE "*) SABLE_ID_IS_MANAGER=1 ;;
  esac

  # Legacy escape: custom env-launched manager alias not (yet) in the registry.
  if [ "$SABLE_ID_IS_MANAGER" -eq 0 ] && [ "$SABLE_ID_SOURCE" = "env" ] \
     && [ "${CLAUDE_AGENT_ROLE:-}" = "manager" ] && [ "$SABLE_ID_IS_REGISTERED" -eq 0 ]; then
    SABLE_ID_IS_MANAGER=1
  fi

  return 0
}

# sable_is_git_push <command-string>
#
# Returns 0 (true) when <command-string> is a real `git push` invocation;
# 1 (false) otherwise.
#
# Matches:
#   git push
#   git -C /path push
#   git -c a=b push origin main
#   git --no-pager push
#   SABLE_SKIP_PRE_PUSH=1 git push        (env-assignment prefix)
#   FOO=bar BAZ=qux git -C /x push       (multiple env assignments)
#   env FOO=bar git push                  (env(1) prefix)
#   env -u GIT_DIR git push              (env -u NAME prefix)
#
# Does NOT match:
#   Commands where "git push" appears only inside a quoted argument
#     e.g.  bd create --description="... git push ..."
#   Substrings like `git pushd`, `echo git pushed`
#
# Algorithm:
#   shlex-tokenize the command (same approach proven in hooks/tdd-gate.sh
#   post SABLE-sqz).  Walk the token list:
#     - At command position, NAME=VALUE tokens are transparent env assignments —
#       consume them WITHOUT leaving command position.
#     - At command position, the token `env` is also transparent: after it,
#       continue consuming NAME=VALUE tokens and -u NAME pairs (env(1) options)
#       while staying at command position.
#     - Find the first `git` token at "command position" (first token, after
#       a shell separator: ; && || |, or after leading env assignments/env(1)).
#     - Skip git global flags: -C <arg>, -c <arg>, --no-pager, --git-dir=*, --work-tree=*,
#       --namespace=*, -p/--paginate, -P/--no-pager, --no-replace-objects, --bare,
#       --literal-pathspecs, --glob-pathspecs, --noglob-pathspecs, --icase-pathspecs,
#       --no-optional-locks, --exec-path=*, --html-path, --man-path, --info-path,
#       --version, --help.
#     - If the next non-flag token is exactly `push`, return 0.
sable_is_git_push() {
  local cmd="${1:-}"
  [ -z "$cmd" ] && return 1
  CMD_STR="$cmd" python3 -c "
import os, re, shlex, sys

cmd = os.environ.get('CMD_STR', '')
try:
    tokens = shlex.split(cmd)
except ValueError:
    sys.exit(1)

SHELL_SEPS = {';', '&&', '||', '|'}
# git global flags that consume the next token as an argument
CONSUME_NEXT = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path'}
# git global flags that are standalone (no next-arg consumed)
STANDALONE = {
    '--no-pager', '-p', '--paginate', '-P', '--no-replace-objects', '--bare',
    '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs',
    '--icase-pathspecs', '--no-optional-locks', '--html-path', '--man-path',
    '--info-path', '--version', '--help',
}
# prefixes that are standalone flags (--exec-path=, --git-dir=, etc.)
STANDALONE_PREFIXES = ('--exec-path=', '--git-dir=', '--work-tree=', '--namespace=')

ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

i = 0
n = len(tokens)
# Track whether we are at a command-position (start or after separator)
at_cmd_pos = True
while i < n:
    tok = tokens[i]
    if tok in SHELL_SEPS:
        at_cmd_pos = True
        i += 1
        continue
    # At command position: transparent env-assignment prefix (NAME=VALUE)
    if at_cmd_pos and ENV_ASSIGN_RE.match(tok):
        i += 1  # consume assignment, stay at command position
        continue
    # At command position: env(1) prefix — consume it and its own options
    if at_cmd_pos and tok == 'env':
        i += 1
        while i < n:
            t = tokens[i]
            if ENV_ASSIGN_RE.match(t):
                i += 1   # env NAME=VALUE — consume, stay in env-option walk
                continue
            if t == '-u' and i + 1 < n:
                i += 2   # env -u NAME — consume both, stay in env-option walk
                continue
            break        # next token is the real command — fall through to outer loop
        continue         # re-evaluate tokens[i] at command position (at_cmd_pos still True)
    if at_cmd_pos and tok == 'git':
        # Found git at command position — now walk flags
        i += 1
        while i < n:
            t = tokens[i]
            if t in CONSUME_NEXT:
                i += 2  # skip flag + its argument
                continue
            if t in STANDALONE or any(t.startswith(p) for p in STANDALONE_PREFIXES):
                i += 1
                continue
            # Not a known flag — this must be the subcommand
            sys.exit(0 if t == 'push' else 1)
        # Ran out of tokens after git — no subcommand found
        sys.exit(1)
    # Not at command position or not git/env/assignment
    at_cmd_pos = False
    i += 1
sys.exit(1)
" 2>/dev/null
}

# sable_resolve_push_repo_dir <cwd> <command>
#
# Returns the effective git working directory for a push command: the shell
# <cwd> with every `git -C <path>` option from <command> applied in order,
# matching git's own semantics (an absolute -C replaces the accumulated dir;
# a relative -C is joined onto it). Falls back to <cwd> when no -C is present.
#
# This is the directory git ACTUALLY operates in. The pre-push gate must
# rebase/typecheck/test there, and post-push must read its branch + diff
# there — NOT the raw shell cwd, which differs whenever a manager pushes a
# worktree via `git -C <worktree> push` from the main checkout (SABLE-041).
#
# Tokenizes with shlex and reuses the same command-position / env-prefix walk
# as sable_is_git_push so env-assignment and env(1) prefixes are transparent.
sable_resolve_push_repo_dir() {
  local cwd="${1:-}" cmd="${2:-}"
  CWD_STR="$cwd" CMD_STR="$cmd" python3 -c "
import os, re, shlex, sys

cwd = os.environ.get('CWD_STR', '')
cmd = os.environ.get('CMD_STR', '')
try:
    tokens = shlex.split(cmd)
except ValueError:
    print(cwd); sys.exit(0)

SHELL_SEPS = {';', '&&', '||', '|'}
CONSUME_NEXT = {'-c', '--git-dir', '--work-tree', '--namespace', '--exec-path'}
ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# Walk to the first 'git' at command position (env-assignment / env(1)
# prefixes are transparent — mirror sable_is_git_push).
i, n = 0, len(tokens)
at_cmd_pos = True
git_idx = -1
while i < n:
    tok = tokens[i]
    if tok in SHELL_SEPS:
        at_cmd_pos = True; i += 1; continue
    if at_cmd_pos and ENV_ASSIGN_RE.match(tok):
        i += 1; continue
    if at_cmd_pos and tok == 'env':
        i += 1
        while i < n:
            t = tokens[i]
            if ENV_ASSIGN_RE.match(t): i += 1; continue
            if t == '-u' and i + 1 < n: i += 2; continue
            break
        continue
    if at_cmd_pos and tok == 'git':
        git_idx = i; break
    at_cmd_pos = False; i += 1

eff = cwd
if git_idx >= 0:
    j = git_idx + 1
    while j < n:
        t = tokens[j]
        if t == '-C' and j + 1 < n:
            p = tokens[j + 1]
            eff = p if os.path.isabs(p) else os.path.join(eff or '.', p)
            j += 2; continue
        if t in CONSUME_NEXT and j + 1 < n:
            j += 2; continue
        if t.startswith('-'):
            j += 1; continue
        break  # subcommand reached — stop scanning global flags
print(eff)
" 2>/dev/null || printf '%s' "$cwd"
}

# sable_validate_base_ref <repo-path> <desired-ref>
#
# Validates that <desired-ref> exists in <repo-path>.
# If it does, prints <desired-ref> and returns 0.
# If it does not, falls back in order:
#   1. origin/main
#   2. @{upstream} (the current branch's configured upstream)
# Prints the resolved ref and always returns 0 (one of the three will work
# or we fall back to the empty string to let the caller decide).
# This prevents hooks from aborting under set -euo pipefail when
# SABLE_BASE_BRANCH points to a ref that doesn't exist in the local repo.
sable_validate_base_ref() {
  local repo_path="${1:-}"
  local desired="${2:-origin/main}"
  [ -z "$repo_path" ] && { printf '%s' "$desired"; return 0; }

  if git -C "$repo_path" rev-parse --verify --quiet "$desired" >/dev/null 2>&1; then
    printf '%s' "$desired"
    return 0
  fi

  # Try origin/main as first fallback
  if [ "$desired" != "origin/main" ] && \
     git -C "$repo_path" rev-parse --verify --quiet "origin/main" >/dev/null 2>&1; then
    printf '%s' "origin/main"
    return 0
  fi

  # Try @{upstream} as second fallback
  local upstream
  upstream=$(git -C "$repo_path" rev-parse --abbrev-ref --symbolic-full-name "@{upstream}" 2>/dev/null || true)
  if [ -n "$upstream" ]; then
    printf '%s' "$upstream"
    return 0
  fi

  # Nothing worked — return the desired ref and let the caller handle failure
  printf '%s' "$desired"
  return 0
}

# sable_resolve_dispatch_lane <hook-input-json>
#
# For PreToolUse:Agent / PostToolUse:Agent hooks. Decides whether pre-dispatch
# governance applies to this Agent call and which manager LANE it belongs to.
#
# v3 (SABLE-4it): identity is the ONLY lane source — NEVER parsed from prompt text.
#
# Lanes:
#   - Manager-typed subagent (Optimus/Tarzan spawning a worker natively): active,
#     lane = the manager's own name. Confirmed on CC 2.1.173+ (spike SABLE-d50.1):
#     a nested PreToolUse:Agent fires carrying the spawner's agent_type.
#   - Legacy env-launched manager terminal (Chuck): active, lane = the manager.
#   - Anonymous main session (Lincoln, no agent_id) in execution mode: active,
#     lane = lincoln (self — Lincoln's utility spawns carry no bead IDs, so
#     claim/overlap/preempt no-op naturally).
#   - Everything else (workers, planning subagents, non-execution main session,
#     unreadable registry): stand down. Fail open.
#
# The v2 "Dispatching-for: <name>" relay parse (sable__parse_dispatch_for) is
# DELETED per the clean-break operator decision — identity (env or agent_type) is
# authoritative.
#
# Sets: SABLE_DISPATCH_ACTIVE (0|1), SABLE_DISPATCH_LANE (lowercase name or "").
# Mode-state path override for tests: SABLE_MODE_FILE.
sable_resolve_dispatch_lane() {
  local json="${1:-}"
  SABLE_DISPATCH_ACTIVE=0
  SABLE_DISPATCH_LANE=""

  sable_resolve_identity "$json"

  # Manager-subagents dispatch their own workers natively (nested Agent +
  # nested PreToolUse:Agent firing with the dispatcher's agent_type, confirmed
  # CC 2.1.177, SABLE-uz9.8/uz9.9). Governance applies to them, lane = the
  # manager's own name (a manager always dispatches for itself). Workers and
  # planning subagents (non-manager) still stand down.
  if [ "$SABLE_ID_IS_SUBAGENT" -eq 1 ]; then
    if [ "$SABLE_ID_IS_MANAGER" -eq 1 ]; then
      SABLE_DISPATCH_ACTIVE=1
      SABLE_DISPATCH_LANE="$SABLE_ID_NAME"
    fi
    return 0
  fi

  if [ "$SABLE_ID_SOURCE" = "env" ]; then
    if [ "$SABLE_ID_IS_MANAGER" -eq 1 ]; then
      SABLE_DISPATCH_ACTIVE=1
      SABLE_DISPATCH_LANE="$SABLE_ID_NAME"
    fi
    return 0
  fi

  local mode_file="${SABLE_MODE_FILE:-${HOME:-}/.claude/sable/state/mode-state.json}"
  [ -f "$mode_file" ] || return 0
  local mode
  mode=$(MODE_FILE="$mode_file" python3 -c "
import json, os
try:
    print(json.load(open(os.environ['MODE_FILE'])).get('mode', ''))
except Exception:
    print('')
" 2>/dev/null)
  [ "$mode" = "execution" ] || return 0

  # Lincoln main session in execution mode: lane = self. (Contract invariant 4 —
  # never the legacy "cockpit" default, never parsed from the prompt.)
  SABLE_DISPATCH_ACTIVE=1
  SABLE_DISPATCH_LANE="lincoln"
  return 0
}
