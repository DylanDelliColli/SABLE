#!/usr/bin/env bash
# tree-claim.sh — PreToolUse:Bash lockfile: one main session per checkout.
# Trigger: PreToolUse:Bash | Timeout: 3000ms
#
# Prevents two main sessions sharing one checkout from racing on the git index.
# Implements the SABLE-7kq operator decision (ba5424d incident: a side session
# swept staged changes into its gitignore commit because both sessions shared
# the index without a claim protocol).
#
# BEHAVIOR:
#   1. Only fires for index-mutating git commands:
#        git add, git commit, git rm, git mv,
#        git restore --staged, git reset
#      Tolerates global git flags (-C, -c k=v, --no-pager, etc.).
#      All other commands exit 0 immediately.
#
#   2. Resolves the claim file per checkout:
#        $(git -C "$CWD" rev-parse --git-dir)/sable-tree-claim
#      For a normal checkout this is <repo>/.git/sable-tree-claim.
#      For a `git worktree add` worktree, .git is a file pointing to the
#      per-worktree gitdir; rev-parse resolves it correctly, giving each
#      worktree its own independent claim file.
#      A leading `cd <dir> &&` in the same command line shifts the shell's
#      working directory before the git invocation runs; the effective-dir
#      walk tracks that (SABLE-5pci) so `cd <worktree> && git add` and
#      `git -C <worktree> add` resolve to the SAME claim file instead of the
#      cd-form silently falling back to the hook's original cwd.
#
#   3. Claim lifecycle (TTL default: 3600s, override SABLE_TREE_CLAIM_TTL):
#        No claim      → write this session's claim, allow.
#        Own claim     → refresh timestamp, allow.
#        Foreign fresh → deny, name holder + age + escape hatches.
#        Foreign stale → take over (overwrite), allow + additionalContext.
#
#   4. Escape hatches:
#        SABLE_TREE_CLAIM_OVERRIDE=1  → allow + take over + additionalContext.
#        Delete the claim file manually and retry.
#
#   5. Fail open on infrastructure errors (rev-parse failure, unreadable claim
#      file, missing session identity): allow + additionalContext, never deny.
#
# TODO: When SABLE-0u1/SABLE-jpr (shared git-subcommand tokenizer in
#       lib-identity.sh) lands, replace the local sable__parse_git_subcommand
#       below with the shared helper.

set -uo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

# ---------------------------------------------------------------------------
# Parse hook input
# ---------------------------------------------------------------------------
_python3_extract() {
  python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('$1', '') or '')
" 2>/dev/null
}

COMMAND=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print((d.get('tool_input') or {}).get('command', '') or '')
" 2>/dev/null) || COMMAND=""

CWD=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('cwd', '') or '')
" 2>/dev/null) || CWD=""
[ -z "$CWD" ] && CWD="${PWD:-}"

SESSION_ID=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('session_id', '') or '')
" 2>/dev/null) || SESSION_ID=""

# Fall back to env, then to unknown-PPID (fail open — don't deny on missing identity)
[ -z "$SESSION_ID" ] && SESSION_ID="${CLAUDE_SESSION_ID:-}"
IDENTITY_KNOWN=1
if [ -z "$SESSION_ID" ]; then
  SESSION_ID="unknown-${PPID:-0}"
  IDENTITY_KNOWN=0
fi

# market-brief-package-q6yu: a human-attributable name for the claim record's
# third field (status output shows "lincoln", not just a raw session UUID —
# the misidentification that led to a wrong release against an ACTIVE
# holder). Same signal order as SESSION_ID: hook-input agent_type (subagent
# context) first, then the legacy env terminal name. "-" when neither is set
# (the common case for an unnamed session) — sable-claim status/release still
# work off the session_id field in that case.
AGENT_NAME=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('agent_type', '') or '')
" 2>/dev/null) || AGENT_NAME=""
[ -z "$AGENT_NAME" ] && AGENT_NAME="${CLAUDE_AGENT_NAME:-}"
[ -z "$AGENT_NAME" ] && AGENT_NAME="-"

# ---------------------------------------------------------------------------
# Helper: emit an additionalContext response and exit 0 (allow)
# ---------------------------------------------------------------------------
allow_with_context() {
  # $1 = message
  MSG="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow',
        'additionalContext': os.environ.get('MSG', '')
    }
}))
"
  exit 0
}

