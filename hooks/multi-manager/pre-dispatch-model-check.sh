#!/usr/bin/env bash
# pre-dispatch-model-check.sh — Enforce worker model selection consistency
# Trigger: PreToolUse:Agent | Timeout: 5000ms
#
# Validates that the manager's chosen worker model matches the bead's
# `model:<haiku|sonnet|opus>` label. The bead label is the primary signal;
# the manager's dispatch must match it OR explicitly override with a reason.
#
# Behavior:
#   1. Parse bead ID(s) from dispatch prompt.
#   2. For each, query bd for `model:` label.
#   3. Compare against tool_input.model:
#      - Match → allow silently
#      - Mismatch + dispatch prompt contains "Model override:" line → allow
#        (override reason is logged in the prompt for audit)
#      - Mismatch without override → DENY
#   4. If bead has no `model:` label AND tool_input.model is unspecified → DENY
#      (force the manager to apply the ladder rather than default silently)
#   5. If bead has no label but tool_input.model IS specified → allow, suggest
#      adding the label via additionalContext (so next dispatch doesn't
#      re-litigate)
#
# Skips for:
#   - Subagent context (NESTED_AGENT_ID present — workers don't dispatch peers)
#   - Read-only / exploration subagent types (Explore, Plan, etc. — model is
#     manager's call, not bead-driven)
#   - Non-manager sessions
#
# See:
#   templates/worker-dispatch.md (Model selection ladder, override syntax)
#   roles/optimus.md, roles/tarzan.md (per-manager ladder embed)

set -euo pipefail

[ -z "${CLAUDE_AGENT_NAME:-}" ] && exit 0
[ "${CLAUDE_AGENT_ROLE:-}" != "manager" ] && exit 0

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
tool_input = d.get('tool_input', {})
prompt = tool_input.get('prompt', '')
subtype = tool_input.get('subagent_type', '')
model = tool_input.get('model', '')
agent_id = d.get('agent_id', '')
print(agent_id)
print(subtype)
print(model)
print('---PROMPT---')
print(prompt)
" 2>/dev/null) || exit 0

NESTED_AGENT_ID=$(echo "$PARSED" | sed -n '1p')
SUBTYPE=$(echo "$PARSED" | sed -n '2p')
DISPATCH_MODEL=$(echo "$PARSED" | sed -n '3p')
PROMPT=$(echo "$PARSED" | sed -n '5,$p')

# Skip subagent context — workers don't dispatch peers
[ -n "$NESTED_AGENT_ID" ] && exit 0

# Skip read-only / exploration agents — model is manager's call
echo "$SUBTYPE" | grep -qiE '^(Explore|Plan|claude-code-guide|general-purpose|feature-dev:code-explorer)$' && exit 0

# Skip if dispatch prompt indicates investigation/exploration (manager judgment)
echo "$PROMPT" | grep -qiE '^Task: (explore|investigate|research|audit|read-only)' && exit 0

# Extract bead IDs from prompt
BEAD_IDS=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
ids = set(re.findall(r'\b((?:bd|sable|twine|epic|task|bug|feat)-[a-zA-Z0-9_-]+)\b', text, re.IGNORECASE))
for i in sorted(ids):
    print(i)
" 2>/dev/null)

# Check for explicit override line in prompt
HAS_OVERRIDE=0
if echo "$PROMPT" | grep -qiE '^Model override:[[:space:]]+\S'; then
  HAS_OVERRIDE=1
fi

# Normalize dispatch model to one of haiku/sonnet/opus (or empty)
NORMALIZED_DISPATCH=""
case "$DISPATCH_MODEL" in
  haiku|*-haiku-*|*-haiku) NORMALIZED_DISPATCH="haiku" ;;
  sonnet|*-sonnet-*|*-sonnet) NORMALIZED_DISPATCH="sonnet" ;;
  opus|*-opus-*|*-opus) NORMALIZED_DISPATCH="opus" ;;
  "") NORMALIZED_DISPATCH="" ;;
  *) NORMALIZED_DISPATCH="unknown" ;;
