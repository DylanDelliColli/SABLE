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

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

# Identity/lane gating via lib-identity.sh (SABLE-uz9.3 / SABLE-4it): governance
# runs for manager-typed subagents (native worker dispatch), legacy manager
# terminals, and the Lincoln main session in execution mode; worker/bare-id
# subagent contexts stand down inside sable_resolve_dispatch_lane. Lane comes
# from identity — the "Dispatching-for:" relay parse is deleted.
# shellcheck source=lib-identity.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib-identity.sh"
sable_resolve_dispatch_lane "$HOOK_INPUT"
[ "$SABLE_DISPATCH_ACTIVE" -eq 1 ] || exit 0

PARSED=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
tool_input = d.get('tool_input', {}) or {}
prompt = tool_input.get('prompt', '')
desc = tool_input.get('description', '')
subtype = tool_input.get('subagent_type', '')
cwd = d.get('cwd', '')
print(f'{subtype}\n{desc}\n{cwd}')
print('---PROMPT---')
print(prompt)
" 2>/dev/null) || exit 0

SUBTYPE=$(echo "$PARSED" | sed -n '1p')
DESC=$(echo "$PARSED" | sed -n '2p')
CWD=$(echo "$PARSED" | sed -n '3p')
PROMPT=$(echo "$PARSED" | sed -n '5,$p')

emit_advisory() { # <text> — emit a single non-blocking additionalContext object
  TEXT="$1" python3 -c "
import json, os
print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': os.environ.get('TEXT', '')}}))
"
}

# ---- Resolve the rebase target (SABLE-uz9.15) ----
# A structured 'Worktree: /abs/path' line in the dispatch prompt is the
# AUTHORITATIVE target and takes priority. Under native dispatch the hook-input
# cwd is the MANAGER's main checkout, not the worker's worktree, so a manager
# that created a per-bundle worktree names it explicitly. cwd / loose inference
# is the fallback only when no usable structured line is present.
ADVISORY=""

WT_LINE=$(printf '%s' "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
m = re.search(r'^[ \t]*Worktree:[ \t]*(\S+)', text, re.MULTILINE)
if not m:
    sys.exit(0)
path = m.group(1).strip().rstrip('\r')
print(('ABS' if path.startswith('/') else 'REL') + '\t' + path)
" 2>/dev/null || echo "")
WT_KIND=$(printf '%s' "$WT_LINE" | cut -f1)
WT_PATH=$(printf '%s' "$WT_LINE" | cut -sf2)

WORKTREE=""
if [ "$WT_KIND" = "ABS" ]; then
  if [ ! -d "$WT_PATH" ]; then
    emit_advisory "PRE-DISPATCH WARNING: Worktree path '$WT_PATH' named in the dispatch prompt was not found on disk; skipping rebase (failing open). Create it with 'bd worktree create' before dispatching this worker."
    exit 0
  fi
  # Explicit work target — do not apply the exploration skip.
  WORKTREE="$WT_PATH"
elif [ "$WT_KIND" = "REL" ]; then
  ADVISORY="PRE-DISPATCH WARNING: Worktree line must be an absolute path, got '$WT_PATH'; ignoring it and falling back to the dispatch cwd."
  WORKTREE="$CWD"
else
  # No structured line: skip genuine read-only agents, then infer from the
  # prompt, then fall back to cwd.
  echo "$SUBTYPE" | grep -qiE '^(Explore|Plan|claude-code-guide)$' && exit 0
  echo "$DESC" | grep -qiE '(explore|research|search|find|read.only|investigate|audit)' && exit 0
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
  [ -z "$WORKTREE" ] && WORKTREE="$CWD"
fi

# Target must be an existing git worktree; otherwise stand down (surfacing any
# pending advisory).
if [ -z "$WORKTREE" ] || [ ! -d "$WORKTREE" ] || { [ ! -d "$WORKTREE/.git" ] && [ ! -f "$WORKTREE/.git" ]; }; then
  [ -n "$ADVISORY" ] && emit_advisory "$ADVISORY"
  exit 0
fi

# Validate base ref and fall back gracefully when SABLE_BASE_BRANCH points to
# a ref that doesn't exist in this repo (SABLE-61n)
BASE_BRANCH=$(sable_validate_base_ref "$WORKTREE" "${SABLE_BASE_BRANCH:-origin/main}")

# Fetch and rebase. Capture output for reporting.
FETCH_OUT=$(git -C "$WORKTREE" fetch origin 2>&1 || echo "FETCH_FAILED: $?")
REBASE_OUT=$(git -C "$WORKTREE" rebase "$BASE_BRANCH" 2>&1 || echo "REBASE_FAILED")

if echo "$REBASE_OUT" | grep -q "REBASE_FAILED"; then
  # Abort the half-done rebase so the worktree isn't left in conflict state
  git -C "$WORKTREE" rebase --abort 2>/dev/null || true
  REBASE_MSG="PRE-DISPATCH WARNING: rebase of $WORKTREE on $BASE_BRANCH failed and was aborted. Resolve manually before dispatching this worker, or dispatch into a fresh worktree. Output:
$(printf '%s' "$REBASE_OUT" | head -c 500)"
  [ -n "$ADVISORY" ] && REBASE_MSG="$ADVISORY
$REBASE_MSG"
  emit_advisory "$REBASE_MSG"
  exit 0
fi

# Success: surface any pending advisory (e.g. relative-path fallback), else silent.
[ -n "$ADVISORY" ] && emit_advisory "$ADVISORY"
exit 0
