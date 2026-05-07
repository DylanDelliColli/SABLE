#!/usr/bin/env bash
# bead-description-gate.sh — Validate bead descriptions on creation
# Trigger: PreToolUse on Bash (matching bd create) | Timeout: 3000ms
#
# Two-mode operation:
#   - Manager mode (CLAUDE_AGENT_ROLE=manager OR CLAUDE_AGENT_NAME set):
#       Hard-block (deny) on missing required content. Multi-manager pattern
#       depends on bead descriptions reliably naming files; nudge isn't enough.
#   - Default mode (no agent identity): nudge via additionalContext.
#
# Label-aware: when --labels includes sherlock-finding, additional sections
# from templates/sherlock-bead.md are required (Rationale, Evidence with
# Fingerprint, Proposed approach, Scope estimate, Risk if not addressed).
# When --labels includes columbo-test-spec, sections from
# templates/columbo-bead.md are required (Feature under test, Test file,
# Cases with Why:, Categories, Fixtures / setup, Out of scope). When
# --labels includes columbo-test-gap, the audit-mode required sections
# are enforced (Symptom, Cited test file, Cited source file, Fingerprint,
# Cases to add, Categories, Risk if not addressed). These are the contracts
# each agent commits to in its role file.

set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
print(cmd)
" 2>/dev/null) || exit 0

COMMAND="$PARSED"
[ -z "$COMMAND" ] && exit 0

# Only act on bd create
echo "$COMMAND" | grep -q '^bd create' || exit 0

# Skip epics — they don't need test specs / file paths
echo "$COMMAND" | grep -qiE -- '--type[= ]?epic' && exit 0

# Determine enforcement mode
if [ -n "${CLAUDE_AGENT_NAME:-}" ] || [ "${CLAUDE_AGENT_ROLE:-}" = "manager" ]; then
  MODE="block"
else
  MODE="nudge"
fi

# Extract --labels or --label value (comma-separated list)
LABELS=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = (
    re.search(r'--labels?[= ]\"([^\"]+)\"', cmd)
    or re.search(r\"--labels?[= ]'([^']+)'\", cmd)
    or re.search(r'--labels?[= ]([^\s\"\']+)', cmd)
)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

SHERLOCK_FINDING=0
if echo ",$LABELS," | grep -q ',sherlock-finding,'; then
  SHERLOCK_FINDING=1
fi

COLUMBO_SPEC=0
if echo ",$LABELS," | grep -q ',columbo-test-spec,'; then
  COLUMBO_SPEC=1
fi

COLUMBO_GAP=0
if echo ",$LABELS," | grep -q ',columbo-test-gap,'; then
  COLUMBO_GAP=1
fi

# Extract description content (between quotes after --description)
DESC=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = re.search(r'--description[= ]\"((?:[^\"\\\\]|\\\\.)*)\"', cmd, re.DOTALL) \
    or re.search(r\"--description[= ]'((?:[^'\\\\]|\\\\.)*)'\", cmd, re.DOTALL)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

# No --description at all
if ! echo "$COMMAND" | grep -qE -- '--description'; then
  if [ "$MODE" = "block" ]; then
    python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': 'SABLE bead quality: bd create has no --description flag. Manager context requires a description that passes the Fresh Agent Test (file paths, test spec, acceptance criteria). Add --description and retry.'
    }
}))
"
    exit 0
  else
    python3 -c "
import json
print(json.dumps({
    'additionalContext': 'SABLE bead quality: This bd create has no --description flag. Every bead needs a description that passes the Fresh Agent Test: file paths, function names, what to change, test file path, and acceptance criteria.'
}))
"
    exit 0
  fi
fi

# Description present but empty
if [ -z "$DESC" ]; then
  exit 0
fi

# Build the missing-sections list
MISSING_LIST=""

append_missing() {
  if [ -z "$MISSING_LIST" ]; then
    MISSING_LIST="$1"
  else
    MISSING_LIST="$MISSING_LIST; $1"
  fi
}

# Sherlock-finding additional checks (only if labeled)
if [ "$SHERLOCK_FINDING" = "1" ]; then
  echo "$DESC" | grep -qE '^## Rationale' \
    || append_missing "## Rationale section"
  echo "$DESC" | grep -qE 'Fingerprint:' \
    || append_missing "Evidence with at least one Fingerprint: line"
  echo "$DESC" | grep -qE '^## Proposed approach' \
    || append_missing "## Proposed approach section"
  echo "$DESC" | grep -qE '^## Scope estimate' \
    || append_missing "## Scope estimate section"
  echo "$DESC" | grep -qE '^## Risk if not addressed' \
    || append_missing "## Risk if not addressed section"
