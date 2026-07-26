#!/usr/bin/env bash
# test-bd-inline-body-guard.sh — INTEGRATION test for
# hooks/multi-manager/inline-body-guard.sh (SABLE-qwthx), against a real bd
# store with no mocks or stubs anywhere.
#
# WHY THIS EXISTS SEPARATELY FROM THE UNIT SUITE (bin/test_sable_inline_body_
# guard.py). The unit suite asserts classify() returns the right verdict on a
# string. That is not the property the bead is about. The property is that a
# REAL substitution never fires against a REAL bd invocation when the guard
# is consulted -- and a decision string is only evidence of that if (a) the
# hazard is real (the same command line, run WITHOUT the guard, really does
# execute the substituted command) and (b) the harness really withholds the
# command on a deny, exactly as Claude Code's PreToolUse contract does.
#
#   NEGATIVE CONTROL: the exact hazardous command line, run directly (no
#   guard consulted) — proves the sentinel-creating substitution is real and
#   the bead really gets created. Without this, "sentinel absent" in the
#   guarded case would be equally consistent with the substitution never
#   having been dangerous in the first place (the SABLE-ms8y false-green
#   shape) or with bd itself refusing for an unrelated reason.
#
#   GUARDED CASE: same command, real hook, honoured verdict (denied => the
#   command is never run) — assert BOTH that no bead was created in the
#   sandboxed BEADS_DB AND that the specific sentinel path this test chose
#   does not exist on the real filesystem (SABLE-jd5fj.15: attributable
#   absence, never a global "nothing changed" sniff).
#
# Also carries the settings-snippet.json regression named in this bead's
# FINALIZED IMPLEMENTATION SPEC (the "f3.2 shape", IRON RULE priority 1):
# after wiring inline-body-guard.sh into templates/multi-manager/settings-
# snippet.json, the multi-manager PreToolUse:Bash entry count must not have
# DECREASED relative to the pre-existing entries this test enumerates by
# name — a regression here would mean the edit silently dropped a sibling
# hook rather than adding this one.
#
# Run with:
#   bash hooks/test/test-bd-inline-body-guard.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/inline-body-guard.sh"
SNIPPET="$REPO/templates/multi-manager/settings-snippet.json"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

if [ ! -f "$HOOK" ]; then
  fail "hook present at hooks/multi-manager/inline-body-guard.sh" "not found at $HOOK"
  echo "Tests: 1 | Passed: 0 | Failed: 1"
  exit 1
fi

# ===========================================================================
# REGRESSION — settings-snippet.json PreToolUse:Bash entry count did not
# decrease (the f3.2 shape). Pre-existing entries enumerated by name so a
# dropped sibling fails loudly instead of surviving as a smaller-but-still-
# nonzero count.
# ===========================================================================
PRE_EXISTING_BASH_HOOKS="control-trace.sh mode-interlock.sh tree-claim.sh stash-worktree-guard.sh worktree-placement-guard.sh read-guard.sh notes-clobber-guard.sh pre-push-rebase-test.sh close-decay-sweep.sh"

