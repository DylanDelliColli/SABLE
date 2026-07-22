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
# ALSO GUARDS the exclusion-rot mechanism (SABLE-wqe2e), cases (e)/(e2)/(f):
# an EXCLUDE entry whose file EXISTS but whose tracking bead has been CLOSED is
# invisible to the staleness guard above, so a suite can stay ungated for weeks
# after its blocker is fixed. --check enforces that every reason carries a
# parseable [blocked-by:]/[permanent:] tag (no bd needed, so it runs in the
# clean room); the local-only --check-beads resolves those ids against the real
# bead store.
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
set_allow_exclude "$FIX_C" test-fixture-allowed.sh --SEP-- "test-fixture-excluded.sh=known-red, tracked [blocked-by: SABLE-fixture]"

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

# ---------------------------------------------------------------------------
# Case (e) exclusion whose tracking bead is closed is flagged (SABLE-wqe2e).
#
# THE DEFECT: --check's staleness guard only catches an EXCLUDE entry naming a
# file that is ABSENT. An entry whose file EXISTS and whose tracking bead has
# been CLOSED is indistinguishable from a legitimately-blocked suite, so the
# gate reports itself clean while a suite that has been green for weeks stays
# ungated (test-sable-msg.sh sat that way for ~2 weeks citing the closed
# SABLE-cncs — SABLE-8onsf).
#
# Driven against FIXTURE EXCLUDE arrays with a STUBBED bd, never the real
# backlog: a unit test that queries live bead statuses would change verdict
# whenever someone closes a bead, which is the opposite of a regression test.
# The real store is exercised separately by case (f) below.
# ---------------------------------------------------------------------------
STUBBIN="$TMPROOT/stubbin"
mkdir -p "$STUBBIN"
cat > "$STUBBIN/bd" <<'STUB'
#!/usr/bin/env bash
# Stub bd: only `bd show <id> --json` is used by --check-beads.
case "${2:-}" in
  SABLE-fixclosed|SABLE-fixclosed2) printf '[{"id":"%s","status":"closed"}]\n' "$2" ;;
  SABLE-fixopen)                    printf '[{"id":"%s","status":"open"}]\n'   "$2" ;;
  *) printf '[]\n'; exit 1 ;;
esac
STUB
chmod +x "$STUBBIN/bd"

FIX_E="$(new_fixture case-e)"
for s in stale fresh partial permanent ghostbead; do
  touch_suite "$FIX_E/hooks/test/test-fixture-$s.sh"
done
set_allow_exclude "$FIX_E" --SEP-- \
  "test-fixture-stale.sh=known-red [blocked-by: SABLE-fixclosed]" \
  "test-fixture-fresh.sh=known-red [blocked-by: SABLE-fixopen]" \
  "test-fixture-partial.sh=known-red [blocked-by: SABLE-fixclosed SABLE-fixopen]" \
  "test-fixture-permanent.sh=structural, needs the install [permanent: SABLE-fixclosed]" \
  "test-fixture-ghostbead.sh=known-red [blocked-by: SABLE-typoed]"

OUT_E=$(PATH="$STUBBIN:$PATH" bash "$FIX_E/.github/ci/shell-run-set.sh" --check-beads 2>&1); RC_E=$?

if [ "$RC_E" -ne 0 ] && printf '%s' "$OUT_E" | grep -q 'test-fixture-stale.sh' \
   && printf '%s' "$OUT_E" | grep 'test-fixture-stale.sh' | grep -q 'SABLE-fixclosed'; then
  pass "(e) exclusion whose tracking bead is closed is flagged — rc!=0 and the message names BOTH the suite file and the closed bead id"
else
  fail "(e) exclusion whose tracking bead is closed is flagged — rc!=0 and the message names BOTH the suite file and the closed bead id" "rc=$RC_E out=$OUT_E"
fi

if printf '%s' "$OUT_E" | grep -q 'test-fixture-fresh.sh'; then
  fail "(e) exclusion whose sole blocker is still OPEN is NOT flagged" "out=$OUT_E"
else
  pass "(e) exclusion whose sole blocker is still OPEN is NOT flagged"
fi

# Promotion requires EVERY blocker cleared — one open bead keeps the exclusion
# legitimate, so a partial close must stay silent.
if printf '%s' "$OUT_E" | grep -q 'test-fixture-partial.sh'; then
  fail "(e) exclusion citing two blockers, only one closed, is NOT flagged (promotion needs ALL cleared)" "out=$OUT_E"
else
  pass "(e) exclusion citing two blockers, only one closed, is NOT flagged (promotion needs ALL cleared)"
fi

# [permanent: ...] cites its bead as CONTEXT, not as a blocker awaiting a fix
# (e.g. SABLE-59zu, the closed bead documenting the clean room's hermeticity).
# Flagging those would fire on 8 of the 10 real entries on day one and the gate
# would simply be suppressed.
if printf '%s' "$OUT_E" | grep -q 'test-fixture-permanent.sh'; then
  fail "(e) [permanent:] exclusion is NOT flagged when its cited bead is closed" "out=$OUT_E"
else
  pass "(e) [permanent:] exclusion is NOT flagged when its cited bead is closed"
fi

# Second, distinct rot mode: a reason citing an id that does not resolve at all.
if [ "$RC_E" -ne 0 ] && printf '%s' "$OUT_E" | grep 'test-fixture-ghostbead.sh' | grep -q 'SABLE-typoed'; then
  pass "(e) exclusion citing an UNRESOLVABLE bead id is flagged and named (distinct rot mode from 'closed')"
