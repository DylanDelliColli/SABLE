#!/usr/bin/env bash
# shell-run-set.sh — single source of truth for which hooks/test/test-*.sh
# suites the ci-verify workflow runs, and which it deliberately excludes.
#
# Three modes:
#   --run       execute the allowlist; exit non-zero if ANY suite reds (this is
#               the gating step the merge-preview ci-verify gate reads).
#   --manifest  print every hooks/test/test-*.sh as RUN / EXCLUDED(reason) /
#               UNCLASSIFIED, then a totals line. Informational (exit 0) — kept
#               for human-readable output — but it emits ::warning:: for any
#               test file that is in neither list.
#   --check     the SAME classification as --manifest, but FAIL-CLOSED
#               (SABLE-lcevs): exits non-zero if any suite is UNCLASSIFIED or
#               any ALLOW/EXCLUDE entry is stale (listed but the file is
#               absent). A ::warning:: annotation alone does not fail a GitHub
#               Actions job, so --manifest's warnings were silently ignorable —
#               that is precisely how SABLE-7v3z kept recurring as one-bead-
#               per-file instances instead of being closed at the root. --check
#               is what ci-verify.yml wires as a REQUIRED step; --manifest
#               stays for readability only.
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
  test-chuck-role-contract.sh
  test-columbo-quick-mode.sh
  test-concurrent-sessions.sh
  test-control-trace.sh
  test-edit-write-claim-reconciler.sh
  test-event-pair.sh
  test-full-ingestion.sh
  test-identity-hermeticity.sh
  test-impact-manifest.sh
  test-impact-selection.sh
  test-install-preserves-pins.sh
  test-lib-git-sandbox.sh
  test-lib-hook-trace.sh
  test-lib-identity.sh
  test-lib-identity-isolation.sh
  test-lib-mode-path.sh
  test-lib-registry-path.sh
  test-mode-interlock.sh
  test-mode-tier.sh
  test-multi-manager-pattern.sh
  test-no-cockpit-naming.sh
  test-no-hook-autopush.sh
  test-orchestration-install.sh
  test-overlap-constraint.sh
  test-overlap-dispatch-e2e.sh
  test-parallel-previews.sh
  test-post-push-merge-notify.sh
  test-post-push-merge-notify-isolation.sh
  test-pre-dispatch-claim.sh
  test-pre-dispatch-model-check.sh
  test-pre-dispatch-preempt.sh
  test-pre-dispatch-refresh.sh
  test-pre-push-rebase-concurrency.sh
  test-pre-push-rebase-test.sh
  test-preview-already-verified.sh
  test-preview-kick.sh
  test-project-clone-portability.sh
  test-quickstart-orchestration.sh
  test-read-guard.sh
  test-registry.sh
  test-role.sh
  test-sable-bin-install.sh
  test-sable-claim.sh
  test-sable-clean-room-verify.sh
  test-sable-clean-room-verify-integration.sh
  test-sable-cli.sh
  test-sable-contract.sh
  test-sable-discover.sh
  test-sable-launch.sh
  test-sable-mode.sh
  test-sable-onboarding-skill.sh
  test-sable-plan-tiers.sh
  test-sable-skills.sh
  test-sable-test.sh
  test-script-dir-symlink.sh
  test-session-role-anchor.sh
  test-shell-run-set-strict.sh
  test-sherlock-research.sh
  test-spine-pinning.sh
  test-staged-planning-docs.sh
  test-stash-worktree-guard.sh
  test-tdd-evidence.sh
  test-tdd-gate.sh
  test-thesis-docs.sh
  test-tier-ssot.sh
  test-tier-ssot-consumers.sh
  test-tree-claim.sh
  test-worker-dispatch-template.sh
  test-worker-flag-done.sh
  test-worktree-isolation.sh
)

# --- Excluded from the run-set, each WITH reason + tracking bead. --------------
# Widen ALLOW (move an entry here into ALLOW) as each of these lands.
declare -A EXCLUDE=(
  [test-install.sh]="needs the ~/.claude SABLE install; clean-room has none (SABLE-59zu)"
  [test-install-guard.sh]="needs the ~/.claude SABLE install (real bd on PATH) for its --from-here/main-checkout proceed cases; clean-room has none (SABLE-59zu)"
  [test-install-agent-defs.sh]="needs the ~/.claude SABLE install (SABLE-59zu)"
  [test-install-version-floor.sh]="needs the ~/.claude SABLE install (SABLE-59zu)"
  [test-install-multi-manager.sh]="vacuous without bd — prints 'SKIP: bd not on PATH' and exits 0 (SABLE-59zu/SABLE-7v3z)"
  [test-quickstart-project.sh]="needs the ~/.claude SABLE install (bd, sable-doctor) for its E2E bootstrap-flow cases; clean-room has none (SABLE-59zu, SABLE-vivm)"
  [test-tmux-e2e.sh]="vacuous without bd — prints 'SKIP: bd not installed' and exits 0 (SABLE-59zu)"
  [test-sable-msg.sh]="known-red: legacy fixed-name tmux sessions vs per-repo naming (SABLE-cncs)"
  [test-sable-worker-status.sh]="tracked-red under ambient tmux; green in clean-room but excluded pending confirmation (SABLE-b574)"
  [test-tmux-roles.sh]="known false-positive (SABLE-p9ih)"
)

