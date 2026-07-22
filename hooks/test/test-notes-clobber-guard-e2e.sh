#!/usr/bin/env bash
# test-notes-clobber-guard-e2e.sh — INTEGRATION test for
# hooks/multi-manager/notes-clobber-guard.sh (SABLE-sm269), against a real bd
# store with no mocks or stubs anywhere.
#
# WHY THIS EXISTS SEPARATELY FROM THE UNIT SUITE. The unit suite asserts the
# hook emits the right decision string. That is not the property the bead is
# about. The property is that the CONTENT SURVIVES — and a decision string is
# only evidence of survival if (a) the underlying write really is destructive
# against this bd, and (b) the harness really withholds the command on a deny.
# Both are asserted here against real beads:
#
#   NEGATIVE CONTROL (the hazard is real): on a throwaway bead, run the exact
#   destructive command WITHOUT the guard and prove the prior notes are gone.
#   Without this, "notes unchanged" in the guarded case would be consistent
#   with bd simply never having been destructive, and the whole suite would be
#   vacuously green — the SABLE-ms8y false-green shape.
#
#   GUARDED CASE: same command, real hook, honoured verdict (denied => the
#   command is not run, exactly as Claude Code's PreToolUse contract does it),
#   then assert the notes are BYTE-IDENTICAL to the pre-write snapshot.
#
#   POSITIVE CONTROL: an --append-notes write is allowed by the hook, is
#   actually executed, and both the prior notes and the appended text are
#   present afterwards — so the guard is not passing by refusing everything.
#
# Run with:
#   bash hooks/test/test-notes-clobber-guard-e2e.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/notes-clobber-guard.sh"

if [ ! -f "$HOOK" ]; then
  echo "FAIL: hook not found at $HOOK"
  exit 2
fi

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP: bd not found on PATH — this suite requires a real bd (no mocks)"
  exit 0
fi

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

SEED_NOTES="WIP-CLAIMS: hooks/multi-manager/notes-clobber-guard.sh,hooks/test/test-notes-clobber-guard.sh
sm269 e2e seed notes — this line is the content whose survival is under test."

# notes_of <bead-id> — the bead's current notes, verbatim.
notes_of() {
  bd show "$1" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if isinstance(d, list):
    d = d[0] if d else {}
sys.stdout.write(d.get('notes') or '')
"
}

# run_hook <command> — drive the real hook with a real PreToolUse payload.
run_hook() {
  python3 -c "
import json, sys
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': sys.argv[1]}, 'hook_event_name': 'PreToolUse'}))
" "$1" | env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE -u SABLE_WORKER_PANE -u SABLE_BEAD \
       bash "$HOOK" 2>/dev/null
}

# decision_of <hook stdout> — 'deny' | 'allow' | '<none>'
decision_of() {
  printf '%s' "$1" | python3 -c "
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print('<none>'); sys.exit(0)
try:
    print((json.loads(raw).get('hookSpecificOutput') or {}).get('permissionDecision') or '<malformed>')
except Exception:
    print('<malformed>')
"
}

make_bead() { # <title-suffix> -> bead id
  bd create --sandbox \
    --title="[int-test] sm269 notes-clobber-guard $1" \
    --description="Scratch bead for the SABLE-sm269 notes-clobber-guard integration test. Safe to close." \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1
}

cleanup_bead() {
  [ -z "${1:-}" ] && return 0
  bd update "$1" --sandbox --notes "[no-test] integration test scratch — safe to close" >/dev/null 2>&1 || true
  bd close "$1" --sandbox >/dev/null 2>&1 || true
}

BEAD_CTRL=""
BEAD_GUARDED=""
trap 'cleanup_bead "$BEAD_CTRL"; cleanup_bead "$BEAD_GUARDED"' EXIT

# ===========================================================================
# NEGATIVE CONTROL — prove `bd update --notes` really is destructive here.
# ===========================================================================
BEAD_CTRL="$(make_bead 'negative control')"
if [ -z "$BEAD_CTRL" ]; then
  echo "SKIP (integration): could not create the negative-control scratch bead"
  exit 0
fi
echo "Integration: negative-control bead = $BEAD_CTRL"

bd update "$BEAD_CTRL" --sandbox --append-notes "$SEED_NOTES" >/dev/null 2>&1
CTRL_BEFORE="$(notes_of "$BEAD_CTRL")"
if printf '%s' "$CTRL_BEFORE" | grep -qF 'WIP-CLAIMS:'; then
  pass "real bd: seeded notes land on the control bead (fixture precondition)"
