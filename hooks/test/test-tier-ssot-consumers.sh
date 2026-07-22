#!/usr/bin/env bash
# test-tier-ssot-consumers.sh — integration tests for the CI-tier SSOT
# (SABLE-cmar4.1): all three named consumers resolve tier membership /
# budgets from the ONE file, and the pre-existing shell-run-set.sh IRON RULE
# guards still fire under the generalization that let it be sourced as a
# library. Real bash processes throughout — no mocks, no bd/dolt.
#
# Fixture: a throwaway git repo (bare origin + working clone) carrying REAL,
# unmodified copies of .github/ci/shell-run-set.sh and .github/ci/
# test-tiers.sh (the actual generalized production files — this is the
# "under the generalization" regression check), plus a minimal hooks/test/
# with two trivial suites the pre_push tier is pointed at. The SSOT is then
# mutated IN PLACE in the fixture and each consumer is re-checked to prove it
# re-resolves from the same file rather than caching a stale copy.
#
# Consumers exercised:
#   1. hooks/multi-manager/pre-push-rebase-test.sh — runs the REAL hook
#      end-to-end against the fixture repo; its phase-3 TEST_CMD falls back to
#      `test-tiers.sh --run pre_push` when nothing else configures a test
#      command (SABLE-cmar4.1 consumption seam), so mutating pre_push's suite
#      membership changes what the hook actually executes.
#   2. bin/sable-merge-gate — production default_mg_timeout() is called
#      directly (importlib, no CLI) against the fixture repo path; mutating
#      the merge_preview budget in the fixture's SSOT changes its return
#      value.
#   3. The future SABLE-jd5fj.5 snapshot runner does not exist yet, so the
#      loader's own full_snapshot resolution stands in for it here: it must
#      alias shell-run-set.sh's ALLOW by reference, so mutating ALLOW (the
#      same file consumer #1's tier data and consumer #3's data both trace
#      back to) changes full_snapshot's membership too — the "no duplicated
#      test lists anywhere" contract any future consumer inherits for free.
#
# Run with:
#   bash hooks/test/test-tier-ssot-consumers.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/pre-push-rebase-test.sh"
LIB_IDENTITY="$REPO/hooks/multi-manager/lib-identity.sh"
SMG="$REPO/bin/sable-merge-gate"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-tier-ssot-consumers.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

REPO_DIR="$TMPROOT/fixture-repo"
BARE_DIR="$TMPROOT/fixture-bare.git"

git init -q --bare "$BARE_DIR"
git clone -q "$BARE_DIR" "$REPO_DIR"

mkdir -p "$REPO_DIR/.github/ci" "$REPO_DIR/hooks/test" "$REPO_DIR/hooks/multi-manager"
cp "$REPO/.github/ci/shell-run-set.sh" "$REPO_DIR/.github/ci/shell-run-set.sh"
cp "$REPO/.github/ci/test-tiers.sh"    "$REPO_DIR/.github/ci/test-tiers.sh"
cp "$LIB_IDENTITY" "$REPO_DIR/hooks/multi-manager/lib-identity.sh"
cp "$HOOK"          "$REPO_DIR/hooks/multi-manager/pre-push-rebase-test.sh"

cat > "$REPO_DIR/hooks/test/test-fixture-alpha.sh" <<'EOF'
#!/usr/bin/env bash
echo "ALPHA-RAN"
exit 0
EOF
cat > "$REPO_DIR/hooks/test/test-fixture-beta.sh" <<'EOF'
#!/usr/bin/env bash
echo "BETA-RAN"
exit 0
EOF
cat > "$REPO_DIR/hooks/test/test-fixture-gamma.sh" <<'EOF'
#!/usr/bin/env bash
echo "GAMMA-RAN-AND-FAILED"
exit 1
EOF
chmod +x "$REPO_DIR"/hooks/test/test-fixture-*.sh

# set_pre_push <suite...> / set_merge_preview_budget <seconds> / set_allow
# <extra-suite>: mutate the fixture's copied SSOT files IN PLACE via a
# targeted regex substitution (never append-at-EOF — the loader's CLI
# dispatch runs top-to-bottom DURING sourcing/execution, before anything
# appended after it, so an appended override is invisible to --run/--budget
# and its own trailing exit status corrupts the script's real exit code).
set_pre_push() {
  python3 -c "
import re, sys
p = '$REPO_DIR/.github/ci/test-tiers.sh'
s = open(p).read()
suites = ' '.join(sys.argv[1:])
s, n = re.subn(r'SABLE_TIER_PRE_PUSH=\([^)]*\)', f'SABLE_TIER_PRE_PUSH=({suites})', s, count=1, flags=re.DOTALL)
assert n == 1, 'SABLE_TIER_PRE_PUSH array not found'
open(p, 'w').write(s)
" "$@"
}

