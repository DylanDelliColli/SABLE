#!/usr/bin/env bash
# test-tiers.sh — per-repo CI-tier SSOT + loader (SABLE-cmar4.1).
#
# The ONE place a repo's three CI tiers' suite membership and duration
# budgets live. Consumers — hooks/multi-manager/pre-push-rebase-test.sh
# (pre_push), bin/sable-merge-gate (merge_preview budget), and the future
# jd5fj.5 snapshot runner (full_snapshot) — source this file or shell out to
# its CLI instead of hardcoding their own suite lists. No duplicated test
# lists anywhere (SABLE-cmar4 S1).
#
# Tiers:
#   pre_push       fast subset run by the local pre-push hook.
#   merge_preview  suites the merge-preview ci-verify gate runs. Today this is
#                  the full .github/ci/shell-run-set.sh ALLOW list — narrowing
#                  it to only suites reachable from the diff (impact-scoping)
#                  is future work on SABLE-cmar4's S1 story; the tier boundary
#                  exists now so that work has a home without another SSOT.
#   full_snapshot  the full-suite green snapshot. Today this is also the same
#                  ALLOW list — a real periodic/cadence-driven snapshot runner
#                  is SABLE-jd5fj.5 and does not exist yet; this tier is
#                  reserved for it.
#
# merge_preview and full_snapshot alias shell-run-set.sh's ALLOW array BY
# REFERENCE (sourced, not copied) so that file stays the one place ALLOW/
# EXCLUDE membership, the SABLE-7v3z UNCLASSIFIED silent-green trap, and the
# stale-entry drift guard live.
#
# Usage (CLI):
#   test-tiers.sh --names                  print valid tier names, one per line
#   test-tiers.sh --list    <tier>         print suite filenames in <tier>
#   test-tiers.sh --budget  <tier>         print the tier's duration budget (seconds)
#   test-tiers.sh --run     <tier>         run every suite in <tier>; exit non-zero on any red
#
# Usage (sourced, bash lib — e.g. from another hooks/multi-manager/*.sh):
#   . .github/ci/test-tiers.sh
#   sable_tier_names                       # -> pre_push\nmerge_preview\nfull_snapshot
#   sable_tier_suites merge_preview        # -> suite filenames, one per line; rc=1 unknown tier
#   sable_tier_budget_sec pre_push         # -> seconds; rc=1 unknown tier
#
# An unknown tier name is rejected (rc=1, message to stderr) from every entry
# point — CLI and lib alike.
set -uo pipefail

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$CI_DIR/../.." && pwd)"
TESTDIR="$REPO/hooks/test"

# shellcheck source=shell-run-set.sh
# Sourcing (not executing) shell-run-set.sh pulls in ALLOW/EXCLUDE without
# triggering its own CLI dispatch — see the sourced-vs-executed guard at the
# bottom of that file (SABLE-cmar4.1).
. "$CI_DIR/shell-run-set.sh"

# --- Tier declarations (the SSOT) -------------------------------------------

SABLE_TIER_NAMES=(pre_push merge_preview full_snapshot)

declare -A SABLE_TIER_BUDGET_SEC=(
  [pre_push]=90
  [merge_preview]=900
  [full_snapshot]=1800
)

# pre_push: curated fast subset for the local pre-push hook. Previously
# hardcoded as a shell loop in this repo's own .sable `testCommand=` line —
# that duplication is gone now; .sable instead calls `--run pre_push` below.
SABLE_TIER_PRE_PUSH=(
  test-pre-push-rebase-test.sh
  test-lib-identity.sh
  test-tdd-gate.sh
  test-worktree-isolation.sh
)

SABLE_TIER_MERGE_PREVIEW=("${ALLOW[@]}")
SABLE_TIER_FULL_SNAPSHOT=("${ALLOW[@]}")

_sable_tier_array_name() {
  case "$1" in
    pre_push)      echo SABLE_TIER_PRE_PUSH ;;
    merge_preview) echo SABLE_TIER_MERGE_PREVIEW ;;
    full_snapshot) echo SABLE_TIER_FULL_SNAPSHOT ;;
    *) return 1 ;;
  esac
}

sable_tier_names() { printf '%s\n' "${SABLE_TIER_NAMES[@]}"; }

sable_tier_suites() {
  local tier="${1:-}" arrname
  arrname=$(_sable_tier_array_name "$tier") || {
    echo "test-tiers.sh: unknown tier '$tier' (valid: ${SABLE_TIER_NAMES[*]})" >&2
    return 1
  }
  local -n arr="$arrname"
  printf '%s\n' "${arr[@]}"
}

sable_tier_budget_sec() {
  local tier="${1:-}"
  if [ -z "${SABLE_TIER_BUDGET_SEC[$tier]+x}" ]; then
    echo "test-tiers.sh: unknown tier '$tier' (valid: ${SABLE_TIER_NAMES[*]})" >&2
    return 1
  fi
  printf '%s\n' "${SABLE_TIER_BUDGET_SEC[$tier]}"
}

sable_tier_run() {
  local tier="${1:-}" name failed=() rc
  sable_tier_suites "$tier" >/dev/null || return 1
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    if [ ! -f "$TESTDIR/$name" ]; then
      echo "::error::tier $tier names $name but it is missing from hooks/test/" >&2
      failed+=("$name (missing)"); continue
    fi
    echo "::group::$name"
    bash "$TESTDIR/$name"; rc=$?
    echo "::endgroup::"
    [ "$rc" -eq 0 ] || failed+=("$name (rc=$rc)")
  done < <(sable_tier_suites "$tier")
  if [ ${#failed[@]} -eq 0 ]; then
    echo "test-tiers.sh: tier $tier — all suites GREEN"
    return 0
  fi
  echo "test-tiers.sh: tier $tier — ${#failed[@]} suite(s) RED:"
  printf '  - %s\n' "${failed[@]}"
  return 1
}

# --- CLI dispatch (only when executed directly, not sourced) ---------------
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1:-}" in
    --names)  sable_tier_names ;;
    --list)   sable_tier_suites "${2:-}" ;;
    --budget) sable_tier_budget_sec "${2:-}" ;;
    --run)    sable_tier_run "${2:-}" ;;
    *) echo "usage: $0 --names | --list <tier> | --budget <tier> | --run <tier>" >&2; exit 2 ;;
  esac
fi
