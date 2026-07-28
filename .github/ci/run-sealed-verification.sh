#!/usr/bin/env bash
# run-sealed-verification.sh — one-job, fail-closed ci-verify orchestrator.
#
# Python, shell, and static authority checks are independent verdict lanes.
# Running them sequentially made every merge preview pay their summed wall
# time even though GitHub gives the job a dedicated runner. This keeps
# one Actions job per rolling worker preview while overlapping the lanes; the
# shell lane deliberately keeps its existing serial execution.
#
# The parent owns every child PID, records its exact `wait` status, and replays
# each log under a named Actions group. A lane without an attributable status
# is RED, so concurrency cannot erase a crashed lane or bless statusless work.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON_BIN="${SABLE_CI_PYTHON_BIN:-python}"
SHELL_RUN_SET="${SABLE_CI_SHELL_RUN_SET:-$REPO/.github/ci/shell-run-set.sh}"
STATIC_RUNNER="${SABLE_CI_STATIC_RUNNER:-}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/sable-sealed-verification.XXXXXX")" || {
  echo "::error::could not create sealed-verification workspace" >&2
  exit 2
}
PIDS=()
LANES=()

cleanup() {
  local pid
  for pid in "${PIDS[@]}"; do
    kill -0 "$pid" 2>/dev/null && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
on_signal() {
  exit 130
}
trap cleanup EXIT
trap on_signal HUP INT TERM

run_python_lane() {
  "$PYTHON_BIN" -m pytest bin/ -q -p no:cacheprovider
}

run_shell_lane() {
  bash "$SHELL_RUN_SET" --run
}

run_static_lane() {
  if [ -n "$STATIC_RUNNER" ]; then
    bash "$STATIC_RUNNER"
    return
  fi
  python bin/sable-fixture-tripwire
  # --check prints the same coverage manifest as --manifest and additionally
  # enforces it. A separate --manifest pass was duplicate work and evidence.
  bash .github/ci/shell-run-set.sh --check
  python bin/columbo-cost-prefilter.py --check-load-declarations
  bash .github/ci/impact-manifest.sh --check
}

start_lane() {
  local name="$1"
  shift
  "$@" > "$WORK/$name.log" 2>&1 &
  PIDS+=("$!")
  LANES+=("$name")
}

cd "$REPO"
start_lane python run_python_lane
start_lane shell run_shell_lane
start_lane static run_static_lane

RCS=()
for pid in "${PIDS[@]}"; do
  wait "$pid"
  RCS+=("$?")
done
PIDS=()

failed=()
for index in "${!LANES[@]}"; do
  lane="${LANES[$index]}"
  log="$WORK/$lane.log"
  echo "::group::sealed-verification / $lane"
  [ -f "$log" ] && cat "$log"
  echo "::endgroup::"

  if [ -z "${RCS[$index]+x}" ]; then
    echo "::error::sealed-verification lane $lane has no attributable child status"
    failed+=("$lane (missing status)")
    continue
  fi
  rc="${RCS[$index]}"
  echo "sealed-verification lane $lane: rc=$rc"
  [ "$rc" -eq 0 ] || failed+=("$lane (rc=$rc)")
done

echo "======================================================================"
if [ "${#failed[@]}" -gt 0 ]; then
  echo "::error::sealed verification RED — ${#failed[@]} lane(s) failed:"
  printf '  - %s\n' "${failed[@]}"
  exit 1
fi
echo "sealed verification GREEN — python + shell + static authority all passed"