set_merge_preview_budget() {
  python3 -c "
import re
p = '$REPO_DIR/.github/ci/test-tiers.sh'
s = open(p).read()
s, n = re.subn(r'\[merge_preview\]=\d+', '[merge_preview]=$1', s, count=1)
assert n == 1, 'merge_preview budget entry not found'
open(p, 'w').write(s)
"
}

add_allow_suite() {
  python3 -c "
import re
p = '$REPO_DIR/.github/ci/shell-run-set.sh'
s = open(p).read()
s, n = re.subn(r'ALLOW=\(\n', 'ALLOW=(\n  $1\n', s, count=1)
assert n == 1, 'ALLOW array open not found'
open(p, 'w').write(s)
"
}

set_pre_push test-fixture-alpha.sh test-fixture-beta.sh

cd "$REPO_DIR" || { echo "FATAL: cd to fixture repo failed"; exit 2; }
git config user.email "test@test"
git config user.name "Test"
git add -A
git commit -q -m "init fixture"
git push -q "$BARE_DIR" HEAD:refs/heads/main 2>/dev/null
cd - >/dev/null

MGR_ENV="CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager"

run_hook() {
  # $1 = env prefix, $2 = cwd
  local input
  input=$(python3 -c "
import json
print(json.dumps({'tool_input': {'command': 'git push'}, 'cwd': '$2'}))
")
  env -i PATH="$PATH" $1 bash "$HOOK" <<< "$input" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Consumer 1: pre-push-rebase-test.sh resolves pre_push from the fixture SSOT
# ---------------------------------------------------------------------------

PP_ENV="$MGR_ENV SABLE_BASE_BRANCH=origin/main SABLE_PRE_PUSH_TYPECHECK_COMMAND=true"
OUT1=$(run_hook "$PP_ENV" "$REPO_DIR")

if [ -z "$OUT1" ]; then
  pass "consumer 1 (pre-push hook): no explicit testCommand + tier SSOT present -> resolves pre_push, both fixture suites pass -> push ALLOWED"
else
  fail "consumer 1 (pre-push hook): no explicit testCommand + tier SSOT present -> resolves pre_push, both fixture suites pass -> push ALLOWED" "got: ${OUT1:0:400}"
fi

# Mutate the SSOT (step 2): drop the passing beta suite, add a failing one.
set_pre_push test-fixture-alpha.sh test-fixture-gamma.sh

OUT2=$(run_hook "$PP_ENV" "$REPO_DIR")

# Assert on the DENY behavior change (ALLOW -> DENY, same repo, only the SSOT
# changed), that phase 4 (not 1/2/3 — SABLE-rzsb S4 inserted a new BUILD
# phase 3 ahead of TESTS, renumbering it) is what fired, AND on the deny
# message's captured-output text: SABLE-29o5y (filed above, fixed under
# SABLE-rzsb.2) was a truncation bug where `${TEST_OUT: -1500}` yielded
# EMPTY — not the whole string — whenever the failing command's output was
# under 1500 chars, exactly this tiny fixture suite's case. Now fixed
# (sable_tail_chars, a length-safe `tail -c`), so the fixture's own failure
# marker is expected to survive into the deny message.
if printf '%s' "$OUT2" | grep -q '"permissionDecision": "deny"' \
   && printf '%s' "$OUT2" | grep -q 'phase 4 (tests)' \
   && printf '%s' "$OUT2" | grep -q 'GAMMA-RAN-AND-FAILED'; then
  pass "consumer 1 (pre-push hook): after mutating pre_push in the SAME file (drop passing beta, add failing gamma), the hook re-resolves it and phase 4 now DENIES with the fixture's own output in the message"
else
  fail "consumer 1 (pre-push hook): after mutating pre_push in the SAME file (drop passing beta, add failing gamma), the hook re-resolves it and phase 4 now DENIES with the fixture's own output in the message" "got: ${OUT2:0:600}"
fi

# Confirm directly (bypassing the hook's deny-message truncation entirely)
# that the mutated file is what the hook just ran: gamma (new) is in, beta
# (dropped) is out.
DIRECT_LIST=$(bash "$REPO_DIR/.github/ci/test-tiers.sh" --list pre_push 2>&1)
if printf '%s' "$DIRECT_LIST" | grep -qx "test-fixture-gamma.sh" \
   && ! printf '%s' "$DIRECT_LIST" | grep -qx "test-fixture-beta.sh"; then
  pass "consumer 1 (pre-push hook): the mutated pre_push tier the hook just consumed is confirmed to be gamma-in/beta-out"
else
  fail "consumer 1 (pre-push hook): the mutated pre_push tier the hook just consumed is confirmed to be gamma-in/beta-out" "got: $DIRECT_LIST"
fi

# ---------------------------------------------------------------------------
# Consumer 2: bin/sable-merge-gate's default_mg_timeout resolves merge_preview
# budget from the fixture SSOT
# ---------------------------------------------------------------------------

read_mg_timeout() {
  python3 -c "
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
loader = SourceFileLoader('sable_merge_gate', '$SMG')
spec = importlib.util.spec_from_loader('sable_merge_gate', loader)
smg = importlib.util.module_from_spec(spec)
loader.exec_module(smg)
print(smg.default_mg_timeout('$REPO_DIR'))
"
}

BEFORE_BUDGET=$(read_mg_timeout)
if [ "$BEFORE_BUDGET" = "900.0" ]; then
  pass "consumer 2 (sable-merge-gate): default_mg_timeout reads merge_preview budget (900) from the fixture SSOT"
else
  fail "consumer 2 (sable-merge-gate): default_mg_timeout reads merge_preview budget (900) from the fixture SSOT" "got: $BEFORE_BUDGET"
fi

# Mutate the SSOT (same file consumer 1 reads) in place.
set_merge_preview_budget 555

AFTER_BUDGET=$(read_mg_timeout)
if [ "$AFTER_BUDGET" = "555.0" ]; then
  pass "consumer 2 (sable-merge-gate): after mutating the SAME file, default_mg_timeout re-resolves to the new budget (555)"
else
  fail "consumer 2 (sable-merge-gate): after mutating the SAME file, default_mg_timeout re-resolves to the new budget (555)" "got: $AFTER_BUDGET"
fi

# ---------------------------------------------------------------------------
# Consumer 2b (SABLE-jd5fj.9): sable_gate_promote_lib's impact-tier timeout
# (_impact_timeout, consumed by run_impact_tier for the cmar4 optimistic-
# promotion combined-tree check) reads the SAME merge_preview budget from
# the SAME SSOT — before jd5fj.9 it was a hand-copied 900 literal, the exact
# duplicated-list class SABLE-cmar4.1 closed for tier membership,
# reintroduced one level down. Reuses the fixture's SSOT already mutated to
# 555 by consumer 2 above, then mutates it AGAIN to a third, distinctive
# value to prove this is live re-resolution and not a value cached at the
# 555 mutation.
# ---------------------------------------------------------------------------

read_impact_timeout() {
  python3 -c "
import importlib.util
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader('sable_merge_gate', '$SMG')
spec = importlib.util.spec_from_loader('sable_merge_gate', loader)
smg = importlib.util.module_from_spec(spec)
loader.exec_module(smg)
print(smg.promote_lib._impact_timeout('$REPO_DIR'))
"
}

unset SABLE_MG_IMPACT_TIMEOUT
IMPACT_AT_555=$(read_impact_timeout)
if [ "$IMPACT_AT_555" = "555.0" ]; then
  pass "consumer 2b (impact-tier timeout): _impact_timeout reads the SAME merge_preview budget (555) as consumer 2, not a second hardcoded copy"
else
  fail "consumer 2b (impact-tier timeout): _impact_timeout reads the SAME merge_preview budget (555) as consumer 2, not a second hardcoded copy" "got: $IMPACT_AT_555"
fi

set_merge_preview_budget 777

IMPACT_AT_777=$(read_impact_timeout)
if [ "$IMPACT_AT_777" = "777.0" ]; then
  pass "consumer 2b (impact-tier timeout): after mutating the SAME file AGAIN, _impact_timeout re-resolves to the new budget (777) -- a future hardcode would fail this"
else
  fail "consumer 2b (impact-tier timeout): after mutating the SAME file AGAIN, _impact_timeout re-resolves to the new budget (777) -- a future hardcode would fail this" "got: $IMPACT_AT_777"
fi

# ---------------------------------------------------------------------------
# Consumer 3 (stand-in for the not-yet-built SABLE-jd5fj.5 snapshot runner):
# full_snapshot aliases shell-run-set.sh's ALLOW BY REFERENCE, not a copy —
# mutating ALLOW (the file consumer 1 and 2's data both ultimately trace to
# for merge_preview) must move full_snapshot too, with zero edits to
# test-tiers.sh itself.
# ---------------------------------------------------------------------------

BEFORE_SNAPSHOT_COUNT=$(bash "$REPO_DIR/.github/ci/test-tiers.sh" --list full_snapshot 2>&1 | grep -c . || true)

add_allow_suite test-fixture-alpha.sh

AFTER_SNAPSHOT_COUNT=$(bash "$REPO_DIR/.github/ci/test-tiers.sh" --list full_snapshot 2>&1 | grep -c . || true)

if [ "$AFTER_SNAPSHOT_COUNT" -eq $((BEFORE_SNAPSHOT_COUNT + 1)) ]; then
  pass "consumer 3 (future snapshot-runner stand-in): full_snapshot membership tracks ALLOW by reference — mutating shell-run-set.sh alone moved it ($BEFORE_SNAPSHOT_COUNT -> $AFTER_SNAPSHOT_COUNT), no test-tiers.sh edit needed"
else
  fail "consumer 3 (future snapshot-runner stand-in): full_snapshot membership tracks ALLOW by reference" "before=$BEFORE_SNAPSHOT_COUNT after=$AFTER_SNAPSHOT_COUNT"
fi

# ---------------------------------------------------------------------------
# IRON RULE regression: the pre-existing shell-run-set.sh guards still fire
# under the generalization (sourced-vs-executed guard added for SABLE-
# cmar4.1). Exercised on the fixture's REAL, unmodified-logic copy — the two
# fixture suites were never added to ALLOW/EXCLUDE (UNCLASSIFIED case) and the
# real ALLOW/EXCLUDE entries name files that don't exist in this minimal
# fixture hooks/test/ (stale-drift case) — both fire with no extra setup,
# proving --manifest's checks are untouched by the sourcing guard. (alpha was
# added to ALLOW by the consumer-3 step above, so beta — never added anywhere
# — is the one still expected UNCLASSIFIED here.)
# ---------------------------------------------------------------------------

MANIFEST_OUT=$(bash "$REPO_DIR/.github/ci/shell-run-set.sh" --manifest 2>&1)

if printf '%s' "$MANIFEST_OUT" | grep -q '::warning::UNCLASSIFIED test-fixture-beta.sh'; then
  pass "IRON RULE: SABLE-7v3z UNCLASSIFIED silent-green trap still fires under the generalization"
else
  fail "IRON RULE: SABLE-7v3z UNCLASSIFIED silent-green trap still fires under the generalization" "$MANIFEST_OUT"
fi

if printf '%s' "$MANIFEST_OUT" | grep -q '::warning::stale run-set entry:'; then
  pass "IRON RULE: EXCLUDE/stale-drift guard still fires under the generalization"
else
  fail "IRON RULE: EXCLUDE/stale-drift guard still fires under the generalization" "$MANIFEST_OUT"
fi

# Direct execution of the fixture's shell-run-set.sh (not sourced) must still
# work standalone — the sourced-vs-executed guard must not have broken the
# ci-verify.yml invocation path. Captured via command substitution (not a
# `| grep` pipeline): shell-run-set.sh deliberately `exit 2`s on bad usage, and
# under this test script's own `set -o pipefail` a `cmd | grep -q` pipeline
# reports THAT nonzero producer exit rather than grep's match, independent of
# whether grep actually matched.
USAGE_OUT=$(bash "$REPO_DIR/.github/ci/shell-run-set.sh" 2>&1); USAGE_RC=$?
if [ "$USAGE_RC" -eq 2 ] && printf '%s' "$USAGE_OUT" | grep -q '^usage:'; then
  pass "shell-run-set.sh executed directly with no args still prints usage (CLI dispatch unaffected by the sourcing guard)"
else
  fail "shell-run-set.sh executed directly with no args still prints usage (CLI dispatch unaffected by the sourcing guard)" "rc=$USAGE_RC out=$USAGE_OUT"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