# ---------------------------------------------------------------------------
# Helper: emit a deny response and exit 0
# ---------------------------------------------------------------------------
deny_with_reason() {
  # $1 = reason
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
  exit 0
}

# ---------------------------------------------------------------------------
# sable__is_index_mutating_git_command <command> <cwd>
#
# Tokenises the command with Python shlex (handles quoting).  Walks ALL
# command positions (start of string, and after shell separators ; && || |)
# transparently through NAME=VALUE environment-assignment prefixes and env(1)
# invocations — aligning with the sable_is_git_push walk in lib-identity.sh.
#
# If ANY command-position segment resolves to an index-mutating git invocation,
# the function prints the effective repo directory to stdout and returns 0.
# When multiple segments match, the FIRST mutating segment's -C path wins
# (multi-segment chained mutating git commands are rare; using the first keeps
# the scope conservative).
#
# Returns 0 (match, effective-dir printed to stdout) or 1 (no match).
#
# Index-mutating subcommands: add, commit, rm, mv, restore (only when
# --staged/-S is also present), reset (any form).
#
# Effective repo directory:
#   - No -C flags on the matching segment → CWD (passed as argv[2]).
#   - One or more -C flags → accumulated left-to-right, resolved against CWD
#     (relative -C args are joined to the accumulated base; absolute args
#     replace it), matching git's own -C behaviour.
# ---------------------------------------------------------------------------
sable__is_index_mutating_git_command() {
  local cmd="$1"
  local cwd="$2"
  CWD_VAL="$cwd" CMD_STR="$cmd" python3 -c "
import os, re, shlex, sys

cmd = os.environ.get('CMD_STR', '')
cwd = os.environ.get('CWD_VAL', '')

try:
    tokens = shlex.split(cmd)
except ValueError:
    sys.exit(1)

SHELL_SEPS = {';', '&&', '||', '|'}
# git global flags that consume the next token as an argument
CONSUME_NEXT = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path'}
STANDALONE = {
    '--no-pager', '-p', '--paginate', '-P', '--no-replace-objects', '--bare',
    '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs',
    '--icase-pathspecs', '--no-optional-locks', '--html-path', '--man-path',
    '--info-path', '--version', '--help',
}
STANDALONE_PREFIXES = ('--exec-path=', '--git-dir=', '--work-tree=', '--namespace=')
MUTATING = {'add', 'commit', 'rm', 'mv', 'reset'}
ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

def resolve_effective_dir(c_paths, base_cwd):
    \"\"\"Accumulate -C paths left-to-right, resolved against base_cwd.\"\"\"
    cur = base_cwd
    for p in c_paths:
        if os.path.isabs(p):
            cur = p
        else:
            cur = os.path.join(cur, p)
    return cur

# Tracks the shell's working directory as command-position 'cd' segments are
# walked, so a preceding 'cd <dir> &&' shifts the base a later 'git ...'
# segment resolves -C paths (or lack thereof) against — same namespace a
# 'git -C <dir> ...' invocation of the same physical op would land in
# (SABLE-5pci: without this, 'cd <wt> && git add' fell back to the hook's
# original cwd instead of <wt>, landing in the wrong repo's claim file).
shell_cwd = cwd

i = 0
n = len(tokens)
at_cmd_pos = True
while i < n:
    tok = tokens[i]
    # Shell separator -> next token is a new command position
    if tok in SHELL_SEPS:
        at_cmd_pos = True
        i += 1
        continue
    # At command position: transparent NAME=VALUE env-assignment prefix
    if at_cmd_pos and ENV_ASSIGN_RE.match(tok):
        i += 1
        continue
    # At command position: env(1) prefix — consume it and its own options
    if at_cmd_pos and tok == 'env':
        i += 1
        while i < n:
            t = tokens[i]
            if ENV_ASSIGN_RE.match(t):
                i += 1
                continue
            if t == '-u' and i + 1 < n:
                i += 2
                continue
            break
        continue  # re-evaluate tokens[i] still at command position
    # At command position: 'cd' — shifts shell_cwd for subsequent segments
    # in this same command line.
    if at_cmd_pos and tok == 'cd':
        i += 1
        # Skip cd's own flags (-L, -P, ...) but not a lone '-' (previous dir).
        while i < n and tokens[i] not in SHELL_SEPS and tokens[i].startswith('-') and tokens[i] != '-':
            i += 1
        target = None
        if i < n and tokens[i] not in SHELL_SEPS:
            target = tokens[i]
            i += 1
        if target is None:
            shell_cwd = os.environ.get('HOME', shell_cwd)
        elif target == '-':
            pass  # previous dir is unknowable here; leave shell_cwd as-is
        elif os.path.isabs(target):
            shell_cwd = target
        else:
            shell_cwd = os.path.join(shell_cwd, target)
        # Skip any further args on this cd invocation until the next separator
        while i < n and tokens[i] not in SHELL_SEPS:
            i += 1
        at_cmd_pos = False
        continue
    # At command position: found 'git' — walk flags and identify subcommand
    if at_cmd_pos and tok == 'git':
        i += 1
        c_paths = []
        while i < n:
            t = tokens[i]
            if t in SHELL_SEPS:
                break
            if t == '-C' and i + 1 < n:
                c_paths.append(tokens[i + 1])
                i += 2
                continue
            if t in CONSUME_NEXT:
                i += 2
                continue
            if t in STANDALONE or any(t.startswith(p) for p in STANDALONE_PREFIXES):
                i += 1
                continue
            if t == '--':
                i += 1
                break
            if t.startswith('-'):
                i += 1
                continue
            # This is the subcommand
            subcommand = t
            remaining = tokens[i + 1:]
            is_mutating = False
            if subcommand in MUTATING:
                is_mutating = True
            elif subcommand == 'restore':
                for rt in remaining:
                    if rt in ('--staged', '-S'):
                        is_mutating = True
                        break
            if is_mutating:
                eff = resolve_effective_dir(c_paths, shell_cwd)
                print(eff)
                sys.exit(0)
            # Not mutating — this segment is done, continue outer loop
            i += 1
            # Skip rest of this segment's args until a separator
            while i < n and tokens[i] not in SHELL_SEPS:
                i += 1
            at_cmd_pos = False
            break
        # If we exhausted tokens inside the git-flag walk (no subcommand found)
        # or hit a separator — just continue outer loop
        continue
    # Not at command position, or not a recognised command-position token
    at_cmd_pos = False
    i += 1

sys.exit(1)
" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Step 1: Check if this command is index-mutating; capture effective repo dir
# ---------------------------------------------------------------------------
[ -z "$COMMAND" ] && exit 0
EFFECTIVE_DIR=$(sable__is_index_mutating_git_command "$COMMAND" "$CWD") || exit 0
# Normalise: if EFFECTIVE_DIR came back empty for any reason, fall back to CWD
[ -z "$EFFECTIVE_DIR" ] && EFFECTIVE_DIR="$CWD"

# ---------------------------------------------------------------------------
# Step 2: Resolve the claim file (per-checkout via git-dir)
# ---------------------------------------------------------------------------
GIT_DIR=$(git -C "$EFFECTIVE_DIR" rev-parse --git-dir 2>/dev/null) || {
  # Not inside a git repo — fail open
  exit 0
}

# git rev-parse --git-dir returns a relative path when called without -C on
# older git versions; resolve it relative to EFFECTIVE_DIR (the -C target).
case "$GIT_DIR" in
  /*) ;;                             # already absolute
  *)  GIT_DIR="$EFFECTIVE_DIR/$GIT_DIR" ;;
esac

CLAIM_FILE="$GIT_DIR/sable-tree-claim"
TTL="${SABLE_TREE_CLAIM_TTL:-3600}"
NOW=$(date +%s 2>/dev/null) || NOW=0

# market-brief-package-q6yu: helper for every "this session now HOLDS the
# claim" message. Names the claim file + a release reminder — the gap the
# bead was filed for: the claim-TAKER previously learned nothing about where
# the record lives or how to release it (only the deny message, shown to the
# BLOCKED party, named the path). Use `sable-claim status/release
# <repo>` — see bin/sable-claim.
claim_taken_suffix() {
  printf ' Claim file: %s. Release when done: sable-claim release "%s" (from this session/agent), or sable-claim status "%s" to inspect.' \
    "$CLAIM_FILE" "$EFFECTIVE_DIR" "$EFFECTIVE_DIR"
}

# ---------------------------------------------------------------------------
# Step 3: Missing identity — fail open, but never clobber an existing claim
# ---------------------------------------------------------------------------
if [ "$IDENTITY_KNOWN" -eq 0 ]; then
  # Write a claim ONLY when no claim file exists yet.  An existing claim
  # (regardless of holder or age) must never be overwritten by an
  # identity-unknown invocation — doing so would evict the legitimate holder.
  if [ ! -f "$CLAIM_FILE" ]; then
    printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
    allow_with_context "tree-claim: session identity unknowable (PPID=${PPID:-?}); claim written as $SESSION_ID.$(claim_taken_suffix) If two sessions share this checkout, set CLAUDE_SESSION_ID or use 'bd worktree create <name>' for isolation."
  else
    allow_with_context "tree-claim: session identity unknowable (PPID=${PPID:-?}); existing claim preserved (holder: $(awk '{print $1}' "$CLAIM_FILE" 2>/dev/null)). If two sessions share this checkout, set CLAUDE_SESSION_ID or use 'bd worktree create <name>' for isolation."
  fi
fi

# ---------------------------------------------------------------------------
# Step 4: SABLE_TREE_CLAIM_OVERRIDE — allow unconditionally, take over
# ---------------------------------------------------------------------------
if [ "${SABLE_TREE_CLAIM_OVERRIDE:-}" = "1" ]; then
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: override active (SABLE_TREE_CLAIM_OVERRIDE=1). Claim taken over by session $SESSION_ID.$(claim_taken_suffix)"
fi

# ---------------------------------------------------------------------------
# Step 5: Read the existing claim (if any)
# ---------------------------------------------------------------------------
if [ ! -f "$CLAIM_FILE" ]; then
  # No claim — write and allow, naming the path so the new holder can find
  # and release it later without reading hook source (market-brief-package-q6yu).
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: claim taken by session $SESSION_ID.$(claim_taken_suffix)"
fi

# Read claim: "session_id timestamp agent_name"
CLAIM_SESSION=$(awk '{print $1}' "$CLAIM_FILE" 2>/dev/null) || CLAIM_SESSION=""
CLAIM_TS=$(awk '{print $2}' "$CLAIM_FILE" 2>/dev/null) || CLAIM_TS=0

# Unreadable / corrupt claim — fail open
if [ -z "$CLAIM_SESSION" ] || [ -z "$CLAIM_TS" ]; then
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: could not parse existing claim file; took over for session $SESSION_ID.$(claim_taken_suffix)"
fi

# ---------------------------------------------------------------------------
# Step 6: Evaluate claim ownership
# ---------------------------------------------------------------------------
if [ "$CLAIM_SESSION" = "$SESSION_ID" ]; then
  # Own claim — refresh timestamp and allow. Stays silent (no context): this
  # is the common per-command path for a session that already knows it holds
  # the claim, not a "you just took a new claim" event.
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  exit 0
fi

# Foreign claim
CLAIM_AGE=$(( NOW - CLAIM_TS ))
if [ "$CLAIM_AGE" -lt 0 ]; then CLAIM_AGE=0; fi
# market-brief-package-q6yu: attributable name, not just a raw session UUID
# (LIVE EVIDENCE ROUND 2 — an anonymous uuid+epoch record invited a wrong
# release against an ACTIVE holder via timing-correlation misattribution).
CLAIM_AGENT=$(awk '{print $3}' "$CLAIM_FILE" 2>/dev/null) || CLAIM_AGENT=""
[ -z "$CLAIM_AGENT" ] && CLAIM_AGENT="-"

if [ "$CLAIM_AGE" -lt "$TTL" ]; then
  # Fresh foreign claim — deny
  deny_with_reason "tree-claim: index locked by session '$CLAIM_SESSION' (agent '$CLAIM_AGENT', ${CLAIM_AGE}s ago, TTL ${TTL}s). Your session: $SESSION_ID. Escape hatches: (1) set SABLE_TREE_CLAIM_OVERRIDE=1 to take over, or (2) delete $CLAIM_FILE manually and retry, or (3) if you ARE '$CLAIM_AGENT', run: sable-claim release \"$EFFECTIVE_DIR\". If the other session is no longer active, the claim will expire automatically after $((TTL - CLAIM_AGE))s."
else
  # Stale claim — take over and allow
  printf '%s %s %s\n' "$SESSION_ID" "$NOW" "$AGENT_NAME" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: stale claim by '$CLAIM_SESSION' (agent '$CLAIM_AGENT', ${CLAIM_AGE}s old, TTL ${TTL}s) taken over by session $SESSION_ID.$(claim_taken_suffix)"
fi
