#!/usr/bin/env bash
# test-shell-run-set-strict.sh — unit tests for shell-run-set.sh --check
# (SABLE-lcevs): the fail-CLOSED counterpart to --manifest.
#
# THE DEFECT THIS GUARDS: --manifest detects UNCLASSIFIED suites and stale
# run-set entries but always exits 0 — a ::warning:: annotation does not fail
# a GitHub Actions job, so a branch could add test suites, have them land
# UNCLASSIFIED, and merge fully green with the new suites never executing in
# CI (SABLE-7v3z's silent-green trap, escalated to this mechanism bead by
# chuck at the merge seat after jd5fj.6 nearly shipped exactly that). --check
# must exit non-zero whenever manifest_scan finds either condition.
#
# Fixture: a throwaway directory tree carrying a REAL, unmodified copy of
# .github/ci/shell-run-set.sh (the actual production script — this is not a
# reimplementation) with its own hooks/test/ directory and its ALLOW/EXCLUDE
# arrays replaced wholesale per case via a targeted regex substitution (same
# technique hooks/test/test-tier-ssot-consumers.sh already uses for its own
# fixture SSOT mutations). No git needed: shell-run-set.sh resolves REPO from
# its own script location (two dirs up from .github/ci/), so placing the copy
# at <fixture>/.github/ci/shell-run-set.sh alone makes TESTDIR resolve to
# <fixture>/hooks/test.
#
# Run with:
#   bash hooks/test/test-shell-run-set-strict.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PROD="$REPO/.github/ci/shell-run-set.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-shell-run-set-strict.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

# new_fixture <name>: sets up <TMPROOT>/<name>/.github/ci/shell-run-set.sh
# (a real copy of production) + an empty hooks/test/ dir, echoes the fixture
# root path.
new_fixture() {
  local dir="$TMPROOT/$1"
  mkdir -p "$dir/.github/ci" "$dir/hooks/test"
  cp "$PROD" "$dir/.github/ci/shell-run-set.sh"
  echo "$dir"
}

# set_allow_exclude <fixture-root> [allow-suite ...] --SEP-- [name=reason ...]
# Wholesale-replaces the ALLOW=(...) and declare -A EXCLUDE=(...) array
# literals in the fixture's copy. Each argument is one shell word (a reason
# may safely contain spaces/parens as a single argv entry — no string-split
# ambiguity). Non-greedy match up to the first "\n)\n" — safe here because
# EXCLUDE's reason strings never contain a line that is just ")" (their
# trailing ")" is always followed by a closing quote on the same line, e.g.
# `(SABLE-59zu)"`).
set_allow_exclude() {
  local fixture="$1"; shift
  python3 -c "
import re, sys
p = sys.argv[1] + '/.github/ci/shell-run-set.sh'
args = sys.argv[2:]
sep = args.index('--SEP--')
allow_items = args[:sep]
exclude_items = args[sep+1:]
s = open(p).read()

allow_body = '\n'.join(f'  {a}' for a in allow_items)
s, n = re.subn(r'ALLOW=\(.*?\n\)\n', f'ALLOW=(\n{allow_body}\n)\n', s, count=1, flags=re.DOTALL)
assert n == 1, 'ALLOW block not found'

excl_body = '\n'.join(
    '  [{}]=\"{}\"'.format(*kv.split('=', 1)) for kv in exclude_items
)
s, n = re.subn(r'declare -A EXCLUDE=\(.*?\n\)\n', f'declare -A EXCLUDE=(\n{excl_body}\n)\n', s, count=1, flags=re.DOTALL)
assert n == 1, 'EXCLUDE block not found'

open(p, 'w').write(s)
" "$fixture" "$@"
}

touch_suite() { mkdir -p "$(dirname "$1")"; printf '#!/usr/bin/env bash\nexit 0\n' > "$1"; }

# ---------------------------------------------------------------------------
# Case (a): a suite present on disk, absent from both ALLOW and EXCLUDE
# (UNCLASSIFIED) => --check exits NON-ZERO and names the suite.
# ---------------------------------------------------------------------------
FIX_A="$(new_fixture case-a)"
touch_suite "$FIX_A/hooks/test/test-fixture-unclassified.sh"
set_allow_exclude "$FIX_A" --SEP--

OUT_A=$(bash "$FIX_A/.github/ci/shell-run-set.sh" --check 2>&1); RC_A=$?

