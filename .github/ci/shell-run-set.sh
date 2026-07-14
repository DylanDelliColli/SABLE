#!/usr/bin/env bash
# shell-run-set.sh — single source of truth for which hooks/test/test-*.sh
# suites the ci-verify workflow runs, and which it deliberately excludes.
#
# Two modes:
#   --run       execute the allowlist; exit non-zero if ANY suite reds (this is
#               the gating step the merge-preview ci-verify gate reads).
#   --manifest  print every hooks/test/test-*.sh as RUN / EXCLUDED(reason) /
#               UNCLASSIFIED, then a totals line. Informational (exit 0), but it
#               emits ::warning:: for any test file that is in neither list —
#               so a newly-added suite can never silently escape the gate
#               (the SABLE-7v3z silent-green trap). Widen ALLOW as the tracked
#               reds in EXCLUDE land.
#
# Why an explicit allowlist instead of "run them all": several suites are
# known-red or vacuously skip in the SABLE-59zu clean-room (no bd/dolt, no
# ambient tmux, no installed ~/.claude). Running them would red the gate on
# arrival (blocking every merge) or count a skip-and-exit-0 as green. Each
# exclusion below is recorded WITH its reason + tracking bead so the coverage
# gap is visible, not silent.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TESTDIR="$REPO/hooks/test"

# --- Known-green in the clean-room, verified at authoring (SABLE-oeci). --------
ALLOW=(
  test-active-contracts-integration.sh
  test-agent-definitions.sh
  test-bead-description-gate.sh
  test-columbo-quick-mode.sh
  test-concurrent-sessions.sh
  test-full-ingestion.sh
  test-lib-git-sandbox.sh
  test-lib-identity.sh
  test-lib-identity-isolation.sh
  test-lib-mode-path.sh
  test-mode-interlock.sh
  test-mode-tier.sh
  test-multi-manager-pattern.sh
  test-no-cockpit-naming.sh
  test-no-hook-autopush.sh
  test-orchestration-install.sh
  test-post-push-merge-notify.sh
  test-post-push-merge-notify-isolation.sh
  test-pre-dispatch-claim.sh
  test-pre-dispatch-model-check.sh
  test-pre-dispatch-overlap.sh
  test-pre-dispatch-preempt.sh
  test-pre-dispatch-refresh.sh
  test-pre-push-rebase-concurrency.sh
  test-pre-push-rebase-test.sh
  test-quickstart-orchestration.sh
  test-read-guard.sh
  test-registry.sh
  test-role.sh
  test-sable-bin-install.sh
  test-sable-claim.sh
  test-sable-cli.sh
  test-sable-contract.sh
  test-sable-discover.sh
  test-sable-launch.sh
  test-sable-mode.sh
  test-sable-plan-tiers.sh
  test-sable-skills.sh
  test-script-dir-symlink.sh
  test-session-role-anchor.sh
  test-sherlock-research.sh
  test-staged-planning-docs.sh
  test-tdd-evidence.sh
  test-tdd-gate.sh
  test-thesis-docs.sh
  test-tree-claim.sh
  test-worker-dispatch-template.sh
  test-worker-flag-done.sh
)

# --- Excluded from the run-set, each WITH reason + tracking bead. --------------
# Widen ALLOW (move an entry here into ALLOW) as each of these lands.
declare -A EXCLUDE=(
  [test-install.sh]="needs the ~/.claude SABLE install; clean-room has none (SABLE-59zu)"
  [test-install-guard.sh]="needs the ~/.claude SABLE install (real bd on PATH) for its --from-here/main-checkout proceed cases; clean-room has none (SABLE-59zu)"
  [test-install-agent-defs.sh]="needs the ~/.claude SABLE install (SABLE-59zu)"
  [test-install-version-floor.sh]="needs the ~/.claude SABLE install (SABLE-59zu)"
  [test-install-multi-manager.sh]="vacuous without bd — prints 'SKIP: bd not on PATH' and exits 0 (SABLE-59zu/SABLE-7v3z)"
  [test-tmux-e2e.sh]="vacuous without bd — prints 'SKIP: bd not installed' and exits 0 (SABLE-59zu)"
  [test-sable-msg.sh]="known-red: legacy fixed-name tmux sessions vs per-repo naming (SABLE-cncs)"
  [test-sable-worker-status.sh]="tracked-red under ambient tmux; green in clean-room but excluded pending confirmation (SABLE-b574)"
  [test-tmux-roles.sh]="known false-positive (SABLE-p9ih)"
)

in_array() { local n="$1"; shift; local e; for e in "$@"; do [ "$e" = "$n" ] && return 0; done; return 1; }

manifest() {
  local total=0 run=0 excl=0 uncl=0 f name
  for f in "$TESTDIR"/test-*.sh; do
    name="$(basename "$f")"
    total=$((total+1))
    if in_array "$name" "${ALLOW[@]}"; then
      printf 'RUN        %s\n' "$name"; run=$((run+1))
    elif [ -n "${EXCLUDE[$name]:-}" ]; then
      printf 'EXCLUDED   %s  — %s\n' "$name" "${EXCLUDE[$name]}"; excl=$((excl+1))
    else
      printf '::warning::UNCLASSIFIED %s — add it to ALLOW or EXCLUDE in .github/ci/shell-run-set.sh (SABLE-7v3z)\n' "$name"
      uncl=$((uncl+1))
    fi
  done
  echo "----------------------------------------------------------------------"
  echo "coverage manifest: $total total | $run run | $excl excluded | $uncl unclassified"
  # Drift guard: an ALLOW/EXCLUDE entry naming a file that no longer exists.
  for name in "${ALLOW[@]}" "${!EXCLUDE[@]}"; do
    [ -f "$TESTDIR/$name" ] || echo "::warning::stale run-set entry: $name is listed but not present in hooks/test/ (SABLE-7v3z)"
  done
}

run_set() {
  local failed=() name rc
  for name in "${ALLOW[@]}"; do
    if [ ! -f "$TESTDIR/$name" ]; then
      echo "::error::run-set names $name but it is missing from hooks/test/"; failed+=("$name (missing)"); continue
    fi
    echo "::group::$name"
    bash "$TESTDIR/$name"; rc=$?
    echo "::endgroup::"
    [ $rc -eq 0 ] || failed+=("$name (rc=$rc)")
  done
  echo "======================================================================"
  if [ ${#failed[@]} -eq 0 ]; then
    echo "ci-verify shell run-set: all ${#ALLOW[@]} suites GREEN"
    return 0
  fi
  echo "ci-verify shell run-set: ${#failed[@]} of ${#ALLOW[@]} suites RED:"
  printf '  - %s\n' "${failed[@]}"
  return 1
}

case "${1:-}" in
  --manifest) manifest ;;
  --run)      run_set ;;
  *) echo "usage: $0 --run | --manifest" >&2; exit 2 ;;
esac
