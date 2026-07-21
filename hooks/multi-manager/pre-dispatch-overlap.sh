#!/usr/bin/env bash
# pre-dispatch-overlap.sh — Declared file-footprint overlap SCHEDULING CONSTRAINT
# Trigger: PreToolUse:Agent | Timeout: 5000ms
#
# Reads claims from all in-progress beads (other than the ones being dispatched
# now) and compares them against this dispatch's declared file footprint.
#
# SABLE-jd5fj.6: this is a SCHEDULING CONSTRAINT, not advisory — an overlap
# with a DIFFERENT in-progress bead's footprint DENIES the dispatch outright.
# Two named outs:
#   1. Wait for the overlapping bead to clear (close/land), or
#   2. Dispatch with an explicit 'Serialize-with: <bead-id>' line naming the
#      overlapping bead — this ALLOWS the dispatch and tags BOTH beads'
#      `serialize_with` metadata so Chuck's for-chuck handoff sequences the
#      merges together instead of racing them.
#
# Skips: worker/bare subagent context, dispatches with no inferrable bead IDs.

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

PROMPT=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print((d.get('tool_input', {}) or {}).get('prompt', ''))
" 2>/dev/null) || exit 0

[ -z "$PROMPT" ] && exit 0

# SABLE-jd5fj.6: the two named outs. 'Serialize-with: <bead-id>[, <bead-id>...]'
# in the dispatch prompt names the overlapping bead(s) the operator has decided
# to serialize with — matched against the ACTUAL overlap hits below, not taken
# on faith (naming an unrelated bead does not launder an unrelated overlap).
# Extracted BEFORE the DISPATCH_IDS pass below, whose input has the
# 'Serialize-with:' line stripped out first — otherwise the named partner bead
# (which is NOT part of this dispatch) gets swept into DISPATCH_IDS by the
# generic id regex and is then wrongly excluded from its own overlap check.
SERIALIZE_WITH=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
ids = set()
for m in re.finditer(r'Serialize-with:\s*([^\n]+)', text, re.IGNORECASE):
    for tok in re.split(r'[,\s]+', m.group(1).strip()):
        if tok:
            ids.add(tok)
print(' '.join(sorted(ids)))
" 2>/dev/null)

