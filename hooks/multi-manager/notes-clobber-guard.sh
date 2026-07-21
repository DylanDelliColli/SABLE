#!/usr/bin/env bash
# notes-clobber-guard.sh — PreToolUse:Bash guard: deny a destructive
# `bd update <id> --notes ...` that would REPLACE a bead's non-empty notes.
# Trigger: PreToolUse:Bash | Timeout: 3000ms
#
# SABLE-sm269. `bd update --notes` REPLACES the notes field wholesale; there is
# no confirmation, no diff, and the bead still looks well-formed afterwards.
# The failure is therefore SILENT BY CONSTRUCTION: whatever the notes carried —
# a WIP-CLAIMS line the overlap hook reads, a manager's dispatch scoping, a
# reviewer's reopen reason — is simply gone, and nothing in the tool output says
# so. Two different managers hit this inside a single day (optimus destroyed
# SABLE-cmar4.1's WIP-CLAIMS line while recording an unrelated serialization
# decision; tarzan's four-bead near-miss on d4zb9/18gw7/onsf/xh6j audited clean
# only because those notes happened to be empty).
#
# WHY A HOOK AND NOT A DOCUMENTED RULE (ruling, lincoln 2026-07-21): the
# documented-convention half was disproven by those two occurrences. Convention
# propagates slower than dispatch does, so every un-briefed agent is one write
# away from recurrence, and because the failure is silent nobody learns from it
# in time. A creation-time mechanical check does not depend on propagation.
#
# BEHAVIOR
#
#   bd update <id> --notes "x"        notes currently NON-EMPTY  -> DENY
#                                     notes currently EMPTY      -> ALLOW, silent
#   bd update <id> --append-notes "x" (any notes)                -> ALLOW, silent
#   anything that is not a `bd update` carrying --notes           -> ALLOW, silent
#   --notes present but the target/notes cannot be resolved       -> ALLOW, LOUD
#
# FAIL-OPEN, BUT NEVER SILENTLY (Standing Discipline 7). Fail-open here means
# the guard must not BLOCK on a command line it cannot parse or a bead it cannot
# read — a guard that hard-fails on a malformed argv would block legitimate
# work. It does NOT mean the guard goes quiet about having failed to evaluate: a
# guard that silently allows on error is indistinguishable from one that allowed
# on purpose (the SABLE-2az2x/6sdpx defect class). So the unresolvable case
# emits an explicit could-not-assess note alongside the allow.
#
# SCOPE. This guard protects ANY notes content, not just dispatch claims, so it
# stays valuable after WIP-CLAIMS finish migrating to the wip_claims metadata
# field (SABLE-szd / SABLE-jd5fj.6) — that migration is tracked separately on
# SABLE-sm269 and is deliberately NOT done here. The sibling hazard on the
# metadata side — `bd update --metadata '{json}'` REPLACES the whole metadata
# blob, wip_claims included — is out of scope for this hook and is recorded on
# SABLE-6la1 item (3).
#
# `bd` absent from PATH is treated as "no write can happen" and exits silently:
# the guarded command would itself fail, so there is nothing to protect.

set -uo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

COMMAND=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print((d.get('tool_input') or {}).get('command', '') or '')
" 2>/dev/null) || COMMAND=""

[ -z "$COMMAND" ] && exit 0

# ---------------------------------------------------------------------------
# Emit helpers (same hookSpecificOutput shape as stash-worktree-guard.sh)
# ---------------------------------------------------------------------------
allow_with_context() {
  MSG="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'allow',
        'additionalContext': os.environ.get('MSG', '')
    }
}))
"
  exit 0
}

deny_with_reason() {
  REASON="$1" python3 -c "
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
}

