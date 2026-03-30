#!/usr/bin/env bash
# agent-tdd-enforce.sh — Inject TDD boilerplate into Agent dispatch prompts
# Trigger: PreToolUse on Agent | Timeout: 3000ms
#
# When dispatching an Agent for implementation work (not research/exploration),
# checks if the prompt contains TDD keywords. If missing, injects a TDD
# reminder as additional context.

set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
tool_name = d.get('tool_name', '')
prompt = d.get('tool_input', {}).get('prompt', '')
desc = d.get('tool_input', {}).get('description', '')
subtype = d.get('tool_input', {}).get('subagent_type', '')
print(f'{tool_name}\n{subtype}\n{desc}')
print('---PROMPT---')
print(prompt)
" 2>/dev/null) || exit 0

TOOL_NAME=$(echo "$PARSED" | sed -n '1p')
SUBTYPE=$(echo "$PARSED" | sed -n '2p')
DESC=$(echo "$PARSED" | sed -n '3p')
PROMPT=$(echo "$PARSED" | sed -n '5,$p')

# Only act on Agent tool
[ "$TOOL_NAME" = "Agent" ] || exit 0

# Skip exploration/research agents — they don't write code
echo "$SUBTYPE" | grep -qiE '^(Explore|Plan|claude-code-guide)$' && exit 0
echo "$DESC" | grep -qiE '(explore|research|search|find|read|check|investigate)' && exit 0

# Skip if not implementation work (no code-related keywords)
echo "$PROMPT" | grep -qiE '(implement|create|write|modify|add|fix|refactor|update|build)' || exit 0

# Check for TDD keywords in the prompt
if echo "$PROMPT" | grep -qiE '(TDD|test.first|failing.test|RED.*GREEN|red-green|write.test|test.before)'; then
  exit 0  # TDD instructions present — allow silently
fi

# Check for [no-test] escape hatch
if echo "$PROMPT" | grep -qiE '\[no-test\]'; then
  exit 0  # Explicitly opted out — allow
fi

# Missing TDD instructions — inject reminder
python3 -c "
import json
print(json.dumps({
    'additionalContext': '''SABLE TDD ENFORCEMENT: This agent dispatch is for implementation work but contains no TDD instructions.

You MUST include TDD Red-Green-Refactor instructions in the agent prompt:
1. Write the failing test FIRST
2. Run it to confirm RED (fails for the expected reason)
3. Write minimal implementation to make it pass
4. Run it to confirm GREEN
5. Run the full test suite before closing

If this is a non-code task (docs, config, UI-only), add [no-test] to the bead notes.
If this is research/exploration, use subagent_type=Explore instead.'''
}))
"