esac

# If no bead IDs in prompt: dispatch is ad-hoc / non-bead-driven.
# Still require an explicit model so manager applies the ladder.
if [ -z "$BEAD_IDS" ]; then
  if [ -z "$NORMALIZED_DISPATCH" ]; then
    REASON="Pre-dispatch model check: no model specified on Agent call and no bead ID in prompt. Apply the ladder (see roles/optimus.md or roles/tarzan.md) and pass model=haiku|sonnet|opus on the Agent call."
    REASON="$REASON" python3 -c "
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
  fi
  exit 0
fi

# For each bead, check model label
MISMATCH_DETAILS=""
NO_LABEL_BEADS=""

for BEAD_ID in $BEAD_IDS; do
  LABELS_OUT=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    item = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
    if not item:
        sys.exit(0)
    labels = item.get('labels', []) or []
    if isinstance(labels, str):
        labels = [l.strip() for l in labels.split(',')]
    for lab in labels:
        print(lab)
except Exception:
    pass
" 2>/dev/null || echo "")

  BEAD_MODEL=""
  for LABEL in $LABELS_OUT; do
    case "$LABEL" in
      model:haiku) BEAD_MODEL="haiku" ;;
      model:sonnet) BEAD_MODEL="sonnet" ;;
      model:opus) BEAD_MODEL="opus" ;;
    esac
  done

  if [ -z "$BEAD_MODEL" ]; then
    NO_LABEL_BEADS="$NO_LABEL_BEADS $BEAD_ID"
    continue
  fi

  if [ -z "$NORMALIZED_DISPATCH" ]; then
    # Bead has label but dispatch didn't specify → mismatch
    MISMATCH_DETAILS="$MISMATCH_DETAILS\n  $BEAD_ID has model:$BEAD_MODEL but dispatch model is unspecified"
    continue
  fi

  if [ "$NORMALIZED_DISPATCH" = "$BEAD_MODEL" ]; then
    # Match — silent allow
    continue
  fi

  # Mismatch
  MISMATCH_DETAILS="$MISMATCH_DETAILS\n  $BEAD_ID has model:$BEAD_MODEL but dispatch chose $NORMALIZED_DISPATCH"
done

# If any mismatches and no override line → deny
if [ -n "$MISMATCH_DETAILS" ] && [ "$HAS_OVERRIDE" = "0" ]; then
  REASON=$(printf 'Pre-dispatch model check: dispatch model disagrees with bead label.%b\n\nEither (a) retry with the matching model, or (b) add a "Model override: <reason>" line to the dispatch prompt explaining why you'\''re stepping up/down. The override is logged in the prompt for audit.' "$MISMATCH_DETAILS")
  REASON="$REASON" python3 -c "
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
fi

# If all beads lack model labels and no dispatch model specified → deny
if [ -n "$NO_LABEL_BEADS" ] && [ -z "$MISMATCH_DETAILS" ] && [ -z "$NORMALIZED_DISPATCH" ]; then
  REASON="Pre-dispatch model check: no model specified on Agent call, and bead(s) [$NO_LABEL_BEADS ] have no model: label. Apply the ladder (see roles/optimus.md or roles/tarzan.md) and either pass model=haiku|sonnet|opus on the Agent call OR add a model: label to the bead first via bd update --add-label=model:<x>."
  REASON="$REASON" python3 -c "
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
fi

# If beads lack labels but dispatch specified a model → allow + nudge to add the label
if [ -n "$NO_LABEL_BEADS" ] && [ -n "$NORMALIZED_DISPATCH" ]; then
  CTX="Pre-dispatch model check: bead(s) [$NO_LABEL_BEADS ] have no model: label. Allowing this dispatch with model=$NORMALIZED_DISPATCH. Suggest \`bd update <id> --add-label=model:$NORMALIZED_DISPATCH\` after dispatch so the next worker doesn't re-derive."
  CTX="$CTX" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': os.environ.get('CTX', '')
    }
}))
"
  exit 0
fi

# All checks passed — silent allow
exit 0