# ---------------------------------------------------------------------------
# Classify the command line. Prints one of:
#   NONE                     no destructive `bd update --notes` found
#   UNRESOLVED               --notes present, but no target bead id parseable
#   TARGETS\n<id>\n<id>...   --notes present; these are the target bead ids
# ---------------------------------------------------------------------------
CLASSIFY=$(CMD_STR="$COMMAND" python3 -c "
import os, re, shlex, sys

cmd = os.environ.get('CMD_STR', '')

# A --notes token anywhere is the cheap pre-filter. Note '--append-notes'
# cannot match '--notes' here: the boundary before 'notes' is a '-', not a
# word boundary the pattern accepts.
if not re.search(r'(^|[\s;&|(])--notes([=\s]|\$)', cmd):
    print('NONE')
    sys.exit(0)

try:
    tokens = shlex.split(cmd)
except ValueError:
    print('UNRESOLVED')
    sys.exit(0)

SHELL_SEPS = {';', '&&', '||', '|', '&'}
ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
# Bead ids as bd mints them: <prefix>-<suffix>, suffix may carry .N for children.
BEAD_ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*-[A-Za-z0-9]+(\.[0-9]+)*\$')


def segments(toks):
    seg = []
    for t in toks:
        if t in SHELL_SEPS:
            yield seg
            seg = []
        else:
            seg.append(t)
    yield seg


def strip_env_prefix(seg):
    i, n = 0, len(seg)
    while i < n:
        t = seg[i]
        if ENV_ASSIGN_RE.match(t):
            i += 1
            continue
        if t == 'env':
            i += 1
            while i < n:
                tt = seg[i]
                if ENV_ASSIGN_RE.match(tt):
                    i += 1
                    continue
                if tt == '-u' and i + 1 < n:
                    i += 2
                    continue
                break
            continue
        break
    return seg[i:]


verdict = 'NONE'
targets = []

for raw_seg in segments(tokens):
    seg = strip_env_prefix(raw_seg)
    if len(seg) < 2:
        continue
    if os.path.basename(seg[0]) != 'bd' or seg[1] != 'update':
        continue

    rest = seg[2:]
    has_notes = any(t == '--notes' or t.startswith('--notes=') for t in rest)
    if not has_notes:
        continue
    has_append = any(t == '--append-notes' or t.startswith('--append-notes=') for t in rest)
    if has_append:
        # An --append-notes in the same invocation is non-destructive by
        # construction; do not over-deny it.
        continue

    # Target ids are the positional args that precede the first flag.
    ids = []
    parseable = True
    for t in rest:
        if t.startswith('-'):
            break
        if BEAD_ID_RE.match(t):
            ids.append(t)
        else:
            parseable = False
            break

    if ids and parseable:
        verdict = 'TARGETS'
        targets = ids
    else:
        verdict = 'UNRESOLVED'
    break

if verdict == 'TARGETS':
    print('TARGETS')
    for t in targets:
        print(t)
else:
    print(verdict)
" 2>/dev/null) || CLASSIFY="UNRESOLVED"

[ -z "$CLASSIFY" ] && CLASSIFY="UNRESOLVED"

VERDICT="$(printf '%s\n' "$CLASSIFY" | head -1)"

APPEND_HINT="Use 'bd update <id> --append-notes \"...\"' instead — it preserves what is already there. If you genuinely intend to REPLACE the notes, read them first ('bd show <id> --json'), fold the parts you want to keep into your new text, and clear the field in a separate deliberate step."

case "$VERDICT" in
  NONE)
    exit 0
    ;;
  UNRESOLVED)
    allow_with_context "notes-clobber-guard: COULD NOT ASSESS this 'bd update --notes' — the target bead id was not parseable from the command line, so the guard does not know whether this write destroys existing notes. ALLOWING (fail-open), but you are on your own: '--notes' REPLACES the notes field wholesale and the loss is silent. $APPEND_HINT"
    ;;
esac

# --- TARGETS: resolve each bead's current notes -----------------------------
if ! command -v bd >/dev/null 2>&1; then
  # No bd means the guarded command cannot write anything either.
  exit 0
fi

# notes_of <bead-id> — prints the current notes; exit 3 = could not assess.
notes_of() {
  bd show "$1" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(3)
if isinstance(d, list):
    if not d:
        sys.exit(3)
    d = d[0]
if not isinstance(d, dict):
    sys.exit(3)
n = d.get('notes')
sys.stdout.write(n if isinstance(n, str) else '')
"
}

NONEMPTY=""
UNASSESSED=""

while IFS= read -r BEAD_ID; do
  [ -z "$BEAD_ID" ] && continue
  [ "$BEAD_ID" = "TARGETS" ] && continue
  NOTES="$(notes_of "$BEAD_ID")" || { UNASSESSED="$UNASSESSED $BEAD_ID"; continue; }
  # Whitespace-only notes carry nothing worth protecting.
  if [ -n "$(printf '%s' "$NOTES" | tr -d '[:space:]')" ]; then
    NONEMPTY="$NONEMPTY $BEAD_ID"
  fi
done <<EOF
$CLASSIFY
EOF

if [ -n "$NONEMPTY" ]; then
  deny_with_reason "notes-clobber-guard: DENIED —'bd update --notes' REPLACES the notes field, and these beads have non-empty notes that this write would destroy silently:${NONEMPTY}. $APPEND_HINT (SABLE-sm269: this exact write destroyed SABLE-cmar4.1's WIP-CLAIMS line; the bead looked fine afterwards.)"
fi

if [ -n "$UNASSESSED" ]; then
  allow_with_context "notes-clobber-guard: COULD NOT ASSESS —'bd show' did not return readable notes for:${UNASSESSED}. ALLOWING (fail-open), but the guard did NOT verify that this '--notes' write destroys nothing. $APPEND_HINT"
fi

exit 0
