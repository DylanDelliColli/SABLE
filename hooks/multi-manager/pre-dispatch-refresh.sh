#!/usr/bin/env bash
# pre-dispatch-refresh.sh — Rebase target worktree on $SABLE_BASE_BRANCH before dispatch
# Trigger: PreToolUse:Agent | Timeout: 30000ms
#
# Before the worker starts, refresh its working tree against the integration branch.
# Eliminates "30-min-old base" conflicts at the source.
#
# $SABLE_BASE_BRANCH defaults to origin/main; export per-repo to override (e.g. origin/dev).
#
# Skips if dispatch is for an exploration agent (Explore/Plan/research subagents
# don't modify code) or if no worktree path can be inferred from the prompt.
#
# This hook does not block — if rebase fails, the failure is reported as
# additionalContext for the manager to handle, but dispatch is allowed.

set -euo pipefail

[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
tool_input = d.get('tool_input', {})
prompt = tool_input.get('prompt', '')
desc = tool_input.get('description', '')
subtype = tool_input.get('subagent_type', '')
agent_id = d.get('agent_id', '')
cwd = d.get('cwd', '')
print(f'{agent_id}\n{subtype}\n{desc}\n{cwd}')
print('---PROMPT---')
print(prompt)
" 2>/dev/null) || exit 0

NESTED_AGENT_ID=$(echo "$PARSED" | sed -n '1p')
SUBTYPE=$(echo "$PARSED" | sed -n '2p')
DESC=$(echo "$PARSED" | sed -n '3p')
CWD=$(echo "$PARSED" | sed -n '4p')
PROMPT=$(echo "$PARSED" | sed -n '6,$p')

# Skip if this dispatch is happening from inside a subagent (not a manager dispatch)
[ -n "$NESTED_AGENT_ID" ] && exit 0

# Skip exploration / read-only subagents
echo "$SUBTYPE" | grep -qiE '^(Explore|Plan|claude-code-guide|general-purpose)$' && exit 0
echo "$DESC" | grep -qiE '(explore|research|search|find|read.only|investigate|audit)' && exit 0

# Locate worktree path from prompt — look for "worktree" or "cd /path" or "in <path>"
WORKTREE=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
patterns = [
    r'worktree(?:\s+(?:at|in|path))?\s*[:=]?\s*([^\s\n\"\\']+)',
    r'(?:cd|in)\s+(/[^\s\n\"\\']+|\\.\\./[^\s\n]+|\\./[^\s\n]+)',
    r'working in\s+([^\s\n\"\\']+)',
]
for p in patterns:
    m = re.search(p, text, re.IGNORECASE)
    if m:
        print(m.group(1))
        sys.exit(0)
" 2>/dev/null || echo "")

# Fall back to current working directory if no worktree explicit
[ -z "$WORKTREE" ] && WORKTREE="$CWD"
[ -z "$WORKTREE" ] && exit 0
[ ! -d "$WORKTREE" ] && exit 0
[ ! -d "$WORKTREE/.git" ] && [ ! -f "$WORKTREE/.git" ] && exit 0

BASE_BRANCH="${SABLE_BASE_BRANCH:-origin/main}"

# Fetch and rebase. Capture output for reporting.
FETCH_OUT=$(git -C "$WORKTREE" fetch origin 2>&1 || echo "FETCH_FAILED: $?")
REBASE_OUT=$(git -C "$WORKTREE" rebase "$BASE_BRANCH" 2>&1 || echo "REBASE_FAILED")

if echo "$REBASE_OUT" | grep -q "REBASE_FAILED"; then
  # Abort the half-done rebase so the worktree isn't left in conflict state
  git -C "$WORKTREE" rebase --abort 2>/dev/null || true

  WORKTREE="$WORKTREE" BASE_BRANCH="$BASE_BRANCH" REBASE_OUT="$REBASE_OUT" python3 -c "
import json, os
wt = os.environ.get('WORKTREE', '')
bb = os.environ.get('BASE_BRANCH', '')
out = os.environ.get('REBASE_OUT', '')[:500]
print(json.dumps({
    'additionalContext': f'PRE-DISPATCH WARNING: rebase of {wt} on {bb} failed and was aborted. Resolve manually before dispatching this worker, or dispatch into a fresh worktree. Output:\n{out}'
}))
"
  exit 0
fi

# Success: silent
exit 0
