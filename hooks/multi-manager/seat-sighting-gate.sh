#!/usr/bin/env bash
# seat-sighting-gate.sh — the merge seat (chuck) may not create WORK beads
# directly; sightings are the carve-out (SABLE-441vl).
#
# Trigger: PreToolUse on Bash (matching `bd create`) | identity-gated on chuck
#
# BACKGROUND. "Chuck cannot file work-creating beads" was, before this hook,
# ONLY role prose (templates/multi-manager/roles/chuck.md's boundaries
# section) — no PreToolUse hook, no code path anywhere actually refused a
# `bd create` from the seat. That gap is what made SABLE-441vl possible in
# the first place: any finding chuck made reached the backlog ONLY IF a
# manager read his message and chose to file it, which worked on ATTENTION,
# not STRUCTURE. This hook makes the boundary MECHANICAL for the first time,
# and builds the carve-out in from day one rather than bolting it on later:
# a SIGHTING (label `sighting`, filed via `sable-msg --file-sighting` —
# see bin/sable-msg's file_sighting_bead) is allowed through; anything else
# from chuck's identity is refused.
#
# WHY THIS MUST NOT WIDEN THE SEAT'S AUTHORITY. The principle this hook
# exists to preserve, not relax: the seat verifies and merges, it does not
# decide what gets built. A sighting is NOT an exception to that — it is
# DEFERRED from the moment sable-msg's create call returns (a second `bd
# update --status=deferred` call, since `bd create` has no --status flag at
# all), so it can never enter `bd ready` until a manager reads it and
# explicitly promotes it. The carve-out widens WHAT CAN BE RECORDED, never
# WHAT CAN BE DISPATCHED.
#
# Only ever acts on identity == chuck (the seat) via lib-identity.sh's
# sable_resolve_identity (agent_type first, CLAUDE_AGENT_NAME env fallback —
# see that file for the priority order and the subagent-contamination
# rationale). Every other identity's `bd create` passes through untouched:
# this hook has exactly one job.

set -euo pipefail

HOOK_INPUT_JSON="$(cat)"

COMMAND=$(printf '%s' "$HOOK_INPUT_JSON" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('tool_input', {}).get('command', ''))
" 2>/dev/null) || exit 0

[ -z "$COMMAND" ] && exit 0

# Only act on bd create.
echo "$COMMAND" | grep -q '^bd create' || exit 0

# shellcheck source=lib-identity.sh
source "$(dirname "${BASH_SOURCE[0]:-$0}")/lib-identity.sh"
sable_resolve_identity "$HOOK_INPUT_JSON"

# Not the seat: no-op. This hook has exactly one job.
[ "$SABLE_ID_NAME" = "chuck" ] || exit 0

# A sighting carries the `sighting` label (bin/sable-msg's SIGHTING_LABELS —
# "sighting,for-triage"). Any --labels/-l value containing it, in any of bd's
# accepted quoting forms, is a sighting and passes through.
LABELS=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = (
    re.search(r'--labels?[= ]\"([^\"]+)\"', cmd)
    or re.search(r\"--labels?[= ]'([^']+)'\", cmd)
    or re.search(r'--labels?[= ]([^\s\"\']+)', cmd)
    or re.search(r'(?:^|\s)-l[= ]\"([^\"]+)\"', cmd)
    or re.search(r\"(?:^|\s)-l[= ]'([^']+)'\", cmd)
    or re.search(r'(?:^|\s)-l[= ]([^\s\"\']+)', cmd)
)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

if echo ",$LABELS," | grep -qE ',sighting,'; then
  exit 0
fi

python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': (
            'SABLE seat boundary (SABLE-441vl): chuck (the merge seat) may not create '
            'WORK beads directly — the seat verifies and merges, it does not decide what '
            'gets built. Record this as a durable SIGHTING instead: sable-msg '
            '--file-sighting \"<observation>\" (deferred on creation, label '
            'sighting,for-triage; a manager promotes it later with bd update <id> '
            '--status=open). If this is delegated author work, message the author lane '
            'instead of filing it yourself.'
        ),
    }
}))
"
