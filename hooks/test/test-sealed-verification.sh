#!/usr/bin/env bash
# test-sealed-verification.sh — regression tests for the one-job CI scheduler.
#
# The green plant uses a three-party barrier: it can pass only if Python,
# shell, and static authority lanes are alive at the same time. The red plant
# then fails one lane deliberately and proves the parent preserves that
# lane's output and exit polarity instead of blessing the two green siblings.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$REPO/.github/ci/run-sealed-verification.sh"
PASS=0
FAIL=0
FAIL_NAMES=""

pass() {
  PASS=$((PASS + 1))
  echo "PASS: $1"
}
fail() {
  FAIL=$((FAIL + 1))
  FAIL_NAMES="$FAIL_NAMES\n  $1"
  echo "FAIL: $1"
  [ -n "${2:-}" ] && echo "  $2"
}

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-sealed-verification.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

cat > "$TMPROOT/common-lane" <<'SH'
#!/usr/bin/env bash
set -u
lane="$1"
shift
touch "$SABLE_TEST_BARRIER/$lane.started"
for _ in $(seq 1 100); do
  started=("$SABLE_TEST_BARRIER"/*.started)
  [ "${#started[@]}" -eq 3 ] && break
  sleep 0.01
done
started=("$SABLE_TEST_BARRIER"/*.started)
if [ "${#started[@]}" -ne 3 ]; then
  echo "$lane never overlapped all three verdict lanes"
  exit 90
fi
printf '%s\n' "$*" > "$SABLE_TEST_BARRIER/$lane.args"
echo "observable output from $lane"
if [ "${SABLE_TEST_FAIL_LANE:-}" = "$lane" ]; then
  exit 41
fi
SH
chmod +x "$TMPROOT/common-lane"

cat > "$TMPROOT/fake-python" <<'SH'
#!/usr/bin/env bash
exec "$SABLE_TEST_COMMON" python "$@"
SH
cat > "$TMPROOT/fake-shell-run-set" <<'SH'
#!/usr/bin/env bash
exec "$SABLE_TEST_COMMON" shell "$@"
SH
cat > "$TMPROOT/fake-static" <<'SH'
#!/usr/bin/env bash
exec "$SABLE_TEST_COMMON" static "$@"
SH
chmod +x "$TMPROOT/fake-python" "$TMPROOT/fake-shell-run-set" "$TMPROOT/fake-static"

run_fixture() {
  local barrier="$1"
  mkdir -p "$barrier"
  SABLE_TEST_COMMON="$TMPROOT/common-lane" \
  SABLE_TEST_BARRIER="$barrier" \
  SABLE_CI_PYTHON_BIN="$TMPROOT/fake-python" \
  SABLE_CI_SHELL_RUN_SET="$TMPROOT/fake-shell-run-set" \
  SABLE_CI_STATIC_RUNNER="$TMPROOT/fake-static" \
    bash "$RUNNER"
}

GREEN_BARRIER="$TMPROOT/green"
OUT_GREEN=$(run_fixture "$GREEN_BARRIER" 2>&1)
RC_GREEN=$?
if [ "$RC_GREEN" -eq 0 ] && \
   [ -f "$GREEN_BARRIER/python.started" ] && \
   [ -f "$GREEN_BARRIER/shell.started" ] && \
   [ -f "$GREEN_BARRIER/static.started" ] && \
   printf '%s' "$OUT_GREEN" | grep -q 'sealed verification GREEN'; then
  pass "all three authority lanes overlap and jointly produce one green verdict"
else
  fail "all three authority lanes overlap and jointly produce one green verdict" \
    "rc=$RC_GREEN out=$OUT_GREEN"
fi

if [ "$(cat "$GREEN_BARRIER/python.args")" = \
     "-m pytest bin/ -q -p no:cacheprovider" ] && \
   [ "$(cat "$GREEN_BARRIER/shell.args")" = "--run" ] && \
   [ -f "$GREEN_BARRIER/static.args" ] && \
   [ -z "$(cat "$GREEN_BARRIER/static.args")" ]; then
  pass "the scheduler invokes the complete Python suite, serial shell authority, and static authority"
else
  fail "the scheduler invokes the complete Python suite, serial shell authority, and static authority" \
    "python=$(cat "$GREEN_BARRIER/python.args" 2>/dev/null) shell=$(cat "$GREEN_BARRIER/shell.args" 2>/dev/null) static=$(cat "$GREEN_BARRIER/static.args" 2>/dev/null)"
fi

for lane in python shell static; do
  if printf '%s' "$OUT_GREEN" | grep -q "observable output from $lane" && \
     printf '%s' "$OUT_GREEN" | grep -q "sealed-verification lane $lane: rc=0"; then
    pass "$lane output and successful status are replayed under the combined verdict"
  else
    fail "$lane output and successful status are replayed under the combined verdict" \
      "out=$OUT_GREEN"
  fi
done

RED_BARRIER="$TMPROOT/red"
OUT_RED=$(SABLE_TEST_FAIL_LANE=shell run_fixture "$RED_BARRIER" 2>&1)
RC_RED=$?
if [ "$RC_RED" -ne 0 ] && \
   printf '%s' "$OUT_RED" | grep -q 'shell (rc=41)' && \
   printf '%s' "$OUT_RED" | grep -q 'observable output from shell' && \
   printf '%s' "$OUT_RED" | grep -q 'sealed-verification lane python: rc=0' && \
   printf '%s' "$OUT_RED" | grep -q 'sealed-verification lane static: rc=0'; then
  pass "one red lane fails the whole verdict while preserving every sibling result"
else
  fail "one red lane fails the whole verdict while preserving every sibling result" \
    "rc=$RC_RED out=$OUT_RED"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed tests:$FAIL_NAMES"
  exit 1
fi