PROMPT_SANS_SERIALIZE=$(echo "$PROMPT" | python3 -c "
import sys, re
text = sys.stdin.read()
print(re.sub(r'(?i)Serialize-with:\s*[^\n]+', '', text))
" 2>/dev/null)

# Extract bead IDs from this dispatch
DISPATCH_IDS=$(echo "$PROMPT_SANS_SERIALIZE" | python3 -c "
import sys, re
text = sys.stdin.read()
ids = set(re.findall(r'\b((?:bd|sable|epic|task|bug|feat)-[a-zA-Z0-9_-]+)\b', text, re.IGNORECASE))
for i in sorted(ids):
    print(i)
" 2>/dev/null)

[ -z "$DISPATCH_IDS" ] && exit 0

# Aggregate declared-footprint file claims from this dispatch's beads. Priority:
# 1) wip_claims metadata (already established — SABLE-szd dedicated field), then
# 2) a planner-authored '## File footprint' description section (SABLE-jd5fj.6:
#    the declared-footprint dogfood — see this bead's own description), which
#    may name extension-less files (e.g. bin/sable-spawn-worker) the generic
#    regex below would miss, then
# 3) the generic per-token file-extension regex, for beads authored before the
#    footprint-section convention. Claims may not exist yet at pre-dispatch time
#    (this hook and pre-dispatch-claim.sh fire on the same trigger with no
#    ordering guarantee), so all three sources are unioned.
DISPATCH_FILES=$(for BID in $DISPATCH_IDS; do
  bd show "$BID" --json 2>/dev/null | python3 -c "
import json, sys, re
try:
    data = json.load(sys.stdin)
    if not (isinstance(data, list) and data):
        sys.exit(0)
    metadata = data[0].get('metadata', {}) or {}
    wip_claims = metadata.get('wip_claims', '') or ''
    for p in wip_claims.split(','):
        p = p.strip()
        if p:
            print(p)
    desc = data[0].get('description', '') or ''
    section = re.search(r'^##\s*File footprint\s*\n(.+?)(?=\n##\s|\Z)', desc,
                         re.MULTILINE | re.DOTALL)
    if section:
        for part in section.group(1).split(','):
            part = part.strip()
            if not part:
                continue
            print(part.split()[0])
    else:
        for m in re.finditer(r'(?:^|[\s\(\[\"\\'])((?:[\w\-./]+/)?[\w\-./]+\.(?:ts|tsx|js|jsx|py|rs|go|java|rb|md|yaml|yml|toml|json|sh|sql|css|scss|html))(?=[\s\)\]\"\\',:;]|$)', desc, re.MULTILINE):
            print(m.group(1))
except Exception:
    pass
" 2>/dev/null
done | sort -u)

[ -z "$DISPATCH_FILES" ] && exit 0

# Find all in-progress beads (status=in_progress) not in dispatch set
IN_PROGRESS=$(bd list --status=in_progress --json 2>/dev/null || echo "[]")

OVERLAPS_JSON=$(echo "$IN_PROGRESS" | DISPATCH_IDS="$DISPATCH_IDS" DISPATCH_FILES="$DISPATCH_FILES" python3 -c "
import json, sys, os

dispatch_ids = set(os.environ.get('DISPATCH_IDS', '').split())
dispatch_files = set(os.environ.get('DISPATCH_FILES', '').split('\n'))
dispatch_files.discard('')

try:
    data = json.load(sys.stdin)
except Exception:
    data = []

if not isinstance(data, list):
    data = []

overlaps = []
for item in data:
    bid = item.get('id', '')
    if not bid or bid in dispatch_ids:
        continue
    metadata = item.get('metadata', {}) or {}
    wip_claims = metadata.get('wip_claims', '') or ''
    files = set(p.strip() for p in wip_claims.split(',') if p.strip())
    shared = files & dispatch_files
    if shared:
        overlaps.append({
            'bead': bid,
            'assignee': item.get('assignee', '') or 'unassigned',
            'files': sorted(shared),
        })

print(json.dumps(overlaps))
" 2>/dev/null) || OVERLAPS_JSON="[]"
[ -z "$OVERLAPS_JSON" ] && OVERLAPS_JSON="[]"
[ "$OVERLAPS_JSON" = "[]" ] && exit 0

DECISION=$(OVERLAPS_JSON="$OVERLAPS_JSON" SERIALIZE_WITH="$SERIALIZE_WITH" python3 -c "
import json, os

overlaps = json.loads(os.environ.get('OVERLAPS_JSON') or '[]')
serialize_with = set(os.environ.get('SERIALIZE_WITH', '').split())

uncovered = [o for o in overlaps if o['bead'] not in serialize_with]
covered = [o for o in overlaps if o['bead'] in serialize_with]

if uncovered:
    lines = '\n'.join(
        f\"  - {o['bead']} ({o['assignee']}, in-progress): {', '.join(o['files'])}\"
        for o in uncovered
    )
    reason = (
        'OVERLAP DETECTED — proposed dispatch shares declared file footprint '
        f'with active in-progress work:\n{lines}\n\n'
        'SCHEDULING CONSTRAINT: this dispatch is DENIED. Two outs -- '
        '(1) wait for the overlapping bead to clear, or '
        '(2) dispatch with an explicit Serialize-with: <bead-id> line naming '
        'the overlapping bead(s) above to proceed -- both merges will be tagged '
        'serialize-together for Chuck.'
    )
    print(json.dumps({'decision': 'deny', 'reason': reason}))
elif covered:
    tagged = sorted({o['bead'] for o in covered})
    print(json.dumps({'decision': 'allow', 'tagged': tagged}))
else:
    print(json.dumps({'decision': 'none'}))
" 2>/dev/null) || DECISION='{"decision": "none"}'

DECISION_KIND=$(printf '%s' "$DECISION" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('decision', 'none'))
except Exception:
    print('none')
" 2>/dev/null)

if [ "$DECISION_KIND" = "deny" ]; then
  REASON=$(printf '%s' "$DECISION" | python3 -c "
import json, sys
print(json.load(sys.stdin).get('reason', ''))
" 2>/dev/null)
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

if [ "$DECISION_KIND" = "allow" ]; then
  TAGGED=$(printf '%s' "$DECISION" | python3 -c "
import json, sys
print(' '.join(json.load(sys.stdin).get('tagged', [])))
" 2>/dev/null)

  # Append (never overwrite — SABLE-szd) serialize_with on every bead on BOTH
  # sides of the accepted overlap, so Chuck's handoff can find the tag from
  # either bead's metadata.
  for DID in $DISPATCH_IDS; do
    for PARTNER in $TAGGED; do
      for A in "$DID" "$PARTNER"; do
        [ "$A" = "$DID" ] && B="$PARTNER" || B="$DID"
        CURRENT=$(bd show "$A" --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and data:
        print((data[0].get('metadata', {}) or {}).get('serialize_with', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")
        NEW=$(python3 -c "
import sys
current, partner = sys.argv[1], sys.argv[2]
existing = set(p.strip() for p in current.split(',') if p.strip())
existing.add(partner)
print(','.join(sorted(existing)))
" "$CURRENT" "$B")
        bd update "$A" --sandbox --set-metadata "serialize_with=$NEW" >/dev/null 2>&1 || true
      done
    done
  done

  TAGGED="$TAGGED" python3 -c "
import json, os
tagged = os.environ.get('TAGGED', '')
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': f'SERIALIZE-WITH ACCEPTED — overlap with {tagged} permitted; tagged serialize-together (serialize_with metadata) for the for-chuck handoff.'
    }
}))
"
  exit 0
fi

exit 0