if [ "$RC_A" -ne 0 ] && printf '%s' "$OUT_A" | grep -q 'test-fixture-unclassified.sh'; then
  pass "(a) UNCLASSIFIED suite on disk, absent from ALLOW/EXCLUDE -> --check exits non-zero and names it"
else
  fail "(a) UNCLASSIFIED suite on disk, absent from ALLOW/EXCLUDE -> --check exits non-zero and names it" "rc=$RC_A out=$OUT_A"
fi

# This case is RED against today's (pre-lcevs) script by construction: the
# same fixture run through --manifest alone always exits 0 no matter what it
# finds — that gap is the regression this bead closes.
MANIFEST_RC_A=0
bash "$FIX_A/.github/ci/shell-run-set.sh" --manifest >/dev/null 2>&1 || MANIFEST_RC_A=$?
if [ "$MANIFEST_RC_A" -eq 0 ]; then
  pass "(a) regression proof: --manifest alone still exits 0 on the same UNCLASSIFIED fixture (the fail-open behavior --check exists to not have)"
else
  fail "(a) regression proof: --manifest alone still exits 0 on the same UNCLASSIFIED fixture" "rc=$MANIFEST_RC_A"
fi

# ---------------------------------------------------------------------------
# Case (b): a run-set entry listed but the file is absent (stale) => --check
# exits NON-ZERO.
# ---------------------------------------------------------------------------
FIX_B="$(new_fixture case-b)"
# hooks/test/ is left empty on disk; ALLOW names a suite that doesn't exist.
set_allow_exclude "$FIX_B" test-fixture-ghost.sh --SEP--

OUT_B=$(bash "$FIX_B/.github/ci/shell-run-set.sh" --check 2>&1); RC_B=$?

if [ "$RC_B" -ne 0 ] && printf '%s' "$OUT_B" | grep -q 'stale run-set entry: test-fixture-ghost.sh'; then
  pass "(b) stale run-set entry (listed, file absent) -> --check exits non-zero and names it"
else
  fail "(b) stale run-set entry (listed, file absent) -> --check exits non-zero and names it" "rc=$RC_B out=$OUT_B"
fi

# ---------------------------------------------------------------------------
# Case (c): every suite present on disk is in ALLOW or EXCLUDE-with-reason,
# and no stale entries => --check exits 0.
# ---------------------------------------------------------------------------
FIX_C="$(new_fixture case-c)"
touch_suite "$FIX_C/hooks/test/test-fixture-allowed.sh"
touch_suite "$FIX_C/hooks/test/test-fixture-excluded.sh"
set_allow_exclude "$FIX_C" test-fixture-allowed.sh --SEP-- "test-fixture-excluded.sh=known-red, tracked (SABLE-fixture)"

OUT_C=$(bash "$FIX_C/.github/ci/shell-run-set.sh" --check 2>&1); RC_C=$?

if [ "$RC_C" -eq 0 ]; then
  pass "(c) every suite classified (ALLOW or EXCLUDE-with-reason), no stale entries -> --check exits 0"
else
  fail "(c) every suite classified (ALLOW or EXCLUDE-with-reason), no stale entries -> --check exits 0" "rc=$RC_C out=$OUT_C"
fi

# ---------------------------------------------------------------------------
# Case (d) check_error_message_preempts_retry: the --check failure message
# must state BOTH the remedy (ALLOW/EXCLUDE) AND that re-running will not
# clear it (SABLE-capzx) — a fail-closed gate can be defeated socially by
# retries if the message only says what to do and never what not to do.
# Re-uses FIX_A (one UNCLASSIFIED suite, non-zero exit) from case (a) above.
# ---------------------------------------------------------------------------
if printf '%s' "$OUT_A" | grep -q 'classify in ALLOW or EXCLUDE' && \
   printf '%s' "$OUT_A" | grep -q 'will not clear on re-run'; then
  pass "(d) check_error_message_preempts_retry: --check failure message states the remedy AND that re-running will not clear it"
else
  fail "(d) check_error_message_preempts_retry: --check failure message states the remedy AND that re-running will not clear it" "out=$OUT_A"
fi

# The anti-retry clause must NOT bleed into the success path (FIX_C, case c
# above) — a check that prints advice on healthy runs trains people to ignore
# it, which is this bead's own failure mode arriving one layer up.
if printf '%s' "$OUT_C" | grep -q 'will not clear on re-run'; then
  fail "(d) check_error_message_preempts_retry: success path stays silent about retries" "out=$OUT_C"
else
  pass "(d) check_error_message_preempts_retry: success path stays silent about retries"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
