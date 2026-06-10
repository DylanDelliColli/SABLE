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
# sable__is_index_mutating_git_command <command>
#
# Tokenises the command with Python shlex (handles quoting), skips any git
# global flags, then checks whether the first positional argument is one of
# the index-mutating subcommands.
#
# Returns 0 (match) or 1 (no match).
#
# Index-mutating subcommands: add, commit, rm, mv, restore (only when
# --staged is also present), reset (any form).
# ---------------------------------------------------------------------------
sable__is_index_mutating_git_command() {
  local cmd="$1"
  python3 - "$cmd" <<'PYEOF'
import sys, shlex

CMD = sys.argv[1]
try:
    tokens = shlex.split(CMD)
except ValueError:
    sys.exit(1)

if not tokens or tokens[0] != 'git':
    sys.exit(1)

# Skip git global flags: -C <path>, -c <k=v>, --git-dir=..., --work-tree=...,
# --no-pager, -p, --no-optional-locks, --exec-path, --html-path, --man-path,
# --info-path, --version, --help, --bare, --namespace=..., --super-prefix=...
# Generically: skip any token starting with '-' and its value if the flag
# takes a mandatory argument. We use a simple approach: if a token starts
# with '-', advance past it. For flags known to consume a following arg
# (-C, -c, --git-dir, --work-tree, --exec-path, --namespace, --super-prefix)
# also advance past their value.
TAKES_ARG = {'-C', '-c', '--git-dir', '--work-tree', '--exec-path',
             '--namespace', '--super-prefix'}

idx = 1  # skip 'git'
while idx < len(tokens):
    t = tokens[idx]
    if t == '--':
        idx += 1
        break
    if not t.startswith('-'):
        break
    idx += 1
    # If the flag takes a following argument (no '='), skip the next token too
    if t in TAKES_ARG:
        idx += 1

if idx >= len(tokens):
    sys.exit(1)

subcommand = tokens[idx]
remaining = tokens[idx+1:]

MUTATING = {'add', 'commit', 'rm', 'mv', 'reset'}
if subcommand in MUTATING:
    sys.exit(0)

if subcommand == 'restore':
    # Only index-mutating when --staged (or -S) is present
    for t in remaining:
        if t in ('--staged', '-S'):
            sys.exit(0)
    sys.exit(1)

sys.exit(1)
PYEOF
}

# ---------------------------------------------------------------------------
# Step 1: Check if this command is index-mutating
# ---------------------------------------------------------------------------
[ -z "$COMMAND" ] && exit 0
sable__is_index_mutating_git_command "$COMMAND" || exit 0

# ---------------------------------------------------------------------------
# Step 2: Resolve the claim file (per-checkout via git-dir)
# ---------------------------------------------------------------------------
GIT_DIR=$(git -C "$CWD" rev-parse --git-dir 2>/dev/null) || {
  # Not inside a git repo — fail open
  exit 0
}

# git rev-parse --git-dir returns a relative path when called without -C on
# older git versions; resolve it relative to CWD.
case "$GIT_DIR" in
  /*) ;;                             # already absolute
  *)  GIT_DIR="$CWD/$GIT_DIR" ;;
esac

CLAIM_FILE="$GIT_DIR/sable-tree-claim"
TTL="${SABLE_TREE_CLAIM_TTL:-3600}"
NOW=$(date +%s 2>/dev/null) || NOW=0

# ---------------------------------------------------------------------------
# Step 3: Missing identity — fail open
# ---------------------------------------------------------------------------
if [ "$IDENTITY_KNOWN" -eq 0 ]; then
  # Write claim anyway so we're not completely invisible, then allow
  printf '%s %s\n' "$SESSION_ID" "$NOW" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: session identity unknowable (PPID=${PPID:-?}); claim written as $SESSION_ID. If two sessions share this checkout, set CLAUDE_SESSION_ID or use 'bd worktree create <name>' for isolation."
fi

# ---------------------------------------------------------------------------
# Step 4: SABLE_TREE_CLAIM_OVERRIDE — allow unconditionally, take over
# ---------------------------------------------------------------------------
if [ "${SABLE_TREE_CLAIM_OVERRIDE:-}" = "1" ]; then
  printf '%s %s\n' "$SESSION_ID" "$NOW" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: override active (SABLE_TREE_CLAIM_OVERRIDE=1). Claim taken over by session $SESSION_ID."
fi

# ---------------------------------------------------------------------------
# Step 5: Read the existing claim (if any)
# ---------------------------------------------------------------------------
if [ ! -f "$CLAIM_FILE" ]; then
  # No claim — write and allow
  printf '%s %s\n' "$SESSION_ID" "$NOW" > "$CLAIM_FILE" 2>/dev/null || true
  exit 0
fi

# Read claim: "session_id timestamp"
CLAIM_SESSION=$(awk '{print $1}' "$CLAIM_FILE" 2>/dev/null) || CLAIM_SESSION=""
CLAIM_TS=$(awk '{print $2}' "$CLAIM_FILE" 2>/dev/null) || CLAIM_TS=0

# Unreadable / corrupt claim — fail open
if [ -z "$CLAIM_SESSION" ] || [ -z "$CLAIM_TS" ]; then
  printf '%s %s\n' "$SESSION_ID" "$NOW" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: could not parse existing claim file; took over for session $SESSION_ID."
fi

# ---------------------------------------------------------------------------
# Step 6: Evaluate claim ownership
# ---------------------------------------------------------------------------
if [ "$CLAIM_SESSION" = "$SESSION_ID" ]; then
  # Own claim — refresh timestamp and allow
  printf '%s %s\n' "$SESSION_ID" "$NOW" > "$CLAIM_FILE" 2>/dev/null || true
  exit 0
fi

# Foreign claim
CLAIM_AGE=$(( NOW - CLAIM_TS ))
if [ "$CLAIM_AGE" -lt 0 ]; then CLAIM_AGE=0; fi

if [ "$CLAIM_AGE" -lt "$TTL" ]; then
  # Fresh foreign claim — deny
  deny_with_reason "tree-claim: index locked by session '$CLAIM_SESSION' (${CLAIM_AGE}s ago, TTL ${TTL}s). Your session: $SESSION_ID. Escape hatches: (1) set SABLE_TREE_CLAIM_OVERRIDE=1 to take over, or (2) delete $CLAIM_FILE manually and retry. If the other session is no longer active, the claim will expire automatically after $((TTL - CLAIM_AGE))s."
else
  # Stale claim — take over and allow
  printf '%s %s\n' "$SESSION_ID" "$NOW" > "$CLAIM_FILE" 2>/dev/null || true
  allow_with_context "tree-claim: stale claim by '$CLAIM_SESSION' (${CLAIM_AGE}s old, TTL ${TTL}s) taken over by session $SESSION_ID."
fi
