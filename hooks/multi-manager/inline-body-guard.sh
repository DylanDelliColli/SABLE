#!/usr/bin/env bash
# inline-body-guard.sh — PreToolUse:Bash guard: refuse a bd/sable-msg
# invocation that carries PROSE with an unescaped backtick or dollar-paren
# (SABLE-qwthx).
# Trigger: PreToolUse:Bash | Timeout: 3000ms
#
# THE INCIDENT THIS CLOSES. An agent-composed Bash command carrying a bead
# description backtick-quoted a command NAME, the ordinary markdown way to
# render one. The CALLING shell command-substituted it before `bd` ever saw
# the argument: `bd hooks install` actually ran, mid-promote, rewriting five
# TRACKED git hook files. `bd create --body-file` existed on the very command
# being invoked and was not used — an escape hatch that must be remembered
# under time pressure is not a control, and three independent agents hit this
# class four times in one shift, every one of them already knowing the rule
# at the moment they broke it. See SABLE-qwthx for the full record.
#
# WHY A HOOK AND NOT ANOTHER DISCIPLINE PASS. The bead's own second instance
# (a backtick in a `sable-msg` body, not a `bd` write) defeated a mitigation
# scoped to "bd write commands" — a name-list is exactly the shape that
# already failed. This hook does not decide by discipline; it inspects the
# actual command line for the actual hazard, at the only moment prevention is
# still possible.
#
# PATTERN LOGIC LIVES IN bin/sable_inline_body_guard_lib.py so it is
# pytest-testable (bin/test_sable_inline_body_guard.py) independent of this
# shell wrapper. NEVER landed in .beads/hooks (bd-managed territory a stray
# `bd hooks install` — this bead's own incident — silently reverts; see
# SABLE-qwthx's ACTIVATION-SURFACE TRAP note). Wired via
# templates/multi-manager/settings-snippet.json instead.
#
# FAIL-OPEN, BUT NEVER SILENTLY (Standing Discipline 7): when the guard
# cannot locate its own pattern-logic library (an installed ~/.claude/hooks
# copy with no bin/ sibling — install.sh COPIES hooks; it does not ship
# bin/sable_inline_body_guard_lib.py alongside them, since sable-bin-install
# only symlinks *.py-less sable-* entrypoints onto PATH), it ALLOWS the
# command through but says so loudly via additionalContext rather than
# quietly passing everything.

set -uo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

COMMAND=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print((d.get('tool_input') or {}).get('command', '') or '')
" 2>/dev/null) || COMMAND=""

[ -z "$COMMAND" ] && exit 0

deny_with_reason() {
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

allow_with_context() {
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

# Resolve the pattern-logic library: the repo-relative sibling of THIS file
# first (the dev checkout / any linked worktree case — every worktree of a
# project carries its own bin/, no git-common-dir indirection needed since
# this hook and bin/ ship in the same tree), and nothing else — there is no
# PATH-installed form of a *.py lib (sable-bin-install deliberately skips
# *.py; see the header note above).
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
LIB_PATH="${HOOK_DIR}/../../bin/sable_inline_body_guard_lib.py"

if [ ! -f "$LIB_PATH" ]; then
  allow_with_context "inline-body-guard: COULD NOT ASSESS — bin/sable_inline_body_guard_lib.py not found alongside this installed hook copy. ALLOWING (fail-open), but no backtick/dollar-paren check ran on this command. SABLE-qwthx: an unescaped backtick or \$(...) in a bd/sable-msg prose argument gets command-substituted and EXECUTED by the calling shell before the intended command ever sees it."
fi

if ! RESULT=$(printf '%s' "$COMMAND" | python3 "$LIB_PATH" 2>&1); then
  allow_with_context "inline-body-guard: COULD NOT ASSESS — bin/sable_inline_body_guard_lib.py exited non-zero. ALLOWING (fail-open), but no backtick/dollar-paren check ran on this command (SABLE-qwthx). Library output: $(printf '%s' "$RESULT" | head -3)"
fi

VERDICT="$(printf '%s\n' "$RESULT" | head -1)"

if [ "$VERDICT" = "REFUSE" ]; then
  REASON="$(printf '%s\n' "$RESULT" | tail -n +2)"
  [ -z "$REASON" ] && REASON="inline-body-guard: DENIED — this command carries an unescaped backtick or dollar-paren in a prose argument (SABLE-qwthx)."
  deny_with_reason "$REASON"
fi

exit 0
