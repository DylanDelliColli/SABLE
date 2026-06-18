#!/usr/bin/env bash
# test-sable-skills.sh — Integration test for the /sable-plan and /sable-execute mode-flip
# skills (SABLE-cav.1 acceptance: "/sable-plan writes mode-state.json with
# mode=planning and loads the planning persona; /sable-execute writes mode=execution
# and loads the overseer persona").
#
# A skill body is prose Claude executes, so the "integration" verified here is:
#   1. each skill file exists with the right invocation name,
#   2. each skill is WIRED to the shared mechanism (`sable-mode set <mode>`),
#   3. each skill loads its persona (planning producers / execution overseer),
#   4. the documented mechanism, run end-to-end, produces the correct
#      mode-state file (exercised through bin/sable-mode against a temp state).
#
# Run with:
#   bash hooks/test/test-sable-skills.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PLAN_SKILL="$REPO/skills/sable-plan/SKILL.md"
EXEC_SKILL="$REPO/skills/sable-execute/SKILL.md"
MODE_BIN="$REPO/bin/sable-mode"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

assert_file() { if [ -f "$1" ]; then pass "$2"; else fail "$2" "missing: $1"; fi; }
assert_grep() {
  # file pattern name
  if grep -qi -- "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3" "pattern not found: $2"; fi
}
assert_no_grep() {
  # file pattern name
  if grep -qi -- "$2" "$1" 2>/dev/null; then fail "$3" "pattern unexpectedly present: $2"; else pass "$3"; fi
}

# 1. files exist
assert_file "$PLAN_SKILL" "/sable-plan skill file exists"
assert_file "$EXEC_SKILL" "/sable-execute skill file exists"

# 2. invocation name in frontmatter
assert_grep "$PLAN_SKILL" "name: sable-plan"    "/sable-plan declares name: sable-plan"
assert_grep "$EXEC_SKILL" "name: sable-execute" "/sable-execute declares name: sable-execute"

# 3. wired to the shared mechanism
assert_grep "$PLAN_SKILL" "sable-mode set planning"  "/sable-plan invokes sable-mode set planning"
assert_grep "$EXEC_SKILL" "sable-mode set execution" "/sable-execute invokes sable-mode set execution"

# 4. persona loaded
assert_grep "$PLAN_SKILL" "planning"  "/sable-plan loads the planning persona"
assert_grep "$PLAN_SKILL" "producer"  "/sable-plan references the Tier-2 producers"
assert_grep "$EXEC_SKILL" "execution" "/sable-execute loads the execution persona"
assert_grep "$EXEC_SKILL" "oversee"   "/sable-execute references overseeing the managers"

# /sable-execute documents the soft handoff-readiness gate (substage + open-questions)
assert_grep "$EXEC_SKILL" "open-question" "/sable-execute documents the open-questions handoff gate"
assert_grep "$EXEC_SKILL" "substage"      "/sable-execute checks the planning substage before handoff"

# 5. end-to-end mechanism: the documented command flips state correctly
STATE_TMP="$(mktemp -u)"
SABLE_MODE_STATE="$STATE_TMP" "$MODE_BIN" set planning --fleet sherlock,columbo,gaudi,victor >/dev/null 2>&1
assert_planning="$(SABLE_MODE_STATE="$STATE_TMP" "$MODE_BIN" get 2>/dev/null)"
if [ "$assert_planning" = "planning" ]; then pass "documented /sable-plan mechanism yields mode=planning"; else fail "documented /sable-plan mechanism yields mode=planning" "got '$assert_planning'"; fi

SABLE_MODE_STATE="$STATE_TMP" "$MODE_BIN" set execution --fleet optimus,tarzan,chuck >/dev/null 2>&1
assert_exec="$(SABLE_MODE_STATE="$STATE_TMP" "$MODE_BIN" get 2>/dev/null)"
if [ "$assert_exec" = "execution" ]; then pass "documented /sable-execute mechanism yields mode=execution"; else fail "documented /sable-execute mechanism yields mode=execution" "got '$assert_exec'"; fi
rm -f "$STATE_TMP"

# 6. staged-planning substages: all five present, named in canonical order
for s in framing research architecture test-strategy decomposition; do
  assert_grep "$PLAN_SKILL" "$s" "/sable-plan names substage: $s"
done
order_ok="$(PLAN_SKILL="$PLAN_SKILL" python3 -c "
import os
text = open(os.environ['PLAN_SKILL']).read().lower()
stages = ['framing','research','architecture','test-strategy','decomposition']
pos = [text.find(s) for s in stages]
print('ok' if all(p >= 0 for p in pos) and pos == sorted(pos) else 'no')
" 2>/dev/null)"
if [ "$order_ok" = "ok" ]; then pass "/sable-plan lists substages in canonical order"; else fail "/sable-plan lists substages in canonical order" "got '$order_ok'"; fi