if [ -f "$SNIPPET" ]; then
  SNIPPET_CHECK=$(python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print('BADJSON:' + str(e)); sys.exit(0)
entries = next((e for e in d.get('hooks', {}).get('PreToolUse', []) if e.get('matcher') == 'Bash'), None)
if entries is None:
    print('NOMATCHER'); sys.exit(0)
cmds = [h.get('command', '') for h in entries.get('hooks', [])]
print('COUNT:' + str(len(cmds)))
for c in cmds:
    print('CMD:' + c)
" "$SNIPPET")

  if printf '%s\n' "$SNIPPET_CHECK" | grep -q '^BADJSON'; then
    fail "settings-snippet.json is valid JSON" "$SNIPPET_CHECK"
  elif printf '%s\n' "$SNIPPET_CHECK" | grep -q '^NOMATCHER'; then
    fail "settings-snippet.json has a PreToolUse Bash matcher entry" ""
  else
    COUNT=$(printf '%s\n' "$SNIPPET_CHECK" | sed -n 's/^COUNT://p')
    MISSING=""
    for h in $PRE_EXISTING_BASH_HOOKS; do
      printf '%s\n' "$SNIPPET_CHECK" | grep -q "^CMD:.*$h\$" || MISSING="$MISSING $h"
    done
    if [ -z "$MISSING" ]; then
      pass "settings-snippet.json: every pre-existing PreToolUse:Bash hook is still present"
    else
      fail "settings-snippet.json: every pre-existing PreToolUse:Bash hook is still present" "missing:$MISSING"
    fi

    if printf '%s\n' "$SNIPPET_CHECK" | grep -q '^CMD:.*inline-body-guard\.sh$'; then
      pass "settings-snippet.json: inline-body-guard.sh is wired into PreToolUse:Bash"
    else
      fail "settings-snippet.json: inline-body-guard.sh is wired into PreToolUse:Bash" ""
    fi

    EXPECTED_MIN=$(( $(printf '%s\n' $PRE_EXISTING_BASH_HOOKS | wc -w) + 1 ))
    if [ -n "$COUNT" ] && [ "$COUNT" -ge "$EXPECTED_MIN" ]; then
      pass "settings-snippet.json: PreToolUse:Bash entry count did not decrease ($COUNT >= $EXPECTED_MIN)"
    else
      fail "settings-snippet.json: PreToolUse:Bash entry count did not decrease" "count='$COUNT' expected>=$EXPECTED_MIN"
    fi
  fi
else
  fail "settings-snippet.json present" "not found at $SNIPPET"
fi

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP (integration): bd not found on PATH — the real-bd half of this suite requires it (no mocks)"
  echo
  echo "=========================================="
  echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
  echo "=========================================="
  if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
  exit 0
fi

# ===========================================================================
# Real-bd fixture: a throwaway sandboxed BEADS_DB, never the live repo DB.
# ===========================================================================
FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

SCRATCH_BEADS_DIR="$FIXTURE_DIR/sandbox"
mkdir -p "$SCRATCH_BEADS_DIR"
( cd "$SCRATCH_BEADS_DIR" && BD_NON_INTERACTIVE=1 bd init --prefix=ibg \
    --non-interactive --skip-agents --skip-hooks --quiet >/dev/null 2>&1 )

if [ ! -d "$SCRATCH_BEADS_DIR/.beads" ]; then
  echo "SKIP (integration): could not init a sandboxed bd store at $SCRATCH_BEADS_DIR"
  echo
  echo "=========================================="
  echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
  echo "=========================================="
  if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
  exit 0
fi

export BEADS_DB="$SCRATCH_BEADS_DIR/.beads"

sandbox_count() { bd count 2>/dev/null; }

# json <command> -> PreToolUse hook input
json() {
  python3 -c "
import json, sys
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': sys.argv[1]}, 'hook_event_name': 'PreToolUse'}))
" "$1"
}

# run_hook <command> — drive the real hook with a real PreToolUse payload.
run_hook() {
  json "$1" | env BEADS_DB="$BEADS_DB" bash "$HOOK" 2>/dev/null
}

# decision_of <hook stdout> -> 'deny' | 'allow' | '<none>' | '<malformed>'
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

# ===========================================================================
# NEGATIVE CONTROL — the sentinel-creating substitution is real and really
# fires when the guard is not consulted.
# ===========================================================================
SENTINEL_CTRL="$FIXTURE_DIR/sentinel-negative-control-$$"
[ -e "$SENTINEL_CTRL" ] && rm -f "$SENTINEL_CTRL"

CTRL_CMD='bd create "[int-test] ibg negative control" --description "hazard: `touch '"$SENTINEL_CTRL"'`"'
BEFORE_CTRL="$(sandbox_count)"
eval "$CTRL_CMD" >/dev/null 2>&1
AFTER_CTRL="$(sandbox_count)"

if [ -f "$SENTINEL_CTRL" ]; then
  pass "real bd: NEGATIVE CONTROL — an unguarded backtick substitution really creates the sentinel"
else
  fail "real bd: NEGATIVE CONTROL — an unguarded backtick substitution really creates the sentinel" \
       "sentinel absent after direct eval; the guarded-case absence assertions below would be vacuous"
fi

if [ -n "$AFTER_CTRL" ] && [ -n "$BEFORE_CTRL" ] && [ "$AFTER_CTRL" -gt "$BEFORE_CTRL" ]; then
  pass "real bd: NEGATIVE CONTROL — the unguarded bd create really lands a bead (fixture precondition)"
else
  fail "real bd: NEGATIVE CONTROL — the unguarded bd create really lands a bead (fixture precondition)" \
       "before=$BEFORE_CTRL after=$AFTER_CTRL"
fi

rm -f "$SENTINEL_CTRL"

# ===========================================================================
# GUARDED CASE — same hazard shape, real hook, verdict honoured, nothing
# executes.
# ===========================================================================
SENTINEL_GUARDED="$FIXTURE_DIR/sentinel-guarded-case-$$"
[ -e "$SENTINEL_GUARDED" ] && rm -f "$SENTINEL_GUARDED"

BEFORE_GUARDED="$(sandbox_count)"
GUARDED_CMD='bd create "[int-test] ibg guarded case" --description "hazard: `touch '"$SENTINEL_GUARDED"'`"'
OUT="$(run_hook "$GUARDED_CMD")"
DECISION="$(decision_of "$OUT")"

if [ "$DECISION" = "deny" ]; then
  pass "real hook: DENIED a bd create whose description carries a real sentinel-creating substitution"
else
  fail "real hook: DENIED a bd create whose description carries a real sentinel-creating substitution" \
       "decision='$DECISION' raw='${OUT:-<empty>}'"
fi

if printf '%s' "$OUT" | grep -qF -- "--body-file"; then
  pass "real hook: the denial names the --body-file escape hatch"
else
  fail "real hook: the denial names the --body-file escape hatch" "raw='${OUT:-<empty>}'"
fi

# Honour the verdict exactly as the PreToolUse contract does: on deny the
# command never runs. THIS is the assertion the bead is actually about.
if [ "$DECISION" != "deny" ]; then
  eval "$GUARDED_CMD" >/dev/null 2>&1
fi

if [ ! -e "$SENTINEL_GUARDED" ]; then
  pass "real hook: the specific sentinel path ($SENTINEL_GUARDED) does not exist after the refused write"
else
  fail "real hook: the specific sentinel path does not exist after the refused write" "sentinel unexpectedly present"
fi

AFTER_GUARDED="$(sandbox_count)"
if [ "$AFTER_GUARDED" = "$BEFORE_GUARDED" ]; then
  pass "real hook: no bead was created in the sandboxed BEADS_DB for the refused write"
else
  fail "real hook: no bead was created in the sandboxed BEADS_DB for the refused write" \
       "before=$BEFORE_GUARDED after=$AFTER_GUARDED"
fi

# ===========================================================================
# POSITIVE CONTROL — the safe --body-file route is allowed AND works.
# ===========================================================================
BODY_FILE="$FIXTURE_DIR/safe-body.md"
printf 'see `bd hooks install` for context (this is now inert -- it is file content, never parsed by a shell)\n' > "$BODY_FILE"

SAFE_CMD="bd create \"[int-test] ibg positive control\" --body-file $BODY_FILE"
OUT="$(run_hook "$SAFE_CMD")"
DECISION="$(decision_of "$OUT")"

if [ "$DECISION" = "<none>" ]; then
  pass "real hook: POSITIVE CONTROL — --body-file is allowed silently (guard does not refuse everything)"
else
  fail "real hook: POSITIVE CONTROL — --body-file is allowed silently (guard does not refuse everything)" \
       "decision='$DECISION' raw='${OUT:-<empty>}'"
fi

BEFORE_SAFE="$(sandbox_count)"
eval "$SAFE_CMD" >/dev/null 2>&1
AFTER_SAFE="$(sandbox_count)"
if [ -n "$AFTER_SAFE" ] && [ -n "$BEFORE_SAFE" ] && [ "$AFTER_SAFE" -gt "$BEFORE_SAFE" ]; then
  pass "real bd: the allowed --body-file create actually lands a bead"
else
  fail "real bd: the allowed --body-file create actually lands a bead" "before=$BEFORE_SAFE after=$AFTER_SAFE"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