else
  fail "real bd: seeded notes land on the control bead (fixture precondition)" "notes='$CTRL_BEFORE'"
fi

# UNGUARDED destructive write — the hook is deliberately not consulted.
bd update "$BEAD_CTRL" --sandbox --notes "unrelated bookkeeping note" >/dev/null 2>&1
CTRL_AFTER="$(notes_of "$BEAD_CTRL")"
if [ "$CTRL_AFTER" != "$CTRL_BEFORE" ] && ! printf '%s' "$CTRL_AFTER" | grep -qF 'WIP-CLAIMS:'; then
  pass "real bd: NEGATIVE CONTROL — an unguarded '--notes' write really does destroy the prior notes"
else
  fail "real bd: NEGATIVE CONTROL — an unguarded '--notes' write really does destroy the prior notes" \
       "notes unchanged after the clobber; the survival assertions below would be vacuous. after='$CTRL_AFTER'"
fi

# ===========================================================================
# GUARDED CASE — same write, real hook, verdict honoured, content survives.
# ===========================================================================
BEAD_GUARDED="$(make_bead 'guarded case')"
if [ -z "$BEAD_GUARDED" ]; then
  echo "SKIP (integration): could not create the guarded scratch bead"
  exit 0
fi
echo "Integration: guarded bead = $BEAD_GUARDED"

bd update "$BEAD_GUARDED" --sandbox --append-notes "$SEED_NOTES" >/dev/null 2>&1
BEFORE="$(notes_of "$BEAD_GUARDED")"

CLOBBER_CMD="bd update $BEAD_GUARDED --sandbox --notes \"unrelated bookkeeping note\""
OUT="$(run_hook "$CLOBBER_CMD")"
DECISION="$(decision_of "$OUT")"

if [ "$DECISION" = "deny" ]; then
  pass "real bd: the destructive write against a real bead with real notes is DENIED"
else
  fail "real bd: the destructive write against a real bead with real notes is DENIED" \
       "decision='$DECISION' raw='${OUT:-<empty>}'"
fi

if printf '%s' "$OUT" | grep -qF -- "$BEAD_GUARDED" && printf '%s' "$OUT" | grep -qF -- "--append-notes"; then
  pass "real bd: the denial names the real bead and hands over the --append-notes route"
else
  fail "real bd: the denial names the real bead and hands over the --append-notes route" "raw='${OUT:-<empty>}'"
fi

# Honour the verdict exactly as the PreToolUse contract does: on deny the
# command never runs. THIS is the assertion the bead is actually about.
if [ "$DECISION" != "deny" ]; then
  eval "$CLOBBER_CMD" >/dev/null 2>&1
fi
AFTER="$(notes_of "$BEAD_GUARDED")"

if [ "$AFTER" = "$BEFORE" ]; then
  pass "real bd: notes_survive_notes_clobber — the bead's notes are BYTE-IDENTICAL after the refused write"
else
  fail "real bd: notes_survive_notes_clobber — the bead's notes are BYTE-IDENTICAL after the refused write" \
       "before='$BEFORE' after='$AFTER'"
fi

if printf '%s' "$AFTER" | grep -qF 'WIP-CLAIMS:'; then
  pass "real bd: the WIP-CLAIMS line specifically is still readable off the bead"
else
  fail "real bd: the WIP-CLAIMS line specifically is still readable off the bead" "after='$AFTER'"
fi

# ===========================================================================
# POSITIVE CONTROL — the non-destructive route is allowed AND works.
# ===========================================================================
APPEND_CMD="bd update $BEAD_GUARDED --sandbox --append-notes \"appended by the sm269 positive control\""
OUT="$(run_hook "$APPEND_CMD")"
DECISION="$(decision_of "$OUT")"

if [ "$DECISION" = "<none>" ]; then
  pass "real bd: POSITIVE CONTROL — the --append-notes write is allowed silently (guard does not refuse everything)"
else
  fail "real bd: POSITIVE CONTROL — the --append-notes write is allowed silently (guard does not refuse everything)" \
       "decision='$DECISION' raw='${OUT:-<empty>}'"
fi

eval "$APPEND_CMD" >/dev/null 2>&1
APPENDED="$(notes_of "$BEAD_GUARDED")"

if printf '%s' "$APPENDED" | grep -qF 'WIP-CLAIMS:' \
   && printf '%s' "$APPENDED" | grep -qF 'appended by the sm269 positive control'; then
  pass "real bd: after the allowed append, BOTH the prior notes and the new text are present"
else
  fail "real bd: after the allowed append, BOTH the prior notes and the new text are present" "notes='$APPENDED'"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