# 7. wired to the substage machine + the interlock backlog gate
assert_grep "$PLAN_SKILL" "sable-mode substage advance" "/sable-plan advances substages via sable-mode"
assert_grep "$PLAN_SKILL" "interlock"                    "/sable-plan references the interlock backlog gate"

# 8. end-to-end: documented step-1 command initializes substage=framing
STATE_TMP2="$(mktemp -u)"
SABLE_MODE_STATE="$STATE_TMP2" "$MODE_BIN" set planning --fleet sherlock,columbo,gaudi,victor >/dev/null 2>&1
init_sub="$(SABLE_MODE_STATE="$STATE_TMP2" "$MODE_BIN" substage get 2>/dev/null)"
if [ "$init_sub" = "framing" ]; then pass "documented /sable-plan step 1 initializes substage=framing"; else fail "documented /sable-plan step 1 initializes substage=framing" "got '$init_sub'"; fi
rm -f "$STATE_TMP2"

# 9. v2 one-window topology (SABLE-uz9.5 / uz9.4 option A)
assert_grep "$PLAN_SKILL" "lincoln"          "/sable-plan addresses Lincoln (v2 identity)"
assert_grep "$PLAN_SKILL" "subagent"         "/sable-plan spawns producers as subagents"
assert_grep "$PLAN_SKILL" "gaudi.*skill\|skill.*gaudi" "/sable-plan runs gaudi as an inline skill"
assert_grep "$EXEC_SKILL" "lincoln"          "/sable-execute addresses Lincoln (v2 identity)"
assert_grep "$EXEC_SKILL" "dispatch their own workers" "/sable-execute: managers dispatch their own workers (native spawn, SABLE-uz9.11)"
assert_no_grep "$EXEC_SKILL" "Dispatching-for"  "/sable-execute drops the old DISPATCH-REQUEST relay attribution"
assert_grep "$EXEC_SKILL" "run_in_background" "/sable-execute spawns managers as invisible background subagents"
assert_grep "$EXEC_SKILL" "ALWAYS background" "/sable-execute spawns managers in the background (never blocks the chat)"
assert_grep "$PLAN_SKILL" "run_in_background" "/sable-plan spawns producers in the background"
assert_grep "$EXEC_SKILL" "Chuck terminal"   "/sable-execute reminds the operator about the Chuck terminal"
assert_grep "$EXEC_SKILL" "pushes approved work itself" "/sable-execute: managers push their own approved work (Lincoln does not)"
assert_no_grep "$EXEC_SKILL" "execute dispatch requests as" "/sable-execute: Lincoln no longer executes manager dispatch requests"

# 9b. Teams topology branch (SABLE-amj.6)
assert_grep "$EXEC_SKILL" "sable-teams-preflight" "/sable-execute runs the topology preflight"
assert_grep "$EXEC_SKILL" "SABLE_TEAMS"            "/sable-execute documents the SABLE_TEAMS toggle"
assert_grep "$EXEC_SKILL" "TeamCreate"             "/sable-execute teams branch creates the sable team"
assert_grep "$EXEC_SKILL" "agents-teams"           "/sable-execute teams branch inline-spawns from the built teams defs"
assert_grep "$EXEC_SKILL" "no separate Chuck"      "/sable-execute teams branch folds Chuck into the team (no second terminal)"

# 9c. Runtime Team* tools availability probe (SABLE-1qt)
# After preflight prints "teams", Lincoln must probe whether TeamCreate/TeamDelete are
# actually live in the session (not just deferred/disconnected) before entering §2b.
assert_grep "$EXEC_SKILL" "runtime tool-availability probe\|runtime.*probe\|tool-availability probe" \
  "/sable-execute: if preflight=teams, Lincoln probes runtime tool availability (SABLE-1qt)"
assert_grep "$EXEC_SKILL" "Teams tools not yet loaded\|Team.*tools.*not.*loaded\|Team\* tools" \
  "/sable-execute: runtime fallback message instructs operator on Teams tools absence (SABLE-1qt)"

# 10. DECOMPOSITION post-batch-create verification (SABLE-xy1)
assert_grep "$PLAN_SKILL" "bd dep tree"        "/sable-plan DECOMPOSITION verifies edges via bd dep tree"
assert_grep "$PLAN_SKILL" "bd ready"           "/sable-plan DECOMPOSITION sanity-checks bd ready"
assert_grep "$PLAN_SKILL" "bd swarm validate"  "/sable-plan DECOMPOSITION runs bd swarm validate"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
