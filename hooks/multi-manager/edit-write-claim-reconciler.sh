#!/usr/bin/env bash
# edit-write-claim-reconciler.sh — Append emergent file claims to bead metadata
# Trigger: PreToolUse:Edit|Write | Timeout: 3000ms
#
# When a worker (subagent) modifies a file not declared in the bead description,
# append the file path to the bead's `wip_claims` metadata field (SABLE-szd:
# NOT notes — see pre-dispatch-claim.sh for why) so other dispatches see it.
#
# Only fires inside subagent contexts (managers don't typically edit files directly
# in this pattern — they dispatch workers). Fast-exit if no agent_id.

set -euo pipefail

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
tool_input = d.get('tool_input', {})
file_path = tool_input.get('file_path', '')
agent_id = d.get('agent_id', '')
transcript = d.get('transcript_path', '')
print(f'{agent_id}\n{file_path}\n{transcript}')
" 2>/dev/null) || exit 0

AGENT_ID=$(echo "$PARSED" | sed -n '1p')
FILE_PATH=$(echo "$PARSED" | sed -n '2p')
TRANSCRIPT=$(echo "$PARSED" | sed -n '3p')

# Only act on subagent contexts
[ -z "$AGENT_ID" ] && exit 0
[ -z "$FILE_PATH" ] && exit 0
[ -z "$TRANSCRIPT" ] && exit 0

# Skip test files and generated files (less likely to be claim-worthy overlap surface)
echo "$FILE_PATH" | grep -qiE '(\.test\.|\.spec\.|__tests__|__pycache__|\.next/|node_modules/)' && exit 0

# Find the bead this subagent was dispatched for by scanning the transcript for
# a recent `bd update --claim` or "bead bd-X" reference
[ ! -f "$TRANSCRIPT" ] && exit 0

BEAD_ID=$(python3 -c "
import json, re, sys
path = sys.argv[1]
ids = []
try:
    with open(path) as f:
        for line in f:
            try:
                msg = json.loads(line)
            except Exception:
                continue
            text = json.dumps(msg)
            for m in re.finditer(r'\b((?:bd|sable|epic|task|bug|feat)-[a-zA-Z0-9_-]+)\b', text, re.IGNORECASE):
                ids.append(m.group(1))
except Exception:
    sys.exit(0)
# Most recent ID is most likely to be the active bead
if ids:
    print(ids[-1])
" "$TRANSCRIPT" 2>/dev/null)

[ -z "$BEAD_ID" ] && exit 0

# Read current claims from the dedicated metadata field (SABLE-szd: NOT
# notes — bd update --notes overwrites the whole field, and this hook fires
# on every Edit/Write, so a notes-overwrite from anywhere else in the bead's
# life would otherwise clobber this claim on the very next reconcile).
CURRENT_CLAIMS=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    if isinstance(data, list) and data:
        print((data[0].get('metadata', {}) or {}).get('wip_claims', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

# If file already in claims, skip
echo "$CURRENT_CLAIMS" | grep -qF "$FILE_PATH" && exit 0

if [ -n "$CURRENT_CLAIMS" ]; then
  NEW_CLAIMS="${CURRENT_CLAIMS}, ${FILE_PATH}"
else
  NEW_CLAIMS="$FILE_PATH"
fi

# SABLE-lfql: --sandbox disables bd's Dolt auto-push (SABLE-rq9k). bd pushes to
# the shared Dolt remote on EVERY mutating write (create/update/close) by
# default; without this flag, every Edit/Write while a bead is claimed pushed
# WIP-CLAIMS bookkeeping to the remote as a pure hook side effect — the exact
# chuck-only-convention violation behind the 2026-07-09 cross-fleet corruption
# incident. --sandbox disables the push WITHOUT blocking the write (unlike
# --readonly, which would drop it); Chuck's batched pull+push carries it later.
bd update "$BEAD_ID" --sandbox --set-metadata "wip_claims=$NEW_CLAIMS" >/dev/null 2>&1 || true

exit 0
