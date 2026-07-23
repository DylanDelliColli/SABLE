#!/usr/bin/env bash
# session-role-anchor.sh — Inject role identity from registry on SessionStart/PreCompact
# Trigger: SessionStart, PreCompact | Timeout: 3000ms
#
# Reads $CLAUDE_AGENT_NAME, locates the role file at ~/.claude/sable/roles/<name>.md,
# and injects its contents as additionalContext. Anchors identity at session start
# and re-anchors after compaction via the SessionStart:compact leg (identity
# erodes silently otherwise). The PreCompact trigger itself is a no-op — its
# hookSpecificOutput schema doesn't support additionalContext (SABLE-jiqm).
#
# Fast-exits if env var is unset (non-manager sessions unaffected).

set -euo pipefail

# SABLE-38zi: a WORKER pane carries the lane manager's CLAUDE_AGENT_NAME +
# manager role so its push fires the manager-gated for-chuck handoff — but it
# must NEVER load that manager's role-card. A worker that boots as its manager
# runs the manager operating loop and re-dispatches its own bead (duplicate
# pane, defeats SABLE_MAX_WORKERS). sable-spawn-worker stamps SABLE_WORKER_PANE=1
# on every worker pane precisely to distinguish it from a real manager pane that
# shares the same identity env; stand down unconditionally when it is set.
[ -n "${SABLE_WORKER_PANE:-}" ] && exit 0

