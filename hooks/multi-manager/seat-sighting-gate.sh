#!/usr/bin/env bash
# seat-sighting-gate.sh — mechanical support for "capture is mandatory,
# priority is advisory" (SABLE-441vl, cockpit ruling 2026-07-22 16:23).
#
# Trigger: PostToolUse on Bash (a successful `bd create` OR
# `sable-msg --file-sighting`) | identity-gated on chuck
#
# THE ACTUAL RULING, so no future worker re-litigates it. The bead this hook
# implements was originally scoped on the premise that the merge seat (chuck)
# CANNOT create work-creating beads at all. That premise is MEASURED FALSE —
# chuck filed seven beads the same day the premise was written — and a
# cockpit ruling (recorded as a COMMENT on SABLE-441vl, not in its
# description, which is why an earlier pass here missed it and built the
# wrong thing) re-scoped the deliverable: CAPTURE IS MANDATORY — an unfiled
# seat finding is invisible by construction, and the seat is where the
# largest share of the fleet's evidence physically passes, so the seat MUST
# be free to file. What the seat does NOT do is set precedence: priority on
# any seat-filed bead is a SEAT ESTIMATE pending cockpit/manager triage,
# never final. "An unenforced boundary the governed party believes is
# enforced fails both ways" — before this hook that split was carried only by
# chuck hand-typing a prose prefix into the description, which is real but
# drifts the moment anyone forgets it.
#
# THIS HOOK NEVER DENIES. It fires AFTER a sighting create already succeeded
# and annotates the result: extracts the new bead id from either the raw
# create's stdout ("Created issue: <id>") or sable-msg's wrapper confirmation
# on stderr ("sable-msg: filed <id>"). PostToolUse observes only the top-level
# Bash command, never sable-msg's child `bd create`, so treating the wrapper as
# its own supported event shape is load-bearing. It then runs a follow-up `bd update
# --add-label seat-filed --set-metadata priority_provisional=true`. A
# PreToolUse hook in this catalog can only allow/deny/inject context — see
# bead-description-gate.sh — it cannot rewrite the command's own flags, which
# is why the annotation has to be a SEPARATE write after the fact rather than
# an injected --labels flag on the original call.
#
# Only ever acts on identity == chuck (the seat) via lib-identity.sh's
# sable_resolve_identity. Every other identity's `bd create` is untouched.

set -euo pipefail

HOOK_INPUT_JSON="$(cat)"

# Field names are defensive: the platform's tool-result key has been observed
# under more than one name (tool_response vs tool_result — see
# hooks/tdd-evidence.sh's own note on this), so both are checked and whichever
# the running platform emits is picked up without a schema guess breaking the
# other.
PARSED=$(printf '%s' "$HOOK_INPUT_JSON" | python3 -c "
import json, os, re, shlex, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
cmd = d.get('tool_input', {}).get('command', '')
resp = d.get('tool_response')
if not isinstance(resp, dict) or not resp:
    resp = d.get('tool_result')
if not isinstance(resp, dict):
    resp = {}
stdout = resp.get('stdout', '') or ''
stderr = resp.get('stderr', '') or ''

def command_kind(command):
    try:
        parts = shlex.split(command)
    except ValueError:
        return ''
    while parts and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', parts[0]):
        parts.pop(0)
    if not parts:
        return ''
    program = os.path.basename(parts[0])
    args = parts[1:]
    if re.fullmatch(r'python(?:[0-9]+(?:\\.[0-9]+)?)?', program):
        while args and args[0] in {
            '-B', '-E', '-I', '-O', '-OO', '-P', '-s', '-S', '-u', '-v', '-x'
        }:
            args.pop(0)
        if not args:
            return ''
        program = os.path.basename(args.pop(0))
    if program == 'bd' and args and args[0] == 'create':
        return 'bd-create'
    if program == 'sable-msg' and '--file-sighting' in args:
        return 'file-sighting'
    return ''

print(command_kind(cmd))
print('---OUTPUT---')
print(stdout)
print(stderr)
" 2>/dev/null) || exit 0

COMMAND_KIND=$(echo "$PARSED" | sed -n '1p')
[ -z "$COMMAND_KIND" ] && exit 0

# shellcheck source=lib-identity.sh
source "$(dirname "${BASH_SOURCE[0]:-$0}")/lib-identity.sh"
sable_resolve_identity "$HOOK_INPUT_JSON"

# Not the seat: no-op. This hook has exactly one job.
[ "$SABLE_ID_NAME" = "chuck" ] || exit 0

OUTPUT=$(echo "$PARSED" | sed -n '/^---OUTPUT---$/,$p' | tail -n +2)

# Each success marker is mode-specific, so a command that merely mentions the
# other tool cannot be misclassified. Failed creates emit neither marker; no
# separate exit-code check is needed.
if [ "$COMMAND_KIND" = "bd-create" ]; then
  BEAD_ID=$(
    echo "$OUTPUT" |
      grep -oE 'Created issue:[[:space:]]*[A-Za-z0-9_.-]+' |
      head -1 |
      awk '{print $NF}' ||
      true
  )
else
  BEAD_ID=$(
    echo "$OUTPUT" |
      grep -oE 'sable-msg: filed[[:space:]]+[A-Za-z0-9_.-]+' |
      head -1 |
      awk '{print $NF}' ||
      true
  )
fi
[ -z "$BEAD_ID" ] && exit 0

# Best-effort, never fails the hook: an annotation that could not be written
# is a missed label, not a reason to fail a Bash tool call after it already
# ran (the create already happened; this is pure side-effect bookkeeping).
bd update "$BEAD_ID" --add-label seat-filed --set-metadata priority_provisional=true \
  >/dev/null 2>&1 || true
