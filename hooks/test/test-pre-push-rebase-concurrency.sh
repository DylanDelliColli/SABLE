#!/usr/bin/env bash
# test-pre-push-rebase-concurrency.sh — real filesystem concurrency plant for
# test-pre-push-rebase-test.sh's fixture-root allocation (SABLE-z776).
#
# The old regression harness ran two complete 58-case copies of the behavioral
# suite, then ci-verify ran that suite a third time. The race it guards is much
# narrower: concurrent invocations once shared fixed /tmp fixture paths and
# deleted or overwrote each other's repositories. The behavioral suite now
# calls the helper below, and this plant races that exact helper directly.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SUITE="$DIR/test-pre-push-rebase-test.sh"
HELPER="$DIR/lib-pre-push-fixture-root.sh"
PASS=0
FAIL=0

pass() {
  PASS=$((PASS + 1))
  echo "PASS: $1"
}
fail() {
  FAIL=$((FAIL + 1))
  echo "FAIL: $1"
  [ -n "${2:-}" ] && echo "  $2"
}

if [ ! -f "$SUITE" ] || [ ! -r "$HELPER" ]; then
  echo "FAIL: suite/helper missing: $SUITE / $HELPER"
  exit 2
fi

# shellcheck source=lib-pre-push-fixture-root.sh
. "$HELPER"

# Couple the plant to the real suite. If the suite stops using the raced helper,
# this test must fail rather than keep proving an orphaned abstraction.
if grep -qF '. "$DIR/lib-pre-push-fixture-root.sh"' "$SUITE" && \
   grep -qF 'TMPROOT="$(sable_pre_push_fixture_root)"' "$SUITE"; then
  pass "the behavioral suite allocates through the concurrency-tested helper"
else
  fail "the behavioral suite must allocate through the concurrency-tested helper"
fi

# The original defect's fixed paths may remain in explanatory comments, but
# never in executable suite lines.
if sed 's/[[:space:]]*#.*$//' "$SUITE" \
     | grep -Eq '/tmp/sable-test-(pre-push|4amz|fofc|tripwire)'; then
  fail "the behavioral suite reintroduced an executable fixed /tmp fixture path"
else
  pass "no executable fixed /tmp fixture path has returned"
fi

N="${SABLE_CONCURRENCY_N:-2}"
case "$N" in
  ''|*[!0-9]*)
    echo "FAIL: SABLE_CONCURRENCY_N must be an integer >= 2"
    exit 2 ;;
esac
if [ "$N" -lt 2 ]; then
  echo "FAIL: SABLE_CONCURRENCY_N must be >= 2 (got $N)"
  exit 2
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-concurrency.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

fixture_worker() {
  local worker="$1" root="" seen=0
  root="$(
    TMPDIR="$WORKDIR"
    export TMPDIR
    sable_pre_push_fixture_root
  )" || exit 2
  printf '%s\n' "$root" > "$WORKDIR/root.$worker"
  printf '%s\n' "$worker" > "$root/owner"
  touch "$WORKDIR/ready.$worker"

  for _ in $(seq 1 200); do
    seen=$(find "$WORKDIR" -maxdepth 1 -name 'ready.*' -type f | wc -l)
    [ "$seen" -eq "$N" ] && break
    sleep 0.01
  done
  [ "$seen" -eq "$N" ] || exit 3
  [ "$(cat "$root/owner" 2>/dev/null)" = "$worker" ] || exit 4
}

pids=()
for worker in $(seq 1 "$N"); do
  fixture_worker "$worker" > "$WORKDIR/out.$worker" 2>&1 &
  pids+=("$!")
done

worker_failures=0
for pid in "${pids[@]}"; do
  wait "$pid" || worker_failures=$((worker_failures + 1))
done

root_count=$(cat "$WORKDIR"/root.* 2>/dev/null | sort -u | wc -l)
if [ "$root_count" -eq "$N" ]; then
  pass "$N concurrent allocations produced $N distinct fixture roots"
else
  fail "$N concurrent allocations must produce $N distinct fixture roots" \
    "distinct=$root_count roots=$(cat "$WORKDIR"/root.* 2>/dev/null)"
fi

if [ "$worker_failures" -eq 0 ]; then
  pass "every concurrent worker retained its own sentinel through the barrier"
else
  fail "concurrent workers must not overwrite each other's sentinels" \
    "failed_workers=$worker_failures"
fi

echo
echo "=========================================="
echo "Tests: $((PASS + FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
[ "$FAIL" -eq 0 ]
