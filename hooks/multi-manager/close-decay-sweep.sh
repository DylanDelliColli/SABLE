#!/usr/bin/env bash
# close-decay-sweep.sh — PreToolUse:Bash advisory: when a bead id is retired by
# `bd close`, flag OPEN beads whose INSTRUCTIONS still name it (SABLE-x9vby).
# Trigger: PreToolUse:Bash | Timeout: 25000ms
#
# THE DEFECT CLASS. An instruction pinned to an identifier decays silently when
# that identifier is retired through NORMAL, CORRECT ACTION. Nothing fails,
# nothing errors, and the stale instruction still READS as satisfiable — so
# whoever follows it does something harmless and wrong, then ships. The
# motivating case: SABLE-jejx3's "verify gz3v2's suppression still holds after
# your change, or migrate the hold" survived lincoln's entirely correct close of
# SABLE-gz3v2, at which point it pointed at a closed bead with nothing to
# migrate while the LIVE hold (SABLE-3nymz) quietly lost its bandage. Caught by
# hand. This hook is that catch, mechanized.
#
# WHY A HOOK AND NOT A LINT. Instructional referrals were measured against the
# real bd corpus at roughly one flag every three-to-four closes, worst case two
# at once — comfortably inside "rare enough to be read", which is the bar a
# close-time interruption has to clear. It is checkable at retirement time, so
# it is enforced there rather than swept up post-hoc. (That figure is EVIDENCE
# OF VIABILITY, not a tuning target: the detector is deliberately tuned LOOSE,
# because a false flag costs one read while a miss costs a recycled agent
# honouring a dead instruction indefinitely.)
#
# NEVER BLOCKS. Fail-open on the decision, loud on the report (standing
# discipline 7): a sweep that could not run says COULD NOT ASSESS out loud,
# because a failed sweep that looks like a clean one is worse than no sweep.
#
# KNOWN LIMIT, shipped in the flag text itself: this sees instructions that NAME
# a retired identifier. It cannot see one invalidated because a code path
# stopped being reached (SABLE-3nymz). A detector whose limits are undocumented
# gets trusted past them.

set -uo pipefail

# shellcheck source=lib-hook-trace.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib-hook-trace.sh"
sable_trace_entry close-decay-sweep

HOOK_INPUT=$(sable_trace_read_stdin) || exit 0

COMMAND=$(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print((d.get('tool_input') or {}).get('command', '') or '')
" 2>/dev/null) || COMMAND=""

[ -z "$COMMAND" ] && exit 0

# Only act on bd close — the one command that retires a bead id.
printf '%s' "$COMMAND" | grep -q '^[[:space:]]*bd close' || exit 0

# Extract the bead IDs being closed. Same tokenizer contract as tdd-gate.sh:
# shlex-tokenize, keep POSITIONAL tokens (stop at the first flag, so flag values
# like `--reason docs-only` can never be mistaken for ids) that match the bead-ID
# shape. Pipes, redirects and chain operators are not id-shaped, so they drop out.
BEAD_IDS=$(BEAD_CMD="$COMMAND" python3 -c "
import os, re, shlex
cmd = re.sub(r'^\s*bd close\s+', '', os.environ.get('BEAD_CMD', ''))
try:
    tokens = shlex.split(cmd)
except ValueError:
    tokens = []
positional = []
for t in tokens:
    if t.startswith('-'):
        break
    positional.append(t)
ID = re.compile(r'^[A-Za-z][A-Za-z0-9-]*-[a-z0-9]+(\.[0-9]+)?\$')
print(' '.join(t for t in positional if ID.match(t)))
" 2>/dev/null) || BEAD_IDS=""

[ -z "$BEAD_IDS" ] && exit 0

# Resolve the sweeper via PATH. Hooks are COPY-installed into ~/.claude/hooks,
# so there is no repo checkout beside this file to reach for; bin/sable-* is
# symlinked into ~/.local/bin by sable-bin-install, which is the supported
# resolution. If it is absent, say so out loud rather than exiting silently —
# an unrunnable sweep must not look like a clean one.
SWEEPER="$(command -v sable-identifier-decay 2>/dev/null || true)"

emit_context() {
  MSG="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': os.environ.get('MSG', '')
    }
}))
"
  exit 0
}

if [ -z "$SWEEPER" ]; then
  emit_context "⚠ identifier-decay: COULD NOT ASSESS ${BEAD_IDS} — sable-identifier-decay is not on PATH (run bin/sable-bin-install). This is NOT a clean result: nothing was checked. Not blocking (fail-open)."
fi

# shellcheck disable=SC2086 — BEAD_IDS is a whitespace-separated id list by construction
OUT=$("$SWEEPER" $BEAD_IDS 2>&1)
RC=$?

# rc 0 = swept (OUT empty means genuinely nothing to flag — stay silent).
# rc 3 = could-not-assess; OUT already carries the loud notice.
# any other rc = the sweeper itself broke; report that, still without blocking.
if [ "$RC" -ne 0 ] && [ "$RC" -ne 3 ]; then
  emit_context "⚠ identifier-decay: COULD NOT ASSESS ${BEAD_IDS} — sable-identifier-decay exited ${RC}: $(printf '%s' "$OUT" | head -3). This is NOT a clean result: nothing was checked. Not blocking (fail-open)."
fi

[ -z "$OUT" ] && exit 0
emit_context "$OUT"
