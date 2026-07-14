#!/usr/bin/env bash
# test-pre-push-rebase-concurrency.sh — Concurrency regression harness for
# test-pre-push-rebase-test.sh (SABLE-z776).
#
# The suite under test formerly hardcoded shared /tmp fixture paths
# (/tmp/sable-test-pre-push-repo, /tmp/sable-test-4amz-repo, …). Once the .sable
# testCommand went live (SABLE-hml), every SABLE-repo push runs that suite in the
# pre-push gate, so concurrent fleet pushes — and the nested case where the gate
# runs the suite while the suite itself invokes the gate — raced on those shared
# paths: two invocations would clobber each other's fixtures, producing
# nondeterministic test failures and missing-fixture errors like
# 'cannot change to /tmp/sable-test-4amz-repo: No such file or directory'.
#
# This harness launches N concurrent invocations of the suite and asserts EVERY
# one both (1) reports all-pass (summary line "Failed: 0") AND (2) emits zero
# missing-fixture errors. Before the fixture-isolation fix at least one run
# flaked; after it, all N pass deterministically because each invocation gets a
# private mktemp -d fixture root.
#
# This is an INTEGRATION test: it exercises real concurrent processes racing on
# the real filesystem, which is exactly the composition the unit suite (running
# solo) cannot observe. It is deliberately NOT wired into the .sable testCommand
# — the gate already runs the suite, and fanning the suite out N-fold inside the
# gate would multiply gate cost and re-introduce nesting depth. Run it directly:
#
#   bash hooks/test/test-pre-push-rebase-concurrency.sh
#
# Tune the fan-out with SABLE_CONCURRENCY_N (default 4).

set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SUITE="$DIR/test-pre-push-rebase-test.sh"

if [ ! -f "$SUITE" ]; then
  echo "FAIL: suite under test not found at $SUITE"
  exit 2
fi

N="${SABLE_CONCURRENCY_N:-4}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-concurrency.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT

echo "Launching $N concurrent invocations of $(basename "$SUITE")..."

pids=()
for i in $(seq 1 "$N"); do
  bash "$SUITE" > "$WORKDIR/out.$i" 2>&1 &
  pids+=("$!")
done

# Collect exit codes in launch order.
codes=()
for pid in "${pids[@]}"; do
  if wait "$pid"; then codes+=("0"); else codes+=("$?"); fi
done

FAIL=0
for i in $(seq 1 "$N"); do
  out="$WORKDIR/out.$i"
  code="${codes[$((i-1))]}"
  problems=""

  # (1) all-pass: the suite prints "... | Failed: N" — require N == 0 AND exit 0.
  if ! grep -qE 'Failed: 0$' "$out"; then
    problems="$problems not-all-pass(summary)"
    FAIL=1
  fi
  if [ "$code" != "0" ]; then
    problems="$problems nonzero-exit($code)"
    FAIL=1
  fi

  # (2) zero missing-fixture errors — the race signature.
  mf=$(grep -icE 'No such file or directory|cannot change to' "$out")
  if [ "$mf" != "0" ]; then
    problems="$problems missing-fixture-errors($mf)"
    FAIL=1
  fi

  summary=$(grep -E 'Tests: ' "$out" | tail -1)
  if [ -z "$problems" ]; then
    echo "PASS: run $i — ${summary:-<no summary>}"
  else
    echo "FAIL: run $i —${problems}"
    echo "  ${summary:-<no summary line>}"
  fi
done

echo
echo "=========================================="
if [ "$FAIL" -eq 0 ]; then
  echo "Concurrency ($N invocations): ALL PASS, zero fixture races"
  echo "=========================================="
  exit 0
else
  echo "Concurrency ($N invocations): FIXTURE RACE DETECTED"
  echo "=========================================="
  exit 1
fi