# --- Shell impact manifest (SABLE-cmar4.2) --------------------------------
# COVERS: suite name -> the production file(s) it most directly exercises
# (space-separated repo-relative paths). A suite absent from this array
# defaults to covering itself (hooks/test/<suite>) — the common self-test
# pattern (e.g. test-lib-identity.sh directly sources lib-identity.sh).
# Consumed by .github/ci/impact-manifest.sh; do not hand-derive this
# elsewhere — that recreates the duplicated-list problem cmar4.1 already
# closed for tier membership.
declare -A COVERS=(
  [test-active-contracts-integration.sh]="hooks/multi-manager/session-role-anchor.sh"
  [test-control-trace.sh]="hooks/multi-manager/control-trace.sh"
  [test-edit-write-claim-reconciler.sh]="hooks/multi-manager/edit-write-claim-reconciler.sh"
  [test-lib-hook-trace.sh]="hooks/multi-manager/lib-hook-trace.sh"
  [test-lib-identity.sh]="hooks/multi-manager/lib-identity.sh"
  [test-lib-mode-path.sh]="hooks/multi-manager/lib-mode-path.sh"
  [test-lib-registry-path.sh]="hooks/multi-manager/lib-registry-path.sh"
  [test-mode-interlock.sh]="hooks/multi-manager/mode-interlock.sh"
  [test-mode-tier.sh]="hooks/multi-manager/mode-interlock.sh"
  [test-orchestration-install.sh]="hooks/multi-manager/inbox-injection-precompact.sh hooks/multi-manager/inbox-injection.sh hooks/multi-manager/mode-interlock.sh hooks/multi-manager/read-guard.sh hooks/multi-manager/session-role-anchor.sh"
  [test-overlap-constraint.sh]="hooks/multi-manager/pre-dispatch-overlap.sh"
  [test-overlap-dispatch-e2e.sh]="hooks/multi-manager/pre-dispatch-overlap.sh"
  [test-parallel-previews.sh]="bin/sable-merge-gate bin/sable_gate_preview_lib.py bin/sable_gate_promote_lib.py bin/sable_gate_classify_lib.py bin/sable_gate_git_lib.py"
  [test-post-push-merge-notify.sh]="hooks/multi-manager/post-push-merge-notify.sh"
  [test-pre-dispatch-claim.sh]="hooks/multi-manager/pre-dispatch-claim.sh"
  [test-pre-dispatch-model-check.sh]="hooks/multi-manager/pre-dispatch-model-check.sh"
  [test-pre-dispatch-preempt.sh]="hooks/multi-manager/pre-dispatch-preempt.sh"
  [test-pre-dispatch-refresh.sh]="hooks/multi-manager/pre-dispatch-refresh.sh"
  [test-pre-push-rebase-test.sh]="hooks/multi-manager/pre-push-rebase-test.sh"
  [test-preview-kick.sh]="hooks/multi-manager/post-push-merge-notify.sh"
  [test-read-guard.sh]="hooks/multi-manager/read-guard.sh"
  [test-registry.sh]="hooks/multi-manager/lib-registry-path.sh"
  [test-sable-mode.sh]="hooks/multi-manager/lib-mode-path.sh"
  [test-sable-test.sh]="bin/sable-test"
  [test-session-role-anchor.sh]="hooks/multi-manager/session-role-anchor.sh"
  [test-tdd-evidence.sh]="hooks/tdd-evidence.sh"
  [test-tdd-gate.sh]="hooks/tdd-gate.sh"
  [test-tier-ssot-consumers.sh]="hooks/multi-manager/pre-push-rebase-test.sh"
  [test-tree-claim.sh]="hooks/multi-manager/tree-claim.sh"
)

