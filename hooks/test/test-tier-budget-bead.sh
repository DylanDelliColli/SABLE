#!/usr/bin/env bash
# test-tier-budget-bead.sh — real-bd integration tests for the per-tier
# duration budget breach path (SABLE-cmar4.4, columbo cmar4 S2 integration
# case).
#
# Real bd throughout (a sandboxed `bd init --non-interactive` under a
# throwaway HOME — real embedded-Dolt database, real `bd create --sandbox` /
# `bd list --json`, no stubbing), and a real .github/ci/test-tiers.sh copied
# from the actual production file and mutated IN PLACE with the same
# targeted-regex technique hooks/test/test-tier-ssot-consumers.sh already
# uses, so this exercises the REAL subprocess read
# (sable_gate_budget_lib.tier_budget_sec shells out to `bash test-tiers.sh
# --budget <tier>`) alongside the real bd filing pipeline.
#
# The DURATION handed to check_and_file() is supplied directly by each case
# rather than produced by a real CI wait — that is the deliberate input
# boundary (the same way other gate integration suites inject a fake `gh`
# verdict rather than waiting on real Actions): what this suite is actually
# proving is the real bd idempotency pipeline and the real SSOT read, not
# wall-clock arithmetic (that is stdlib time.monotonic() subtraction, unit-
# tested trivially and not worth flaking a real-process suite over).
#
# Cases (columbo cmar4 S2 integration spec):
#   C1 breach files exactly one bead.
#   C2 a second breach against the SAME (tier, budget) key files none —
#      idempotent even across what models two separate promote() calls.
#   C3 a budget-version bump (editing the SSOT's number) is a NEW key and
#      files a NEW bead — the budget value doubling as its own version.
#   C4 under-budget (after bumping the budget above the duration) is not a
#      breach at all: no WARN, no new bead.
#
# Run with:
#   bash hooks/test/test-tier-budget-bead.sh
#
# Self-skips (SKIP, not FAIL) when bd is absent, matching the bd/dolt-suites-
# self-skip contract other real-bd suites in this fleet already carry
# (hooks/test/test-snapshot-freeze.sh's C9, bin/test_sable_reconcile_handoffs_
# integration.py).

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin"

PASS=0; FAIL=0; SKIP=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
skip(){ SKIP=$((SKIP+1)); echo "SKIP: $1"; }

if ! command -v bd >/dev/null 2>&1; then
  skip "entire suite — no bd on PATH (SABLE-59zu clean-room has none by design)"
  echo
  echo "test-tier-budget-bead.sh: $PASS passed, $FAIL failed, $SKIP skipped"
  exit 0
fi

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-tier-budget-bead.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

REPO_DIR="$TMPROOT/fixture-repo"
BD_HOME="$TMPROOT/bd-home"
mkdir -p "$REPO_DIR/.github/ci" "$BD_HOME"
cp "$REPO/.github/ci/shell-run-set.sh" "$REPO_DIR/.github/ci/shell-run-set.sh"
cp "$REPO/.github/ci/test-tiers.sh"    "$REPO_DIR/.github/ci/test-tiers.sh"

# --- mutate the fixture's copied SSOT budget IN PLACE, same targeted regex
# technique as test-tier-ssot-consumers.sh (never append-at-EOF: the loader's
# CLI dispatch runs during sourcing, before anything appended after it).
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

# `bd init` on the embedded-Dolt backend can leave a PARTIAL database on a
# first-run repo_state.json race (rc 0 but no config.yaml) — gate on the
# artifact and retry, same as bin/test_sable_reconcile_handoffs_integration.py
# and test-snapshot-freeze.sh's C9.
BD_INIT_OK=0
for _ in 1 2 3 4; do
  rm -rf "$REPO_DIR/.beads"
  ( cd "$REPO_DIR" && env HOME="$BD_HOME" BD_NON_INTERACTIVE=1 CI=true bd init --non-interactive >/dev/null 2>&1 )
  if [ -f "$REPO_DIR/.beads/config.yaml" ]; then BD_INIT_OK=1; break; fi
done

if [ "$BD_INIT_OK" -ne 1 ]; then
  skip "entire suite — bd init never produced a clean sandbox DB"
  echo
  echo "test-tier-budget-bead.sh: $PASS passed, $FAIL failed, $SKIP skipped"
  exit 0
fi

bd_in_sandbox() {
  ( cd "$REPO_DIR" && env HOME="$BD_HOME" BD_NON_INTERACTIVE=1 CI=true bd "$@" )
}

bd_count_suite_optimization() {
  bd_in_sandbox list --status=open --label=suite-optimization --json 2>/dev/null \
    | python3 -c 'import json,sys
try: print(len(json.load(sys.stdin)))
except Exception: print(0)'
}

bd_ids_suite_optimization() {
  bd_in_sandbox list --status=open --label=suite-optimization --json 2>/dev/null \
    | python3 -c 'import json,sys
try:
    for b in json.load(sys.stdin): print(b.get("id",""))
except Exception: pass'
}