else
  fail "(e) exclusion citing an UNRESOLVABLE bead id is flagged and named (distinct rot mode from 'closed')" "rc=$RC_E out=$OUT_E"
fi

# No bd on PATH => rc 0 with an EXPLICIT skip line. A silent pass here would
# reproduce the very false-green this case exists to close, in the one
# environment (the SABLE-59zu clean room) where the gate is mandatory.
OUT_E_NOBD=$(env PATH=/usr/bin:/bin /bin/bash "$FIX_E/.github/ci/shell-run-set.sh" --check-beads 2>&1); RC_E_NOBD=$?
if [ "$RC_E_NOBD" -eq 0 ] && printf '%s' "$OUT_E_NOBD" | grep -q 'SKIP: bd not on PATH'; then
  pass "(e) no bd on PATH -> --check-beads exits 0 with an explicit 'SKIP: bd not on PATH' line, never a silent no-op"
else
  fail "(e) no bd on PATH -> --check-beads exits 0 with an explicit 'SKIP: bd not on PATH' line, never a silent no-op" "rc=$RC_E_NOBD out=$OUT_E_NOBD"
fi

# A clean fixture (every blocker open, permanent entries only otherwise) must
# exit 0 — the gate has to be quiet when there is nothing to promote, or it
# gets ignored the way case (d) guards against.
FIX_E2="$(new_fixture case-e-clean)"
touch_suite "$FIX_E2/hooks/test/test-fixture-fresh.sh"
set_allow_exclude "$FIX_E2" --SEP-- "test-fixture-fresh.sh=known-red [blocked-by: SABLE-fixopen]"
OUT_E2=$(PATH="$STUBBIN:$PATH" bash "$FIX_E2/.github/ci/shell-run-set.sh" --check-beads 2>&1); RC_E2=$?
if [ "$RC_E2" -eq 0 ]; then
  pass "(e) all blockers still open -> --check-beads exits 0"
else
  fail "(e) all blockers still open -> --check-beads exits 0" "rc=$RC_E2 out=$OUT_E2"
fi

# ---------------------------------------------------------------------------
# Case (e2): tracking-tag WELL-FORMEDNESS is enforced by --check, which is the
# half of the guard the bd-less clean room can run. Without it an author could
# write a reason with no parseable bead id at all and the local freshness gate
# would have nothing to check — the exclusion would be unauditable by either
# mode.
# ---------------------------------------------------------------------------
FIX_G="$(new_fixture case-g)"
touch_suite "$FIX_G/hooks/test/test-fixture-untagged.sh"
set_allow_exclude "$FIX_G" --SEP-- "test-fixture-untagged.sh=known-red, tracked (SABLE-fixopen)"

OUT_G=$(bash "$FIX_G/.github/ci/shell-run-set.sh" --check 2>&1); RC_G=$?
if [ "$RC_G" -ne 0 ] && printf '%s' "$OUT_G" | grep -q 'test-fixture-untagged.sh'; then
  pass "(e2) EXCLUDE reason with no [blocked-by:]/[permanent:] tag -> --check exits non-zero and names the suite (no bd needed)"
else
  fail "(e2) EXCLUDE reason with no [blocked-by:]/[permanent:] tag -> --check exits non-zero and names the suite (no bd needed)" "rc=$RC_G out=$OUT_G"
fi

# ...and the tagged shape passes, so (e2) is asserting the tag specifically
# and not some unrelated property of the fixture.
FIX_G2="$(new_fixture case-g-ok)"
touch_suite "$FIX_G2/hooks/test/test-fixture-tagged.sh"
set_allow_exclude "$FIX_G2" --SEP-- "test-fixture-tagged.sh=known-red [blocked-by: SABLE-fixopen]"
OUT_G2=$(bash "$FIX_G2/.github/ci/shell-run-set.sh" --check 2>&1); RC_G2=$?
if [ "$RC_G2" -eq 0 ]; then
  pass "(e2) EXCLUDE reason carrying a well-formed tracking tag -> --check exits 0"
else
  fail "(e2) EXCLUDE reason carrying a well-formed tracking tag -> --check exits 0" "rc=$RC_G2 out=$OUT_G2"
fi

# ---------------------------------------------------------------------------
# Case (f) real EXCLUDE entries resolve against the real bd store.
#
# INTEGRATION, deliberately not stubbed: the unit fixtures above can never
# catch a reason citing a typo'd or deleted bead id in the ACTUAL run-set,
# because the stub decides what resolves. This runs the production script's
# own --check-beads against the production EXCLUDE table and the real bead
# store. Self-skips when bd is absent (the clean room), per SABLE-59zu.
# ---------------------------------------------------------------------------
if command -v bd >/dev/null 2>&1; then
  OUT_F=$(bash "$PROD" --check-beads 2>&1); RC_F=$?
  if [ "$RC_F" -eq 0 ]; then
    pass "(f) real EXCLUDE entries resolve against the real bd store — every cited id resolves and no blocked-by entry has all blockers closed"
  else
    fail "(f) real EXCLUDE entries resolve against the real bd store — every cited id resolves and no blocked-by entry has all blockers closed" "rc=$RC_F out=$OUT_F"
  fi
else
  echo "SKIP: (f) real EXCLUDE entries resolve against the real bd store — bd not on PATH (SABLE-59zu clean room)"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