# LIB_FANOUT: each shared lib path (hooks/multi-manager/lib-*.sh) -> every
# ALLOW suite whose covered file(s) source it, DIRECTLY OR TRANSITIVELY
# (lib-A sources lib-B is included — lib-identity.sh sources lib-mode-path.sh
# + lib-registry-path.sh, so every suite fanned out to lib-identity.sh is
# fanned out to those two as well). Completeness is MECHANICALLY ENFORCED by
# impact-manifest.sh's --check: a suite whose covered file starts sourcing a
# lib that lacks an entry here, or that is missing from an existing lib's
# list, fails the gate (SABLE-cmar4.2) — this table cannot silently drift out
# of sync with the real sourcing graph the way a hand-audited doc could.
declare -A LIB_FANOUT=(
  [hooks/multi-manager/lib-identity.sh]="test-pre-push-rebase-test.sh test-tier-ssot-consumers.sh test-mode-interlock.sh test-mode-tier.sh test-post-push-merge-notify.sh test-preview-kick.sh test-pre-dispatch-claim.sh test-pre-dispatch-model-check.sh test-pre-dispatch-preempt.sh test-pre-dispatch-refresh.sh test-overlap-constraint.sh test-overlap-dispatch-e2e.sh test-read-guard.sh test-orchestration-install.sh test-lib-identity.sh"
  [hooks/multi-manager/lib-hook-trace.sh]="test-post-push-merge-notify.sh test-preview-kick.sh test-tdd-gate.sh test-tdd-evidence.sh test-lib-hook-trace.sh"
  [hooks/multi-manager/lib-mode-path.sh]="test-pre-push-rebase-test.sh test-tier-ssot-consumers.sh test-mode-interlock.sh test-mode-tier.sh test-post-push-merge-notify.sh test-preview-kick.sh test-pre-dispatch-claim.sh test-pre-dispatch-model-check.sh test-pre-dispatch-preempt.sh test-pre-dispatch-refresh.sh test-overlap-constraint.sh test-overlap-dispatch-e2e.sh test-read-guard.sh test-orchestration-install.sh test-lib-identity.sh test-session-role-anchor.sh test-active-contracts-integration.sh test-lib-mode-path.sh test-sable-mode.sh"
  [hooks/multi-manager/lib-registry-path.sh]="test-pre-push-rebase-test.sh test-tier-ssot-consumers.sh test-mode-interlock.sh test-mode-tier.sh test-post-push-merge-notify.sh test-preview-kick.sh test-pre-dispatch-claim.sh test-pre-dispatch-model-check.sh test-pre-dispatch-preempt.sh test-pre-dispatch-refresh.sh test-overlap-constraint.sh test-overlap-dispatch-e2e.sh test-read-guard.sh test-orchestration-install.sh test-lib-identity.sh test-registry.sh test-lib-registry-path.sh"
  [hooks/multi-manager/lib-evidence-key.sh]="test-tdd-gate.sh test-tdd-evidence.sh test-sable-test.sh"
)

in_array() { local n="$1"; shift; local e; for e in "$@"; do [ "$e" = "$n" ] && return 0; done; return 1; }

# manifest_scan: the shared classification pass behind both --manifest and
# --check. Prints the same RUN/EXCLUDED/UNCLASSIFIED lines and totals either
# way; the only difference between the two modes is the caller's exit code
# decision below, which reads the MANIFEST_UNCL_NAMES / MANIFEST_STALE_NAMES
# arrays this populates rather than re-scanning.
manifest_scan() {
  local total=0 run=0 excl=0 uncl=0 f name
  MANIFEST_UNCL_NAMES=()
  MANIFEST_STALE_NAMES=()
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
      MANIFEST_UNCL_NAMES+=("$name")
    fi
  done
  echo "----------------------------------------------------------------------"
  echo "coverage manifest: $total total | $run run | $excl excluded | $uncl unclassified"
  # Drift guard: an ALLOW/EXCLUDE entry naming a file that no longer exists.
  for name in "${ALLOW[@]}" "${!EXCLUDE[@]}"; do
    if [ ! -f "$TESTDIR/$name" ]; then
      echo "::warning::stale run-set entry: $name is listed but not present in hooks/test/ (SABLE-7v3z)"
      MANIFEST_STALE_NAMES+=("$name")
    fi
  done
}

manifest() {
  manifest_scan
}

# check: fail-closed wrapper around manifest_scan (SABLE-lcevs). The detector
# in manifest_scan already finds UNCLASSIFIED suites and stale entries; this
# is the enforcement manifest_scan alone never had — a non-zero exit whenever
# either count is non-zero, which is what makes classification mandatory
# instead of a warning a branch can land while ignoring.
check() {
  manifest_scan
  local n_uncl="${#MANIFEST_UNCL_NAMES[@]}" n_stale="${#MANIFEST_STALE_NAMES[@]}"
  if [ "$n_uncl" -gt 0 ] || [ "$n_stale" -gt 0 ]; then
    echo "::error::shell-run-set --check: $n_uncl unclassified, $n_stale stale run-set entr(y/ies) — classify in ALLOW or EXCLUDE (with reason) before merge — this will not clear on re-run: the classification is missing, not flaky (SABLE-lcevs)"
    return 1
  fi
  echo "shell-run-set --check: 0 unclassified, 0 stale — gate is fail-closed and clean"
  return 0
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

# SABLE-cmar4.1: guard the CLI dispatch behind a sourced-vs-executed check so
# .github/ci/test-tiers.sh (the tier SSOT loader) can `. shell-run-set.sh` to
# reuse ALLOW/EXCLUDE as the merge_preview/full_snapshot tier membership
# (SABLE-7v3z's UNCLASSIFIED trap and the stale-drift guard stay defined right
# here, unduplicated) without triggering this dispatch on the SOURCING
# script's own argv. Direct execution (`bash shell-run-set.sh --run|--manifest`,
# as ci-verify.yml does) is unaffected — ${BASH_SOURCE[0]} == $0 only then.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1:-}" in
    --manifest) manifest ;;
    --check)    check ;;
    --run)      run_set ;;
    *) echo "usage: $0 --run | --manifest | --check" >&2; exit 2 ;;
  esac
fi
