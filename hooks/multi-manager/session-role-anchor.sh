#!/usr/bin/env bash
# session-role-anchor.sh — Inject role identity from registry on SessionStart/PreCompact
# Trigger: SessionStart, PreCompact | Timeout: 3000ms
#
# Reads $CLAUDE_AGENT_NAME, locates the role file at ~/.claude/sable/roles/<name>.md,
# and injects its contents as additionalContext. Anchors identity at session start
# and re-anchors after compaction (identity erodes silently otherwise).
#
# Fast-exits if env var is unset (non-manager sessions unaffected).

set -euo pipefail

# Manager identity must be set explicitly via launch alias
[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0

ROLE_FILE="$HOME/.claude/sable/roles/${CLAUDE_AGENT_NAME}.md"
[ ! -f "$ROLE_FILE" ] && exit 0

ROLE_CONTENT=$(cat "$ROLE_FILE")

ROLE_CONTENT="$ROLE_CONTENT" python3 -c "
import json, os, sys
content = os.environ.get('ROLE_CONTENT', '')
name = os.environ.get('CLAUDE_AGENT_NAME', '').upper()

# Detect which event fired us (SessionStart or PreCompact) so we emit the
# correct hookEventName in hookSpecificOutput. Claude Code silently drops
# additionalContext payloads if the wrapper/event name is missing or wrong.
try:
    hook_input = json.load(sys.stdin)
    event = hook_input.get('hook_event_name', 'SessionStart')
except Exception:
    event = 'SessionStart'

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': event,
        'additionalContext': f'=== AGENT IDENTITY: {name} ===\n\n{content}\n\n=== END IDENTITY ===\n\nYou are {name}. Operate within this role. Do not act as another manager.'
    }
}))
"
