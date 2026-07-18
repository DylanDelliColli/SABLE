#!/usr/bin/env bash
# test-bead-description-gate.sh — Unit tests for bead-description-gate.sh
#
# Pipes synthetic PreToolUse:Bash JSON input to the hook and verifies the
# response (deny vs allow vs nudge). No bd or git state required.
#
# Run with:
#   bash hooks/test/test-bead-description-gate.sh
#
# Exits 0 if all pass, nonzero if any fail.

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/bead-description-gate.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""

# Helpers
make_input() {
  # $1 = command string
  python3 -c "
import json, sys
cmd = sys.argv[1]
print(json.dumps({'tool_input': {'command': cmd}}))
" "$1"
}

# run_hook <env-prefix> <command>
# Outputs: <exit-code><tab><stdout>
run_hook() {
  local env_prefix="$1"
  local command="$2"
  local input
  input=$(make_input "$command")
  local out
  out=$(env -i PATH="$PATH" $env_prefix bash "$HOOK" <<< "$input" 2>/dev/null || echo "RUN_ERR:$?")
  echo -n "$out"
}

assert_allow() {
  # $1 = test name, $2 = env, $3 = command
  local name="$1" env="$2" cmd="$3"
  local out
  out=$(run_hook "$env" "$cmd")
  # Allow = empty stdout (no decision JSON emitted)
  if [ -z "$out" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: empty (allow)"
    echo "  Got:      $out"
  fi
}

assert_deny() {
  # $1 = test name, $2 = env, $3 = command, $4 = substring expected in reason
  local name="$1" env="$2" cmd="$3" expect="$4"
  local out
  out=$(run_hook "$env" "$cmd")
  if echo "$out" | grep -q '"permissionDecision": "deny"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: deny containing '$expect'"
    echo "  Got:      $out"
  fi
}

assert_nudge() {
  # $1 = test name, $2 = env, $3 = command, $4 = substring expected
  local name="$1" env="$2" cmd="$3" expect="$4"
  local out
  out=$(run_hook "$env" "$cmd")
  if echo "$out" | grep -q '"additionalContext"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: nudge containing '$expect'"
    echo "  Got:      $out"
  fi
}

# Build a sherlock-complete description with real newlines (matches what
# `bd create --description="..."` produces when the agent types a multi-line
# heredoc-style quoted string).
COMPLETE_SHERLOCK_DESC=$'## Rationale\nFoo\n\n## Evidence\n### File: src/auth/middleware.ts\n- Symbol: publicPaths\n- Fingerprint: const publicPaths = [\n\n## Proposed approach\nBar\n\n## Scope estimate\nS\n\n## Risk if not addressed\nBaz\n\nTest spec: src/auth/test_middleware.test.ts'

# ---------- Default mode (no agent identity) ----------

# Test 1: non-bd-create commands ignored
assert_allow "ignores non-bd-create" "" "git status"

# Test 2: epic creation skipped
assert_allow "epic exempt" "" "bd create --type=epic --title=foo --description=\"bar\""

# Test 3: missing description in default mode → nudge
assert_nudge "default: missing description nudges" "" "bd create --title=foo" "no --description flag"

# Test 4: vague description in default mode → nudge with missing list
assert_nudge "default: vague description nudges" "" "bd create --title=foo --description=\"do the thing\"" "missing"

# Test 5: full description (file path + test) in default mode → allow
assert_allow "default: complete description allowed" "" "bd create --title=foo --description=\"Update src/foo.ts. Test in src/foo.test.ts.\""

# ---------- Manager mode (CLAUDE_AGENT_NAME set) ----------

MANAGER_ENV="CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager"

# Test 6: missing description in manager mode → DENY
assert_deny "manager: missing description denied" "$MANAGER_ENV" "bd create --title=foo" "no --description"

# Test 7: vague description in manager mode → DENY with missing list
assert_deny "manager: vague description denied" "$MANAGER_ENV" "bd create --title=foo --description=\"do the thing\"" "missing"

# Test 8: full description in manager mode → allow
assert_allow "manager: complete description allowed" "$MANAGER_ENV" "bd create --title=foo --description=\"Update src/foo.ts. Test in src/foo.test.ts.\""

# Test 9: epic creation skipped in manager mode
assert_allow "manager: epic exempt" "$MANAGER_ENV" "bd create --type=epic --title=foo --description=\"bar\""

# ---------- Sherlock-finding label checks (manager mode only) ----------

# Test 10: sherlock-finding label without required sections → DENY listing them
assert_deny "manager: sherlock-finding incomplete denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=sherlock-finding --description=\"Update src/foo.ts. Test in src/foo.test.ts.\"" \
  "Rationale"

# Test 11: sherlock-finding label with all required sections → allow
assert_allow "manager: sherlock-finding complete allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=sherlock-finding --description=\"$COMPLETE_SHERLOCK_DESC\""

# Test 12: sherlock-finding label with everything except Fingerprint → DENY mentioning Fingerprint
PARTIAL_NO_FP=$'## Rationale\nFoo\n\n## Evidence\n### File: src/auth.ts\n- Symbol: foo\n\n## Proposed approach\nBar\n\n## Scope estimate\nS\n\n## Risk if not addressed\nBaz\n\nTest spec: src/auth.test.ts'
assert_deny "manager: sherlock-finding without fingerprint denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=sherlock-finding --description=\"$PARTIAL_NO_FP\"" \
  "Fingerprint"

# Test 13: non-sherlock-finding label, complete description → allow even in manager mode
assert_allow "manager: non-sherlock label allowed when complete" "$MANAGER_ENV" \
  "bd create --title=foo --labels=bug,for-tarzan --description=\"Update src/foo.ts. Test in src/foo.test.ts.\""

# ---------- Columbo-test-spec label checks ----------

# Build a complete columbo-test-spec description — all six required sections,
# Cases includes a bullet with a Why: sub-line. Avoid embedded double quotes
# in fixtures since they terminate the bash-quoted --description argument.
COMPLETE_COLUMBO_SPEC=$'## Feature under test\nPOST /items endpoint creates an item from a JSON body.\n\n## Test file\ntests/items.skel.test.ts\n\n## Test layer\nUNIT\n\n## Cases\n- Case name: rejects empty name\n  - Why: catches the bug where empty strings bypassed the not-null constraint\n  - Inputs: name=empty-string\n  - Expected: 422 with body error=name_required\n\n## Categories\n2, 3\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nMulti-tenant rate limiting (deferred to SABLE-future).'

# Test 14: columbo-test-spec missing ## Cases → DENY mentioning Cases (manager mode)
SPEC_NO_CASES=$'## Feature under test\nPOST /items.\n\n## Test file\ntests/items.skel.test.ts\n\n## Categories\n2, 3\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nNone.'
assert_deny "manager: columbo-test-spec without Cases denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$SPEC_NO_CASES\"" \
  "Cases"

# Test 15: columbo-test-spec missing Why: sub-line in Cases → DENY mentioning Why
SPEC_NO_WHY=$'## Feature under test\nPOST /items.\n\n## Test file\ntests/items.skel.test.ts\n\n## Cases\n- Case name: rejects empty name\n  - Inputs: name=""\n  - Expected: 422\n\n## Categories\n2, 3\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nNone.'
assert_deny "manager: columbo-test-spec without Why denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$SPEC_NO_WHY\"" \
  "Why"

# Test 16: complete columbo-test-spec → allow (manager mode)
assert_allow "manager: columbo-test-spec complete allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$COMPLETE_COLUMBO_SPEC\""

# Test 17: complete columbo-test-spec in default mode → allow (no agent identity)
assert_allow "default: columbo-test-spec complete allowed" "" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$COMPLETE_COLUMBO_SPEC\""

# Test 18: incomplete columbo-test-spec in default mode → nudge (not deny)
assert_nudge "default: columbo-test-spec incomplete nudges" "" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$SPEC_NO_CASES\"" \
  "Cases"

# Test 19: columbo-test-spec coexists with model + for-tarzan labels — complete → allow
assert_allow "manager: columbo-test-spec with sibling labels allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec,model:sonnet,for-tarzan --description=\"$COMPLETE_COLUMBO_SPEC\""

# ---------- Columbo-test-gap label checks ----------

COMPLETE_COLUMBO_GAP=$'## Symptom\nThe existing test for process_refund only covers the success path; nothing exercises partial-failure mid-batch.\n\n## Cited test file\ntests/refund.test.ts — refund_success_block\n\n## Cited source file\nsrc/refund.ts — process_refund\n\n## Existing test quality\nGrade: ★★\nRationale: existing test asserts the full-success path completes; no negative-space assertions.\n\n## Fingerprint\nfor (const item of refundBatch)\n\n## Cases to add\n- Case name: handles 503 mid-batch without losing successful refunds\n  - Why: catches silent partial-failure regression\n  - Inputs: batch of 5 items, gateway returns 200 for items 0-2, 503 for items 3-4\n  - Expected: items 0-2 marked refunded; items 3-4 retained as pending; no double-refund on retry\n\n## Categories\n5, 8\n\n## Risk if not addressed\nA partial-failure mid-batch will silently drop pending refunds, causing customer-visible refund delays and accounting drift.'

# Test 20: columbo-test-gap missing ## Fingerprint → DENY mentioning Fingerprint
GAP_NO_FP=$'## Symptom\nFoo.\n\n## Cited test file\ntests/refund.test.ts\n\n## Cited source file\nsrc/refund.ts\n\n## Cases to add\n- Case name: x\n  - Why: y\n  - Inputs: z\n  - Expected: q\n\n## Categories\n5\n\n## Risk if not addressed\nBar.'
assert_deny "manager: columbo-test-gap without Fingerprint denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$GAP_NO_FP\"" \
  "Fingerprint"

# Test 21: columbo-test-gap missing ## Risk if not addressed → DENY mentioning Risk
GAP_NO_RISK=$'## Symptom\nFoo.\n\n## Cited test file\ntests/refund.test.ts\n\n## Cited source file\nsrc/refund.ts\n\n## Fingerprint\nfor (const item of refundBatch)\n\n## Cases to add\n- Case name: x\n  - Why: y\n  - Inputs: z\n  - Expected: q\n\n## Categories\n5'
assert_deny "manager: columbo-test-gap without Risk denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$GAP_NO_RISK\"" \
  "Risk"

# Test 22: complete columbo-test-gap → allow (manager mode)
assert_allow "manager: columbo-test-gap complete allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$COMPLETE_COLUMBO_GAP\""

# Test 23: complete columbo-test-gap in default mode → allow
assert_allow "default: columbo-test-gap complete allowed" "" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$COMPLETE_COLUMBO_GAP\""

# Test 24: columbo-test-gap incomplete in default mode → nudge (not deny)
assert_nudge "default: columbo-test-gap incomplete nudges" "" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$GAP_NO_FP\"" \
  "Fingerprint"

# Test 25: columbo-test-gap coexists with sibling labels — complete → allow
assert_allow "manager: columbo-test-gap with sibling labels allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-gap,model:opus,for-optimus --description=\"$COMPLETE_COLUMBO_GAP\""

# Test 26: existing sherlock-finding behavior unchanged when columbo gate is also active
assert_allow "regression: sherlock-finding still allowed after columbo gate added" "$MANAGER_ENV" \
  "bd create --title=foo --labels=sherlock-finding --description=\"$COMPLETE_SHERLOCK_DESC\""

# Test 27: cross-label — bead carries BOTH columbo-test-spec and sherlock-finding.
# Both gates fire; description must satisfy both contracts. Composing the
# COMPLETE_SHERLOCK_DESC + columbo sections produces a description that
# passes everything. Confirms the gate ANDs label requirements rather than
# silently picking one and ignoring the other.
DUAL_DESC=$'## Rationale\nFoo\n\n## Evidence\n### File: src/auth.ts\n- Symbol: foo\n- Fingerprint: const foo = [\n\n## Proposed approach\nBar\n\n## Scope estimate\nS\n\n## Risk if not addressed\nBaz\n\n## Feature under test\nAuth middleware.\n\n## Test file\ntests/auth.skel.test.ts\n\n## Test layer\nUNIT\n\n## Cases\n- Case name: rejects unsigned token\n  - Why: catches signature-bypass regression\n  - Inputs: token with empty signature\n  - Expected: 401 with reason invalid_signature\n\n## Categories\n3, 10\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nRefresh-token rotation.'
assert_allow "manager: dual-label spec + sherlock-finding allowed when both contracts satisfied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec,sherlock-finding --description=\"$DUAL_DESC\""

# Test 28: cross-label — same dual-label bead but missing columbo's Cases →
# DENY mentioning Cases (proves columbo gate fires even when sherlock gate
# would otherwise pass).
DUAL_NO_CASES=$'## Rationale\nFoo\n\n## Evidence\n### File: src/auth.ts\n- Symbol: foo\n- Fingerprint: const foo = [\n\n## Proposed approach\nBar\n\n## Scope estimate\nS\n\n## Risk if not addressed\nBaz\n\n## Feature under test\nAuth middleware.\n\n## Test file\ntests/auth.skel.test.ts\n\n## Test layer\nUNIT\n\n## Categories\n3\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nNone.'
assert_deny "manager: dual-label spec + sherlock-finding denied when columbo Cases missing" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec,sherlock-finding --description=\"$DUAL_NO_CASES\"" \
  "Cases"

# Test 29: columbo-test-spec without ## Test layer → DENY mentioning Test layer (manager mode)
SPEC_NO_LAYER=$'## Feature under test\nPOST /items.\n\n## Test file\ntests/items.skel.test.ts\n\n## Cases\n- Case name: rejects empty name\n  - Why: catches the bug\n  - Inputs: name=empty-string\n  - Expected: 422\n\n## Categories\n2, 3\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nNone.'
assert_deny "manager: columbo-test-spec without Test layer denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$SPEC_NO_LAYER\"" \
  "Test layer"

# Test 30: columbo-test-gap without ## Existing test quality → DENY mentioning Existing test quality (manager mode)
GAP_NO_QUALITY=$'## Symptom\nThe existing test only covers the success path.\n\n## Cited test file\ntests/refund.test.ts\n\n## Cited source file\nsrc/refund.ts\n\n## Fingerprint\nfor (const item of refundBatch)\n\n## Cases to add\n- Case name: handles 503 mid-batch\n  - Why: catches partial failure\n  - Inputs: batch of 5\n  - Expected: items partial state\n\n## Categories\n5\n\n## Risk if not addressed\nSilent partial-failure regression.'
assert_deny "manager: columbo-test-gap without Existing test quality denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$GAP_NO_QUALITY\"" \
  "Existing test quality"

# ---------- --body-file / batch mode tests (SABLE-e8w, SABLE-bvw) ----------

# Create temp body files for the --body-file tests
GOOD_BODY=$(mktemp /tmp/good_body.XXXXXX.md)
BAD_BODY=$(mktemp /tmp/bad_body.XXXXXX.md)
trap 'rm -f "$GOOD_BODY" "$BAD_BODY"' EXIT

# Good body: passes the Fresh Agent Test — has file paths and test spec
cat > "$GOOD_BODY" << 'BODYEOF'
## Problem
The bead-description-gate.sh hook false-positives on bd create --body-file
because it requires --description inline. Fix: detect --body-file early and
read the referenced file, then apply the same quality checks.

## Approach
Modify hooks/bead-description-gate.sh lines 84-106 to detect --body-file,
--graph, --file, --stdin before the --description check. For --body-file PATH
(not "-"), read the file and run DESC checks against its contents.

## Test spec
Unit: hooks/test/test-bead-description-gate.sh — assert_allow for
--body-file with a good file, assert_deny for --body-file with a bad file.
Integration: same harness — writes real temp files to /tmp, runs the real
hook via bash, checks exit behavior and JSON output.

## Acceptance criteria
- bd create --body-file /tmp/good.md with compliant content exits 0 (allow).
- bd create --body-file /tmp/bad.md with non-compliant content is denied/nudged.
- bd create --stdin / --graph / --file are never hard-denied.
- bd create without any description source remains gated exactly as before.
BODYEOF

# Bad body: missing test spec and file paths
cat > "$BAD_BODY" << 'BODYEOF'
## Problem
Something is broken somewhere. Fix it.
BODYEOF

MANAGER_ENV="CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager"

# Test 31: --body-file good content (manager mode) → allow
assert_allow "body-file: good content allowed (manager)" "$MANAGER_ENV" \
  "bd create --title=foo --body-file $GOOD_BODY"

# Test 32: --body-file bad content (manager mode) → deny (same verdict as inline bad desc)
assert_deny "body-file: bad content denied (manager)" "$MANAGER_ENV" \
  "bd create --title=foo --body-file $BAD_BODY" \
  "missing"

# Test 33: --body-file good content (default mode) → allow
assert_allow "body-file: good content allowed (default)" "" \
  "bd create --title=foo --body-file $GOOD_BODY"

# Test 34: --body-file bad content (default mode) → nudge
assert_nudge "body-file: bad content nudges (default)" "" \
  "bd create --title=foo --body-file $BAD_BODY" \
  "missing"

# Test 35: --body-file - (stdin) → nudge only, no deny (batch/stdin mode)
assert_nudge "body-file -: nudge only, no deny (manager)" "$MANAGER_ENV" \
  "bd create --title=foo --body-file -" \
  "batch/stdin mode"

# Test 36: --graph FILE → nudge only, no deny (manager mode)
assert_nudge "graph: nudge only, no deny (manager)" "$MANAGER_ENV" \
  "bd create --graph /tmp/g.json" \
  "batch/stdin mode"

# Test 37: --file FILE → nudge only, no deny (manager mode)
assert_nudge "file-flag: nudge only, no deny (manager)" "$MANAGER_ENV" \
  "bd create --file /tmp/batch.md" \
  "batch/stdin mode"

# Test 38: --stdin → nudge only, no deny (manager mode)
assert_nudge "stdin: nudge only, no deny (manager)" "$MANAGER_ENV" \
  "bd create --title=foo --stdin" \
  "batch/stdin mode"

# Test 39: plain create with no description source still denied (regression guard)
assert_deny "regression: plain create no desc still denied (manager)" "$MANAGER_ENV" \
  "bd create --title=foo" \
  "no --description"

# ---------- Docs-only path tests (SABLE-ue4) ----------

# Test 40: docs-only description citing feedback/foo.md + [no-test] → passes file-path check
DOCS_DESC="Update feedback/bead-quality.md to clarify the Fresh Agent Test. [no-test] — docs-only edit, no code changed."
assert_allow "docs: feedback/ path passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$DOCS_DESC\""

# Test 41: docs-only description citing docs/ path → passes
DOCS2_DESC="Revise docs/architecture.md section on dispatch protocol. [no-test] — documentation only."
assert_allow "docs: docs/ path passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$DOCS2_DESC\""

# Test 42: description citing a .md file path → passes
MD_PATH_DESC="Rewrite CLAUDE.md workflow section for clarity. [no-test] — docs only, no source changes."
assert_allow "docs: .md extension passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$MD_PATH_DESC\""

# Test 43: description citing a .json file path → passes
JSON_PATH_DESC="Update hooks/test/fixtures/sample.json to add new test vectors. Test in hooks/test/test-bead-description-gate.sh."
assert_allow "docs: .json extension passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$JSON_PATH_DESC\""

# Test 44: description with NO path of any kind → still flagged (regression guard)
assert_nudge "docs: pathless description still flagged (default)" "" \
  "bd create --title=foo --description=\"Fix the thing. Test in the test file.\"" \
  "file paths"

# ---------- bin/ + .yml + dot-dir path tests (SABLE-fupt) ----------

# Test 44b: description citing only bin/<executable> (extensionless) → passes
BIN_PATH_DESC="Fix bin/sable-view to handle the new flag. TDD red-green confirms fix."
assert_allow "docs: bin/ extensionless executable passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$BIN_PATH_DESC\""

# Test 44c: description citing only a .github/workflows/*.yml path → passes
YML_PATH_DESC="Fix .github/workflows/test.yml to run on push. TDD red-green confirms fix."
assert_allow "docs: .github/*.yml path passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$YML_PATH_DESC\""

# Test 44d: description citing only a dot-dir path with no recognized extension → passes
DOTDIR_PATH_DESC="Add .github/CODEOWNERS entry for the hooks directory. TDD red-green confirms fix."
assert_allow "docs: dot-dir extensionless path passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$DOTDIR_PATH_DESC\""

# Test 44e: a genuinely pathless description still trips the gate (regression guard)
assert_nudge "docs: pathless description still flagged after bin//.yml/dot-dir fix (default)" "" \
  "bd create --title=foo --description=\"Fix the thing properly. TDD red-green confirms fix.\"" \
  "file paths"

# ---------- Extensionless build-file + location-briefing/ tests (SABLE-w2x7) ----------

# Test 44f: description citing only Makefile (extensionless build file) → passes
MAKEFILE_DESC="Fix the build in Makefile to add a new target. TDD red-green confirms fix."
assert_allow "docs: Makefile-only path passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$MAKEFILE_DESC\""

# Test 44g: description citing only Dockerfile (extensionless build file) → passes
DOCKERFILE_DESC="Fix the build in Dockerfile to add a new stage. TDD red-green confirms fix."
assert_allow "docs: Dockerfile-only path passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$DOCKERFILE_DESC\""

# Test 44h: description citing only location-briefing/foo.md → passes
LOCATION_BRIEFING_DESC="Update location-briefing/foo.md with new context. TDD red-green confirms fix."
assert_allow "docs: location-briefing/ path passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$LOCATION_BRIEFING_DESC\""

# Test 44i: regression — pathless description still flagged after Makefile/Dockerfile/location-briefing fix
assert_nudge "docs: pathless description still flagged after extensionless fix (default)" "" \
  "bd create --title=foo --description=\"Fix the thing properly. TDD red-green confirms fix.\"" \
  "file paths"

# ---------- Justfile/Rakefile extensionless build-file tests (SABLE-i0db) ----------

# Test 44j: description citing only Justfile (extensionless build file) → passes
JUSTFILE_DESC="Fix the build in Justfile to add a new recipe. TDD red-green confirms fix."
assert_allow "docs: Justfile-only path passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$JUSTFILE_DESC\""

# Test 44k: description citing only Rakefile (extensionless build file) → passes
RAKEFILE_DESC="Fix the build in Rakefile to add a new task. TDD red-green confirms fix."
assert_allow "docs: Rakefile-only path passes file-path check (default)" "" \
  "bd create --title=foo --description=\"$RAKEFILE_DESC\""

# Test 44l: regression — pathless description still flagged after Justfile/Rakefile fix
assert_nudge "docs: pathless description still flagged after Justfile/Rakefile fix (default)" "" \
  "bd create --title=foo --description=\"Fix the thing properly. TDD red-green confirms fix.\"" \
  "file paths"

# ---------- Short-flag alias tests (-d / -f) (SABLE-iyv) ----------

GOOD_SHORT_DESC="hooks/bead-description-gate.sh line 98: extend HAS_FILE_FLAG regex. Test in hooks/test/test-bead-description-gate.sh — assert_nudge for -f."

# Test 45: bd create -d "<good desc>" → allow in manager mode
assert_allow "short-alias: -d good desc allowed (manager)" "$MANAGER_ENV" \
  "bd create --title=foo -d \"$GOOD_SHORT_DESC\""

# Test 46: bd create -d "<good desc>" → allow in default mode
assert_allow "short-alias: -d good desc allowed (default)" "" \
  "bd create --title=foo -d \"$GOOD_SHORT_DESC\""

# Test 47: bd create -d "<vague desc>" → deny in manager mode (same verdict as --description vague)
assert_deny "short-alias: -d vague desc denied (manager)" "$MANAGER_ENV" \
  "bd create --title=foo -d \"do the thing\"" \
  "missing"

# Test 48: bd create -d "<vague desc>" → nudge in default mode (same verdict as --description vague)
assert_nudge "short-alias: -d vague desc nudges (default)" "" \
  "bd create --title=foo -d \"do the thing\"" \
  "missing"

# Test 49: bd create -f /tmp/batch.md → nudge only, no deny (manager mode)
assert_nudge "short-alias: -f file nudge only (manager)" "$MANAGER_ENV" \
  "bd create --title=foo -f /tmp/batch.md" \
  "batch/stdin mode"

# Test 50: bd create -f /tmp/batch.md → nudge only, no deny (default mode)
assert_nudge "short-alias: -f file nudge only (default)" "" \
  "bd create --title=foo -f /tmp/batch.md" \
  "batch/stdin mode"

# Test 51: regression — bd create with neither -d nor --description nor any batch flag → still denied (manager)
assert_deny "short-alias regression: no desc still denied (manager)" "$MANAGER_ENV" \
  "bd create --title=foo --priority=1" \
  "no --description"

# Test 52: --description whose CONTENT contains " -d " must not confuse extraction
# The desc has a literal "-d " inside the value; extraction must pull the full content.
TRICKY_DESC="hooks/bead-description-gate.sh: check that -d inside a quoted value does not break flag parsing. Test in hooks/test/test-bead-description-gate.sh."
assert_allow "short-alias: -d inside desc content not confused (manager)" "$MANAGER_ENV" \
  "bd create --title=foo --description=\"$TRICKY_DESC\""

# ---------- Summary ----------

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed tests:$FAIL_NAMES"
  exit 1
fi
exit 0