# Manager identity must be set explicitly via launch alias
[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0

# Resolve the role PROJECT-FIRST (a project-scoped orchestration install lives in
# ./.claude) then fall back to the user-level install (~/.claude).
_PROJECT_CAND="$PWD/.claude/sable/roles/${CLAUDE_AGENT_NAME}.md"
_USER_CAND="$HOME/.claude/sable/roles/${CLAUDE_AGENT_NAME}.md"
ROLE_FILE=""
for _cand in "$_PROJECT_CAND" "$_USER_CAND"; do
    if [ -f "$_cand" ]; then ROLE_FILE="$_cand"; break; fi
done
[ -z "$ROLE_FILE" ] && exit 0

# SABLE-thx70: LOUD ON SHADOWING. Project-first precedence is a supported
# feature (the comment above), but a stale project-local copy silently
# outranking a freshly-edited user-level one, forever, with no event, is not
# -- it defeated six days of role-card edits fleet-wide before anyone noticed
# (measured on this bead's own optimus.md: 6 bead ids present ONLY in the
# user-level copy, never read). Precedence is UNCHANGED here; this only makes
# the disagreement visible. Fires ONLY when both candidates exist AND differ
# in content -- identical content, or only one candidate present, is the
# ordinary case and must stay silent or every boot would warn.
if [ -f "$_PROJECT_CAND" ] && [ -f "$_USER_CAND" ] && ! cmp -s "$_PROJECT_CAND" "$_USER_CAND"; then
    _proj_mtime=$(stat -c '%y' "$_PROJECT_CAND" 2>/dev/null || stat -f '%Sm' "$_PROJECT_CAND" 2>/dev/null || echo unknown)
    _user_mtime=$(stat -c '%y' "$_USER_CAND" 2>/dev/null || stat -f '%Sm' "$_USER_CAND" 2>/dev/null || echo unknown)
    printf 'SABLE-ROLE-CARD-SHADOWED: role card for %s differs between the project-local and user-level installs. Precedence is unchanged -- the project-local copy WINS -- but the user-level copy is being silently ignored:\n  WINNER   (project-local): %s (mtime: %s)\n  SHADOWED (user-level):    %s (mtime: %s)\nIf the user-level edit was intentional, it will never be read while the project-local copy exists.\n' \
        "$CLAUDE_AGENT_NAME" "$_PROJECT_CAND" "$_proj_mtime" "$_USER_CAND" "$_user_mtime" >&2
fi

ROLE_CONTENT=$(cat "$ROLE_FILE")

# --- Live protocol state surface (SABLE-9ozz) -------------------------------
# A pane restart (/clear, crash, session limit) silently reverts this session to
# the STATIC role card above and loses every conversation-state convention — the
# merge-gate sole-path contract, interim fleet caps, the manual-relay rule while
# a hook is dark. Those lived only in the previous conversation's context, which
# the restart destroyed (the 2026-07-13 gah9 bypass: chuck's static identity
# still described the OLD manual-merge flow). Surface the LIVE protocol state
# from disk — the orchestration mode-state + an active-contracts file colocated
# with it — so a fresh boot reconciles against the current contract, not the
# historical identity. Empty surfaces => byte-identical to the legacy identity-
# only injection (nothing extra emitted). This is ALSO how fix-direction-2's
# boot reconciliation instruction reaches every manager at once, without editing
# each role card.
LIVE_MODE=""
LIVE_CONTRACTS=""
_lmp="$(dirname "${BASH_SOURCE[0]:-$0}")/lib-mode-path.sh"
if [ -f "$_lmp" ]; then
    # shellcheck source=lib-mode-path.sh
    . "$_lmp" 2>/dev/null || true
fi
if command -v sable_mode_state_path >/dev/null 2>&1; then
    _mode_state="$(sable_mode_state_path "$PWD" 2>/dev/null || true)"
else
    _mode_state="${SABLE_MODE_STATE:-}"
fi
if [ -n "${_mode_state:-}" ] && [ -f "$_mode_state" ]; then
    LIVE_MODE="$(STATE="$_mode_state" python3 -c '
import json, os
try:
    d = json.load(open(os.environ["STATE"]))
except Exception:
    raise SystemExit(0)
m = d.get("mode", "")
if not m:
    raise SystemExit(0)
line = m
sub = d.get("substage")
if sub:
    line += " / " + sub
since = d.get("since")
if since:
    line += " (since " + since + ")"
print(line)
' 2>/dev/null || true)"
fi
# The contracts surface is colocated with the mode-state file; SABLE_ACTIVE_CONTRACTS
# overrides the file directly (parallel to SABLE_MODE_STATE for mode-state).
if [ -n "${SABLE_ACTIVE_CONTRACTS:-}" ]; then
    _contracts_file="$SABLE_ACTIVE_CONTRACTS"
elif [ -n "${_mode_state:-}" ]; then
    _contracts_file="$(dirname "$_mode_state")/active-contracts.md"
else
    _contracts_file=""
fi
if [ -n "${_contracts_file:-}" ] && [ -s "$_contracts_file" ]; then
    LIVE_CONTRACTS="$(cat "$_contracts_file" 2>/dev/null || true)"
fi

ROLE_CONTENT="$ROLE_CONTENT" LIVE_MODE="$LIVE_MODE" LIVE_CONTRACTS="$LIVE_CONTRACTS" python3 -c "
import json, os, sys
content = os.environ.get('ROLE_CONTENT', '')
name = os.environ.get('CLAUDE_AGENT_NAME', '').upper()
live_mode = os.environ.get('LIVE_MODE', '').strip()
live_contracts = os.environ.get('LIVE_CONTRACTS', '').strip()

# Detect which event fired us (SessionStart or PreCompact) so we emit the
# correct hookEventName in hookSpecificOutput. Claude Code silently drops
# additionalContext payloads if the wrapper/event name is missing or wrong.
try:
    hook_input = json.load(sys.stdin)
    event = hook_input.get('hook_event_name', 'SessionStart')
except Exception:
    event = 'SessionStart'

# SABLE-jiqm: PreCompact's hookSpecificOutput schema does not support
# additionalContext (only UserPromptSubmit/PostToolUse/PostToolBatch/Stop do) —
# emitting it here fails Claude Code's hook JSON validation on every /compact,
# silently losing the re-anchor. Re-anchoring already happens via the
# SessionStart:compact leg (this same script, fired again post-compaction with
# hook_event_name still 'SessionStart'), so no-op here instead of emitting an
# invalid shape.
if event == 'PreCompact':
    sys.exit(0)

identity = (
    f'=== AGENT IDENTITY: {name} ===\n\n{content}\n\n=== END IDENTITY ===\n\n'
    f'You are {name}. Operate within this role. Do not act as another manager.'
)

# Only append the live-protocol block when there IS live state on disk, so a
# non-orchestration manager session stays byte-identical to the legacy injection.
if live_mode or live_contracts:
    parts = [
        '',
        '',
        '=== LIVE PROTOCOL STATE (SABLE-9ozz — read from disk at SessionStart) ===',
        '',
        'This block reflects the CURRENT fleet protocol. A pane restart (/clear,',
        'crash, session limit) silently reverted you to the STATIC identity above,',
        'which is only a HISTORICAL baseline. Where this live state conflicts with',
        'that identity, THIS WINS.',
        '',
    ]
    if live_mode:
        parts.append('Orchestration mode: ' + live_mode)
        parts.append('')
    if live_contracts:
        parts.append('Active contracts:')
        parts.append(live_contracts)
        parts.append('')
    parts.append('Boot reconciliation (do this BEFORE your first action): reconcile the')
    parts.append('above against your static identity, then run  bd memories  and read your')
    parts.append('for-<role> inbox. Only then act.')
    parts.append('')
    parts.append('=== END LIVE PROTOCOL STATE ===')
    identity += '\n'.join(parts)

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': event,
        'additionalContext': identity,
    }
}))
"
