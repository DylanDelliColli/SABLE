#!/usr/bin/env bash
# test-sable-onboarding-skill.sh — skill-contract test for /sable-onboarding
# (SABLE-gn7a.4). A skill body is prose Claude executes, so the "integration"
# verified here is that skills/sable-onboarding/SKILL.md documents the BINDING
# gated-apply sequence exactly:
#
#   * the numbered sequence 0-7 is present and in canonical order, with the S6
#     launch-branch confirmation FIRST (step 0, before any binary report/apply)
#     and the proof run marked optional / default-yes / operator-locked,
#   * every APPLY step names its delegate (install.sh --project / bd init /
#     sable-doctor --project) — onboarding never hand-mutates,
#   * every apply is re-verified via `sable-onboard --check <id>`,
#   * the two-authored-artifacts invariant (.sable lines + the generated
#     workflow, nothing else) is stated,
#   * the detached-HEAD / unborn / no-remote git-state remedies are present
#     verbatim (the step-0 hard stops).
#
# Integration-layer coverage of the flow actually RUNNING lands in SABLE-gn7a.5's
# E2E suite; this suite is the skill-contract layer.
#
# Run with:
#   bash hooks/test/test-sable-onboarding-skill.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SKILL="$REPO/skills/sable-onboarding/SKILL.md"

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

# 1. file exists + declares its invocation name
assert_file "$SKILL" "/sable-onboarding skill file exists"
assert_grep "$SKILL" "name: sable-onboarding" "/sable-onboarding declares name: sable-onboarding"

# ---------------------------------------------------------------------------
# test_onboarding_skill_documents_gated_apply_sequence
# ---------------------------------------------------------------------------

# 2. the numbered sequence 0-7 is all present
for n in 0 1 2 3 4 5 6 7; do
  assert_grep "$SKILL" "## $n\." "/sable-onboarding documents step $n"
done

# 3. the sequence is in canonical order (positions 0..7 strictly increasing)
order_ok="$(SKILL="$SKILL" python3 -c "
import os
text = open(os.environ['SKILL']).read().lower()
pos = [text.find('## %d.' % n) for n in range(8)]
print('ok' if all(p >= 0 for p in pos) and pos == sorted(pos) and len(set(pos)) == 8 else 'no')
" 2>/dev/null)"
if [ "$order_ok" = "ok" ]; then pass "/sable-onboarding lists steps 0-7 in canonical order"; else fail "/sable-onboarding lists steps 0-7 in canonical order" "got '$order_ok'"; fi

# 4. S6 branch confirmation is FIRST — before the binary report (step 1) and any apply
s6_first="$(SKILL="$SKILL" python3 -c "
import os
text = open(os.environ['SKILL']).read().lower()
s6 = text.find('s6 launch-branch confirmation')
step1 = text.find('## 1.')
print('ok' if s6 >= 0 and step1 >= 0 and s6 < step1 else 'no')
" 2>/dev/null)"
if [ "$s6_first" = "ok" ]; then pass "/sable-onboarding runs the S6 branch confirmation first (step 0, before step 1)"; else fail "/sable-onboarding runs the S6 branch confirmation first (step 0, before step 1)" "got '$s6_first'"; fi
assert_grep "$SKILL" "git state FIRST" "/sable-onboarding runs git-state probes before anything else"

# 5. proof run marked optional / default-yes / operator-locked / skippable
assert_grep "$SKILL" "offered-default-yes" "/sable-onboarding proof run is offered-default-yes"
assert_grep "$SKILL" "default \*\*yes\*\*"  "/sable-onboarding proof run default is yes"
assert_grep "$SKILL" "skippable"            "/sable-onboarding proof run is skippable"
assert_grep "$SKILL" "operator-locked"      "/sable-onboarding proof run is operator-locked"
assert_grep "$SKILL" "never run unconsented" "/sable-onboarding proof run is never run unconsented"
assert_grep "$SKILL" "quick-tier"           "/sable-onboarding proof run proposes a quick-tier run"
assert_grep "$SKILL" "/sable-plan"          "/sable-onboarding proof run drives /sable-plan"
assert_grep "$SKILL" "sample bead"          "/sable-onboarding proof run creates then closes a sample bead"

# 6. every APPLY names its delegate — onboarding never hand-mutates
assert_grep "$SKILL" "install.sh --project"  "/sable-onboarding delegates install to install.sh --project"
assert_grep "$SKILL" "bd init"               "/sable-onboarding delegates the beads workspace to bd init"
assert_grep "$SKILL" "sable-doctor --project" "/sable-onboarding delegates the green verdict to sable-doctor --project"

# 7. every apply is re-verified via sable-onboard --check <id>
assert_grep "$SKILL" "sable-onboard --check" "/sable-onboarding re-verifies each apply via sable-onboard --check"
# the concrete check ids the re-verify closes
for id in install-scope beads-workspace sable-contract ci-verify; do
  assert_grep "$SKILL" "sable-onboard --check $id" "/sable-onboarding re-verifies check id: $id"
done

# 8. the two-authored-artifacts invariant is stated (.sable lines + the workflow, nothing else)
assert_grep "$SKILL" "authors ONLY two artifacts" "/sable-onboarding states the two-authored-artifacts invariant"
assert_grep "$SKILL" "named delegation"           "/sable-onboarding states every other mutation is a named delegation"

# 9. the .sable testCommand execute-once gate (propose -> confirm -> run once -> only then write)
assert_grep "$SKILL" "execute-once"   "/sable-onboarding gates .sable testCommand behind an execute-once run"
assert_grep "$SKILL" "sable_stack_detect" "/sable-onboarding drives the .sable writer (sable_stack_detect.py)"
assert_grep "$SKILL" "sable_ci_template"  "/sable-onboarding drives the CI generator (sable_ci_template)"
assert_grep "$SKILL" "never overwrite"    "/sable-onboarding never overwrites an existing ci-verify workflow"

# 10. the detached-HEAD / unborn / no-remote git-state remedies, present VERBATIM
assert_grep "$SKILL" "'HEAD' is not a branch name"          "/sable-onboarding carries the detached-HEAD remedy verbatim"
assert_grep "$SKILL" "make an initial commit before onboarding" "/sable-onboarding carries the unborn-branch remedy verbatim"
assert_grep "$SKILL" "No git remote"                        "/sable-onboarding carries the no-remote remedy verbatim"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