# check_and_file <duration_sec> — runs the REAL module against $REPO_DIR with
# a real (sandboxed) bd, real test-tiers.sh subprocess read. Prints
# "<checked>|<breached>|<filed>|<key>" on stdout; WARN-or-not observable via
# the captured stderr file.
run_check() {
  local duration="$1" out_file="$2" err_file="$3"
  ( cd "$REPO_DIR" && env HOME="$BD_HOME" BD_NON_INTERACTIVE=1 CI=true \
      PYTHONPATH="$BIN" python3 -c "
import sable_gate_budget_lib as b
r = b.check_and_file('$REPO_DIR', 'merge_preview', float('$duration'),
                     context='integration-fixture')
print(f\"{r.get('checked')}|{r.get('breached')}|{r.get('filed')}|{r.get('key')}\")
" ) >"$out_file" 2>"$err_file"
}

# ---------------------------------------------------------------------------
# C1 — breach files exactly one bead
# ---------------------------------------------------------------------------
set_merge_preview_budget 500
DUR=1000

OUT1="$TMPROOT/out1"; ERR1="$TMPROOT/err1"
run_check "$DUR" "$OUT1" "$ERR1"
RESULT1="$(cat "$OUT1")"
COUNT1="$(bd_count_suite_optimization)"

if [ "$RESULT1" = "True|True|True|merge_preview:500" ]; then
  pass "C1 check_and_file reports breached+filed against the real SSOT budget"
else
  fail "C1 check_and_file reports breached+filed" "got: $RESULT1"
fi
if [ "$COUNT1" = "1" ]; then
  pass "C1 breach files exactly one suite-optimization bead"
else
  fail "C1 exactly one bead" "count=$COUNT1"
fi
if grep -q "WARN" "$ERR1" && grep -q "merge_preview" "$ERR1"; then
  pass "C1 breach WARNs on stderr, naming the tier"
else
  fail "C1 breach WARNs on stderr" "$(cat "$ERR1")"
fi

FIRST_ID="$(bd_ids_suite_optimization)"

# ---------------------------------------------------------------------------
# C2 — a second breach against the SAME (tier, budget) key files none
# ---------------------------------------------------------------------------
OUT2="$TMPROOT/out2"; ERR2="$TMPROOT/err2"
run_check "$DUR" "$OUT2" "$ERR2"
RESULT2="$(cat "$OUT2")"
COUNT2="$(bd_count_suite_optimization)"
SECOND_ID="$(bd_ids_suite_optimization)"

if [ "$RESULT2" = "True|True|False|merge_preview:500" ]; then
  pass "C2 second breach against the same key reports filed=False (found existing)"
else
  fail "C2 second breach reports filed=False" "got: $RESULT2"
fi
if [ "$COUNT2" = "1" ] && [ "$SECOND_ID" = "$FIRST_ID" ]; then
  pass "C2 idempotent by key — still exactly one bead, same identity"
else
  fail "C2 idempotent by key" "count=$COUNT2 first=$FIRST_ID second=$SECOND_ID"
fi

# ---------------------------------------------------------------------------
# C3 — a budget-version bump (editing the SSOT's number) is a NEW key and
# files a NEW bead, even though the same worktree/duration breaches again.
# ---------------------------------------------------------------------------
set_merge_preview_budget 700   # still < DUR=1000, so still a breach — but a
                               # DIFFERENT key than merge_preview:500.
OUT3="$TMPROOT/out3"; ERR3="$TMPROOT/err3"
run_check "$DUR" "$OUT3" "$ERR3"
RESULT3="$(cat "$OUT3")"
COUNT3="$(bd_count_suite_optimization)"

if [ "$RESULT3" = "True|True|True|merge_preview:700" ]; then
  pass "C3 a bumped SSOT budget produces a new key and files"
else
  fail "C3 bumped budget files under a new key" "got: $RESULT3"
fi
if [ "$COUNT3" = "2" ]; then
  pass "C3 budget-version bump files a NEW bead (now two open, not a dup of C1's)"
else
  fail "C3 budget-version bump files a new bead" "count=$COUNT3"
fi

# ---------------------------------------------------------------------------
# C4 — under-budget (bump above the duration): not a breach at all, no WARN,
# no new bead.
# ---------------------------------------------------------------------------
set_merge_preview_budget 2000  # now > DUR=1000
OUT4="$TMPROOT/out4"; ERR4="$TMPROOT/err4"
run_check "$DUR" "$OUT4" "$ERR4"
RESULT4="$(cat "$OUT4")"
COUNT4="$(bd_count_suite_optimization)"

if [ "$RESULT4" = "True|False|None|None" ]; then
  pass "C4 under-budget negative case: not breached, nothing filed"
else
  fail "C4 under-budget reports not breached" "got: $RESULT4"
fi
if [ "$COUNT4" = "2" ]; then
  pass "C4 under-budget leaves the open bead count unchanged"
else
  fail "C4 under-budget does not file" "count=$COUNT4"
fi
if ! grep -q "WARN" "$ERR4"; then
  pass "C4 under-budget prints no WARN"
else
  fail "C4 under-budget prints no WARN" "$(cat "$ERR4")"
fi

echo
echo "test-tier-budget-bead.sh: $PASS passed, $FAIL failed, $SKIP skipped"
if [ "$FAIL" -ne 0 ]; then
  echo -e "Failures:$FAIL_NAMES"
  exit 1
fi
exit 0
