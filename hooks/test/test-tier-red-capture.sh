#!/usr/bin/env bash
# test-tier-red-capture.sh — the seam where SABLE-twpe2 (gate transport) and
# SABLE-muew7 (per-conjunct control reporting) meet.
#
# Neither bead alone fixes SABLE-1gnuj's class of defect: a control that
# names its failing conjunct is worthless if the gate tails the detail away
# in transit (twpe2), and fixing the transport is worthless if controls never
# emit per-conjunct state to begin with (muew7). This suite proves the PAIR:
# a real three-clause require_all control, with real padding output ahead of
# the failure (enough that the pre-twpe2 trailing-800-byte tail would have
# cut it), run through the REAL impact tier
# (bin/sable_gate_promote_lib.py::run_impact_tier, no mocks) — and asserts
# the report the gate returns names the SPECIFIC clause that broke.
#
# The known-positive control is the complement: the identical fixture,
# passing, must produce a report that names NOTHING about the control at all
# (require_all's own silence-on-green, preserved end to end) — proving the
# RED assertion below is a real distinguishing property, not a string that
# happens to always be present (SABLE-jd5fj.15's attributable-absence rule:
# every assertion here targets THIS run's own distinctive marker, never a
# global grep count).
#
# Run with:
#   bash hooks/test/test-tier-red-capture.sh

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
GATE_LIB="$REPO_ROOT/bin/sable_gate_promote_lib.py"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -f "$GATE_LIB" ] || { echo "FATAL: missing $GATE_LIB"; exit 2; }
[ -f "$TESTDIR/lib-require-all.sh" ] || { echo "FATAL: missing $TESTDIR/lib-require-all.sh"; exit 2; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

# This run's own distinctive strings — never reused across runs, never
# grepped for as a bare literal elsewhere, so a pass here is attributable to
# THIS fixture and not to leftover state or a coincidentally-matching string.
RUNID="tiercap-$$-$RANDOM"
CLAUSE_LABEL="clause-rc-zero-$RUNID"
MARKER="marker-$RUNID"
PADDING="$(python3 -c 'print("Q" * 1200)')"

# ---------------------------------------------------------------------------
# A scratch repo whose combined-tree impact tier selects exactly one real
# shell suite: a three-clause require_all control. Red/green is toggled by
# the FIXTURE_RED env var at RUN time (not baked into the fixture file), so
# one repo serves both the RED case and its known-positive GREEN control.
# ---------------------------------------------------------------------------
REPO="$TMPROOT/repo"
mkdir -p "$REPO/.github/ci" "$REPO/hooks/test"
git -C "$REPO" init -q -b trunk
git -C "$REPO" config user.email t@sable.invalid
git -C "$REPO" config user.name "SABLE Test"
cp "$TESTDIR/lib-require-all.sh" "$REPO/hooks/test/lib-require-all.sh"

cat > "$REPO/.github/ci/impact-manifest.sh" <<'EOF'
#!/bin/sh
echo test-multiclause-fixture.sh
EOF
chmod +x "$REPO/.github/ci/impact-manifest.sh"

cat > "$REPO/hooks/test/test-multiclause-fixture.sh" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib-require-all.sh
source "$HERE/lib-require-all.sh"
PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# Padding BEFORE the failure region — over 800 chars, so a trailing-800-byte
# tail (the pre-SABLE-twpe2 gate defect) would exclude everything below it.
echo "$FIXTURE_PADDING"

if [ "${FIXTURE_RED:-0}" = "1" ]; then c1=1; else c1=0; fi
[ 1 -eq 1 ]; c2=$?
[ 1 -eq 1 ]; c3=$?
require_all "multiclause fixture" \
  "$FIXTURE_CLAUSE_LABEL" "$c1" \
  "passing-clauseB" "$c2" \
  "passing-clauseC" "$c3"
if [ "$REQUIRE_ALL_OK" -eq 1 ]; then
  pass "multiclause control"
else
  fail "multiclause control ($FIXTURE_MARKER)" "$REQUIRE_ALL_DETAIL"
fi

echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: 0"
[ "$FAIL" -gt 0 ] && exit 1
exit 0
EOF
chmod +x "$REPO/hooks/test/test-multiclause-fixture.sh"

git -C "$REPO" add -A
git -C "$REPO" commit -q -m init
SHA="$(git -C "$REPO" rev-parse HEAD)"

RUNNER="$TMPROOT/run_tier.py"
cat > "$RUNNER" <<PYEOF
import sys
sys.path.insert(0, "$REPO_ROOT/bin")
import sable_gate_promote_lib as promote_lib
repo, sha = sys.argv[1], sys.argv[2]
outcome, detail = promote_lib.run_impact_tier(repo, sha, ["hooks/test/test-multiclause-fixture.sh"])
print("OUTCOME=" + outcome)
print("DETAIL=" + detail.replace("\n", " | "))
PYEOF

run_real_tier() {
  # $1 = "1" (red) or "0" (green); prints OUTCOME=... / DETAIL=... on stdout.
  FIXTURE_RED="$1" \
  FIXTURE_PADDING="$PADDING" \
  FIXTURE_CLAUSE_LABEL="$CLAUSE_LABEL" \
  FIXTURE_MARKER="$MARKER" \
  SABLE_MG_IMPACT_LOCK="$TMPROOT/impact-tier.lock" \
  SABLE_MG_IMPACT_WINDOW_LOG="$TMPROOT/windows.jsonl" \
    python3 "$RUNNER" "$REPO" "$SHA"
}

# ---------------------------------------------------------------------------
# RED: the real gate, the real suite, one real failing clause.
# ---------------------------------------------------------------------------
RED_OUT="$(run_real_tier 1)"
RED_OUTCOME="$(printf '%s\n' "$RED_OUT" | grep '^OUTCOME=' | cut -d= -f2-)"
RED_DETAIL="$(printf '%s\n' "$RED_OUT" | grep '^DETAIL=' | cut -d= -f2-)"

if [ "$RED_OUTCOME" = "red" ]; then
  pass "real impact tier reports RED on the real failing suite"
else
  fail "real impact tier reports RED on the real failing suite" "outcome=$RED_OUTCOME detail=$RED_DETAIL"
fi

if [ "${RED_DETAIL#*$CLAUSE_LABEL}" != "$RED_DETAIL" ]; then
  pass "propagated report NAMES the specific failing clause ($CLAUSE_LABEL)"
else
  fail "propagated report NAMES the specific failing clause ($CLAUSE_LABEL)" "detail=$RED_DETAIL"
fi

if [ "${RED_DETAIL#*$MARKER}" != "$RED_DETAIL" ]; then
  pass "propagated report survives to the runner's output (marker $MARKER present)"
else
  fail "propagated report survives to the runner's output (marker $MARKER present)" "detail=$RED_DETAIL"
fi

if [ "${RED_DETAIL#*passing-clauseB}" = "$RED_DETAIL" ] && [ "${RED_DETAIL#*passing-clauseC}" = "$RED_DETAIL" ]; then
  pass "the two PASSING clauses are not named alongside the failing one"
else
  fail "the two PASSING clauses are not named alongside the failing one" "detail=$RED_DETAIL"
fi

# ---------------------------------------------------------------------------
# KNOWN-POSITIVE CONTROL: same suite, same padding, GREEN. The propagated
# report must contain NEITHER this run's marker NOR its clause label — proof
# the RED assertions above test a real distinguishing property, not a string
# that is always present regardless of outcome (SABLE-jd5fj.15).
# ---------------------------------------------------------------------------
GREEN_OUT="$(run_real_tier 0)"
GREEN_OUTCOME="$(printf '%s\n' "$GREEN_OUT" | grep '^OUTCOME=' | cut -d= -f2-)"
GREEN_DETAIL="$(printf '%s\n' "$GREEN_OUT" | grep '^DETAIL=' | cut -d= -f2-)"

if [ "$GREEN_OUTCOME" = "green" ]; then
  pass "known-positive control: the same fixture passing reports GREEN"
else
  fail "known-positive control: the same fixture passing reports GREEN" "outcome=$GREEN_OUTCOME detail=$GREEN_DETAIL"
fi

if [ "${GREEN_DETAIL#*$MARKER}" = "$GREEN_DETAIL" ] && [ "${GREEN_DETAIL#*$CLAUSE_LABEL}" = "$GREEN_DETAIL" ]; then
  pass "known-positive control: GREEN report contains neither this run's marker nor its clause label"
else
  fail "known-positive control: GREEN report contains neither this run's marker nor its clause label" "detail=$GREEN_DETAIL"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: 0"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
