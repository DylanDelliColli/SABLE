#!/usr/bin/env bash
# tdd-evidence.sh — Silent logger for test command evidence
# Fires on every PreToolUse:Bash, writes only when it detects test commands.
# Partner to tdd-gate.sh which checks the evidence file on bd close.

set -euo pipefail

# Read stdin and parse with python3 (jq not available)
PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
sid = d.get('session_id', '')
aid = d.get('agent_id', '') or ''
print(f'{sid}\n{aid}\n{cmd}')
" 2>/dev/null) || exit 0

SESSION_ID=$(echo "$PARSED" | sed -n '1p')
AGENT_ID=$(echo "$PARSED" | sed -n '2p')
COMMAND=$(echo "$PARSED" | sed -n '3p')

[ -z "$COMMAND" ] && exit 0
[ -z "$SESSION_ID" ] && exit 0

# Match test runner commands. The bash-harness pattern matches any
# `bash <path>test-<name>.sh` invocation — covers SABLE's hook tests
# (hooks/test/test-*.sh) plus any project that names test scripts test-*.sh.
# The character class [^&|;] prevents `&&`/`||`/`;` chains from making the
# match leak into the wrong half of a compound command.
if echo "$COMMAND" | grep -qE '(vitest|pytest|npm test|npx vitest|python -m pytest|python3? [^&|;]*test_[A-Za-z0-9_-]+\.py|bash [^&|;]*test-[A-Za-z0-9_-]+\.sh)'; then
  # Per-agent keying (SABLE-d72): session_id is SHARED across the whole nested
  # agent tree, so key by session_id + agent_id when a subagent ran the tests.
  # Main sessions (no agent_id) keep the session-global file unchanged.
  if [ -n "$AGENT_ID" ]; then
    EVIDENCE_FILE="/tmp/tdd-evidence-${SESSION_ID}-${AGENT_ID}"
  else
    EVIDENCE_FILE="/tmp/tdd-evidence-${SESSION_ID}"
  fi
  echo "$(date -Iseconds) $COMMAND" >> "$EVIDENCE_FILE"
fi

exit 0