fi

# Columbo-test-spec additional checks (forward-mode output, only if labeled)
if [ "$COLUMBO_SPEC" = "1" ]; then
  echo "$DESC" | grep -qE '^## Feature under test' \
    || append_missing "## Feature under test section"
  echo "$DESC" | grep -qE '^## Test file' \
    || append_missing "## Test file section"
  echo "$DESC" | grep -qE '^## Cases' \
    || append_missing "## Cases section"
  # Cases must contain at least one bullet with a Why: sub-line
  echo "$DESC" | grep -qE '(Why|why):' \
    || append_missing "## Cases must include at least one Why: sub-line per case"
  echo "$DESC" | grep -qE '^## Categories' \
    || append_missing "## Categories section"
  echo "$DESC" | grep -qE '^## Fixtures' \
    || append_missing "## Fixtures / setup section (use 'Fixtures: none.' if no setup)"
  echo "$DESC" | grep -qE '^## Out of scope' \
    || append_missing "## Out of scope section"
fi

# Columbo-test-gap additional checks (audit-mode output, only if labeled)
if [ "$COLUMBO_GAP" = "1" ]; then
  echo "$DESC" | grep -qE '^## Symptom' \
    || append_missing "## Symptom section"
  echo "$DESC" | grep -qE '^## Cited test file' \
    || append_missing "## Cited test file section"
  echo "$DESC" | grep -qE '^## Cited source file' \
    || append_missing "## Cited source file section"
  echo "$DESC" | grep -qE '^## Fingerprint' \
    || append_missing "## Fingerprint section (literal substring grep-able from cited file)"
  echo "$DESC" | grep -qE '^## Cases to add' \
    || append_missing "## Cases to add section"
  echo "$DESC" | grep -qE '^## Categories' \
    || append_missing "## Categories section"
  echo "$DESC" | grep -qE '^## Risk if not addressed' \
    || append_missing "## Risk if not addressed section"
fi

# Standard checks (apply to all non-epic beads)
if ! echo "$DESC" | grep -qiE '(test|\.test\.|\.spec\.|__tests__|pytest|vitest|TDD|red.green|\[no-test\])'; then
  append_missing "test spec (which test file, what assertions)"
fi

if ! echo "$DESC" | grep -qiE '(\.(ts|tsx|py|js|jsx|sh|go|rs|rb)|frontend/|src/|lib/|components/|hooks/|templates/)'; then
  append_missing "file paths (exact files to create/modify)"
fi

# Pass — no missing sections
[ -z "$MISSING_LIST" ] && exit 0

# Emit verdict based on mode
if [ "$MODE" = "block" ]; then
  if [ "$SHERLOCK_FINDING" = "1" ]; then
    REASON="SABLE bead quality (sherlock-finding): Description missing required sections per templates/sherlock-bead.md — $MISSING_LIST. Fix the description and retry. Sherlock findings have a higher quality bar than the default Fresh Agent Test."
  elif [ "$COLUMBO_SPEC" = "1" ]; then
    REASON="SABLE bead quality (columbo-test-spec): Description missing required sections per templates/columbo-bead.md — $MISSING_LIST. Fix the description and retry. Columbo test-spec beads form the worker's contract — the skeleton file plus the bead's Cases section together specify what must be tested."
  elif [ "$COLUMBO_GAP" = "1" ]; then
    REASON="SABLE bead quality (columbo-test-gap): Description missing required sections per templates/columbo-bead.md — $MISSING_LIST. Fix the description and retry. Columbo gap beads must include a Fingerprint to survive line drift between audit and execution."
  else
    REASON="SABLE bead quality: Description missing — $MISSING_LIST. Manager context requires beads pass the Fresh Agent Test before creation. Add the missing sections and retry."
  fi
  MISSING_LIST="$MISSING_LIST" REASON="$REASON" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': os.environ.get('REASON', '')
    }
}))
"
else
  MISSING_LIST="$MISSING_LIST" python3 -c "
import json, os
m = os.environ.get('MISSING_LIST', '')
print(json.dumps({
    'additionalContext': f'SABLE bead quality: Description is missing: {m}. Good beads include file paths, function names, test file references, and acceptance criteria so agents can act immediately without re-exploring.'
}))
"
fi
