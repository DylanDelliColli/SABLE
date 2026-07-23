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
#   --check-beads
#               LOCAL-ONLY freshness gate (SABLE-wqe2e): resolves every bead id
#               cited in an EXCLUDE entry's tracking tag against the real bd
#               store and fails when a [blocked-by: ...] entry's blockers are
#               ALL closed (the suite is a promotion candidate) or when any
#               cited id does not resolve at all (typo'd/deleted). NOT wired
#               into ci-verify — the clean room deliberately has no bd
#               (SABLE-59zu) — so it prints an explicit SKIP and exits 0 there.
#               See the EXCLUDE tracking-tag block below for the split of
#               responsibility between this mode and --check.
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
  test-ci-bd-coverage-gap.sh
  test-columbo-quick-mode.sh
  test-concurrent-sessions.sh
  test-control-trace.sh
  test-coverage-floor-gate.sh
  test-dep-merge-state.sh
  test-doctor-snapshot-staleness.sh
  test-edit-write-claim-reconciler.sh
  test-event-pair.sh
  test-full-ingestion.sh
  test-identity-hermeticity.sh
  test-impact-manifest.sh
  test-impact-selection.sh
  test-impact-tier-serialization.sh
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
  test-notes-clobber-guard.sh
  test-optimistic-promotion.sh
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
  test-reconcile-rebase-copy.sh
  test-registry.sh
  test-require-all.sh
  test-role-card-install.sh
  test-role.sh
  test-sable-bin-install.sh
  test-sable-claim.sh
  test-sable-clean-room-verify.sh
  test-sable-clean-room-verify-integration.sh
  test-sable-cli.sh
  test-sable-contained.sh
  test-sable-contract.sh
  test-sable-discover.sh
  test-sable-launch.sh
  test-sable-mode.sh
  test-sable-msg.sh
  test-sable-onboarding-skill.sh
  test-sable-plan-tiers.sh
  test-sable-skills.sh
  test-sable-test.sh
  test-sable-worker-status.sh
  test-script-dir-symlink.sh
  test-session-role-anchor.sh
  test-shell-run-set-strict.sh
  test-sherlock-research.sh
  test-snapshot-freeze.sh
  test-spine-pinning.sh
  test-staged-planning-docs.sh
  test-stash-worktree-guard.sh
  test-tarzan-optimus-accept-contract.sh
  test-tdd-evidence.sh
  test-tdd-gate.sh
  test-thesis-docs.sh
  test-tier-budget-bead.sh
  test-tier-red-capture.sh
  test-tier-ssot.sh
  test-tier-ssot-consumers.sh
  test-tmux-roles.sh
  test-tree-claim.sh
  test-worker-dispatch-template.sh
  test-worker-flag-done.sh
  test-worktree-isolation.sh
  test-worktree-placement-guard.sh
)

# --- Excluded from the run-set, each WITH reason + tracking tag. ---------------
# Widen ALLOW (move an entry here into ALLOW) as each blocked-by entry lands.
#
# THAT WIDENING IS NOT AUTOMATIC, AND FORGETTING IT IS ITS OWN FALSE-GREEN
# (SABLE-8onsf). --check's staleness guard only catches an entry naming a file
# that no longer EXISTS; it cannot see an entry whose tracking bead is closed.
# test-sable-msg.sh sat here for ~2 weeks citing SABLE-cncs after cncs had been
# closed as verified-fixed with an 11/11 green run, so the fleet's delivery-
# verification suite was ungated the entire time -- and when it later went red
# for an unrelated reason (SABLE-4nr0q's ambient-identity leak) nothing caught
# it.
#
# TRACKING TAG, REQUIRED IN EVERY REASON (SABLE-wqe2e). Prose is free-form, but
# each reason must carry EXACTLY ONE machine-readable tag:
#
#   [blocked-by: <id> ...]  TEMPORARY. This suite would be in ALLOW if those
#                           beads were fixed. When ALL of them close, the suite
#                           must be promoted — --check-beads FAILS until it is.
#   [permanent: <id> ...]   STRUCTURAL. The exclusion follows from how the
#                           clean room is built (no bd, no ~/.claude install)
#                           or from what the suite is, not from an open defect.
#                           The cited beads are CONTEXT; their closing changes
#                           nothing, so --check-beads does not flag them.
#
# The split exists because a naive "all cited beads closed => promote" rule
# fires on 8 of the entries below on day one: they cite SABLE-59zu, which is
# the CLOSED bead that DOCUMENTS the clean room's hermeticity, not a defect
# awaiting a fix. A gate that is wrong 8 times out of 10 gets suppressed, which
# is the same "train everyone to ignore it" failure this file already guards
# against elsewhere. Tagging makes the author state which kind of citation it
# is, once, at the point they know.
#
# WHO ENFORCES WHAT: --check (runs in the bd-less clean room) enforces only
# WELL-FORMEDNESS — every reason carries exactly one tag with parseable bead
# ids. --check-beads (local, where bd exists) enforces FRESHNESS against the
# real store. Neither can do the other's job in the other's environment.
#
# THE RESIDUAL HOLE, stated rather than papered over: nothing stops an author
# from writing [permanent: ...] on a genuinely temporary exclusion. That is a
# deliberate-miscategorization risk, not the accidental-drift risk this bead
# closes; drift is what actually happened to test-sable-msg.sh.
declare -A EXCLUDE=(
  [test-install.sh]="needs the ~/.claude SABLE install; clean-room has none [permanent: SABLE-59zu]"
  [test-install-guard.sh]="needs the ~/.claude SABLE install (real bd on PATH) for its --from-here/main-checkout proceed cases; clean-room has none [permanent: SABLE-59zu]"
  [test-install-agent-defs.sh]="needs the ~/.claude SABLE install [permanent: SABLE-59zu]"
  [test-install-version-floor.sh]="needs the ~/.claude SABLE install [permanent: SABLE-59zu]"
  [test-install-multi-manager.sh]="vacuous without bd — prints 'SKIP: bd not on PATH' and exits 0 [permanent: SABLE-59zu SABLE-7v3z]"
  [test-quickstart-project.sh]="needs the ~/.claude SABLE install (bd, sable-doctor) for its E2E bootstrap-flow cases; clean-room has none [permanent: SABLE-59zu SABLE-vivm]"
  [test-notes-clobber-guard-e2e.sh]="real-bd-only by construction — its whole claim is that CONTENT survives in a real bead store, and its negative control needs a real destructive bd write; in the clean room it would SKIP and count green. Run it locally against real bd; the decision logic is covered fail-closed by test-notes-clobber-guard.sh, which IS in the run-set. [permanent: SABLE-sm269 SABLE-59zu]"
  [test-tmux-e2e.sh]="vacuous without bd — prints 'SKIP: bd not installed' and exits 0 [permanent: SABLE-59zu]"
  [test-landing-pair-gate.sh]="real-bd-only by construction — its whole claim is that a MUST-LAND-TOGETHER pairing declared in real bd metadata is read back mechanically by promote(); prints 'SKIP: bd not on PATH' and exits 0 in the clean room [permanent: SABLE-59zu]"
  [test-seat-sighting.sh]="real-bd-only by construction — its whole claim is that a filed sighting's DEFERRED status and ready-pool absence/presence are read back from a real bd store; prints 'SKIP: bd not on PATH' and exits 0 in the clean room [permanent: SABLE-59zu]"
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
  [test-ci-bd-coverage-gap.sh]=".github/ci/shell-run-set.sh hooks/test/test-dep-merge-state.sh hooks/test/test-overlap-dispatch-e2e.sh hooks/test/lib-require-all.sh"
  [test-control-trace.sh]="hooks/multi-manager/control-trace.sh"
  [test-dep-merge-state.sh]="bin/sable-dep-check bin/sable-spawn-worker"
  [test-doctor-snapshot-staleness.sh]="bin/sable-doctor bin/sable-bin-install install.sh"
  [test-edit-write-claim-reconciler.sh]="hooks/multi-manager/edit-write-claim-reconciler.sh"
  [test-impact-tier-serialization.sh]="bin/sable_gate_promote_lib.py"
  [test-landing-pair-gate.sh]="bin/sable_gate_promote_lib.py bin/sable_gate_classify_lib.py bin/sable-merge-gate"
  [test-lib-hook-trace.sh]="hooks/multi-manager/lib-hook-trace.sh"
  [test-lib-identity.sh]="hooks/multi-manager/lib-identity.sh"
  [test-lib-mode-path.sh]="hooks/multi-manager/lib-mode-path.sh"
  [test-lib-registry-path.sh]="hooks/multi-manager/lib-registry-path.sh"
  [test-mode-interlock.sh]="hooks/multi-manager/mode-interlock.sh"
  [test-mode-tier.sh]="hooks/multi-manager/mode-interlock.sh"
  [test-notes-clobber-guard.sh]="hooks/multi-manager/notes-clobber-guard.sh"
  [test-notes-clobber-guard-e2e.sh]="hooks/multi-manager/notes-clobber-guard.sh"
  [test-orchestration-install.sh]="hooks/multi-manager/inbox-injection-precompact.sh hooks/multi-manager/inbox-injection.sh hooks/multi-manager/mode-interlock.sh hooks/multi-manager/read-guard.sh hooks/multi-manager/session-role-anchor.sh"
  [test-optimistic-promotion.sh]="bin/sable-merge-gate bin/sable_footprint_lib.py bin/sable_gate_promote_lib.py bin/sable_gate_preview_lib.py bin/sable_gate_classify_lib.py bin/sable_gate_git_lib.py"
  [test-overlap-constraint.sh]="hooks/multi-manager/pre-dispatch-overlap.sh"
  [test-overlap-dispatch-e2e.sh]="hooks/multi-manager/pre-dispatch-overlap.sh"
  [test-parallel-previews.sh]="bin/sable-merge-gate bin/sable_gate_preview_lib.py bin/sable_gate_promote_lib.py bin/sable_gate_classify_lib.py bin/sable_gate_git_lib.py"
  [test-post-push-merge-notify.sh]="hooks/multi-manager/post-push-merge-notify.sh"
  [test-pre-dispatch-claim.sh]="hooks/multi-manager/pre-dispatch-claim.sh bin/sable-dep-check"
  [test-pre-dispatch-model-check.sh]="hooks/multi-manager/pre-dispatch-model-check.sh"
  [test-pre-dispatch-preempt.sh]="hooks/multi-manager/pre-dispatch-preempt.sh"
  [test-pre-dispatch-refresh.sh]="hooks/multi-manager/pre-dispatch-refresh.sh"
  [test-pre-push-rebase-test.sh]="hooks/multi-manager/pre-push-rebase-test.sh"
  [test-preview-kick.sh]="hooks/multi-manager/post-push-merge-notify.sh"
  [test-read-guard.sh]="hooks/multi-manager/read-guard.sh"
  [test-registry.sh]="hooks/multi-manager/lib-registry-path.sh"
  [test-require-all.sh]="hooks/test/lib-require-all.sh"
  [test-role-card-install.sh]="bin/sable-orchestration-install templates/multi-manager/roles/lincoln.md templates/multi-manager/roles/optimus.md templates/multi-manager/roles/tarzan.md templates/multi-manager/roles/chuck.md"
  [test-sable-contained.sh]="bin/sable-contained"
  [test-sable-mode.sh]="hooks/multi-manager/lib-mode-path.sh"
  [test-sable-msg.sh]="bin/sable-msg bin/sable_pane_lib.py"
  [test-sable-test.sh]="bin/sable-test"
  [test-sable-worker-status.sh]="bin/sable-worker-status bin/sable_pane_lib.py"
  [test-seat-sighting.sh]="bin/sable-msg hooks/multi-manager/seat-sighting-gate.sh"
  [test-session-role-anchor.sh]="hooks/multi-manager/session-role-anchor.sh"
  [test-snapshot-freeze.sh]="bin/sable-snapshot bin/sable_snapshot_lib.py bin/sable_gate_promote_lib.py bin/sable_gate_classify_lib.py"
  [test-tarzan-optimus-accept-contract.sh]="templates/multi-manager/roles/tarzan.md templates/multi-manager/roles/optimus.md"
  [test-tdd-evidence.sh]="hooks/tdd-evidence.sh"
  [test-tdd-gate.sh]="hooks/tdd-gate.sh"
  [test-tier-budget-bead.sh]="bin/sable_gate_budget_lib.py bin/sable_gate_promote_lib.py bin/sable-merge-gate"
  [test-tier-red-capture.sh]="bin/sable_gate_promote_lib.py hooks/test/lib-require-all.sh"
  [test-tier-ssot-consumers.sh]="hooks/multi-manager/pre-push-rebase-test.sh"
  [test-tmux-roles.sh]="templates/multi-manager/roles/lincoln.md templates/multi-manager/roles/optimus.md templates/multi-manager/roles/tarzan.md templates/multi-manager/roles/chuck.md templates/multi-manager/agents.yaml"
  [test-tree-claim.sh]="hooks/multi-manager/tree-claim.sh hooks/multi-manager/tree-claim-impl.sh"
  [test-worktree-placement-guard.sh]="hooks/multi-manager/worktree-placement-guard.sh"
)

# --- Iron-rule real-bd suites (SABLE-jd5fj.16) ------------------------------
# Suites whose DEFINING coverage is against a real bd (no mocks/stubs — see
# each suite's own header) and that therefore self-skip a substantial leg —
# for test-overlap-dispatch-e2e.sh, the WHOLE suite — in the ci-verify clean
# room (SABLE-59zu ships no bd). jd5fj.13 established that chuck's local
# combined-tree impact tier is their sole real executor; that tier's
# selection (.github/ci/impact-manifest.sh's select_impacted()) draws ONLY
# from ALLOW, so EXCLUDING these suites here would silently delete their only
# real executor too, not just their (already-vacuous) CI leg. They MUST stay
# in ALLOW.
#
# What they must NOT do is print a clean green summary that reads the same
# as having actually run: check_loud_skip() (below) mechanically enforces
# that every suite named here (a) is present in ALLOW and (b) carries a loud,
# non-zero "Skipped: $N" marker in its own summary output for the bd-absent
# path. Widen this list by hand when a new suite earns the same shape —
# auto-detecting via `grep 'command -v bd'` false-positived on ~15 unrelated
# ALLOW suites that merely reference bd in passing (e.g. a single negative-
# path assertion), not suites whose entire value proposition is real-bd
# coverage.
IRON_RULE_REALBD_SUITES=(
  test-dep-merge-state.sh
  test-overlap-dispatch-e2e.sh
)

# check_loud_skip: populates LOUD_SKIP_BAD with "<suite>: <cause>" lines for
# any IRON_RULE_REALBD_SUITES entry that (a) is not actually in ALLOW, or (b)
# has no "Skipped: $" marker in its own file — i.e. could exit its bd-absent
# branch without ever printing a summary that distinguishes "ran everything"
# from "skipped the real-bd leg". Pure grep, no bd needed, so this runs in
# the clean room same as check_exclude_tags.
check_loud_skip() {
  LOUD_SKIP_BAD=()
  local name
  for name in "${IRON_RULE_REALBD_SUITES[@]}"; do
    if ! in_array "$name" "${ALLOW[@]}"; then
      LOUD_SKIP_BAD+=("$name: registered in IRON_RULE_REALBD_SUITES but missing from ALLOW — EXCLUDE would silently delete its only real executor too (chuck's local impact tier draws suites from ALLOW only, SABLE-jd5fj.16)")
      continue
    fi
    if [ ! -f "$TESTDIR/$name" ]; then
      LOUD_SKIP_BAD+=("$name: registered in IRON_RULE_REALBD_SUITES but missing from hooks/test/")
      continue
    fi
    if ! grep -q 'Skipped: \$' "$TESTDIR/$name"; then
      LOUD_SKIP_BAD+=("$name: no loud 'Skipped: \$N' marker in its own summary — a self-skipped real-bd leg (bd absent, SABLE-59zu clean room) would print a clean summary indistinguishable from a full run (SABLE-jd5fj.16)")
    fi
  done
}

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

# --- EXCLUDE tracking-tag machinery (SABLE-wqe2e) ----------------------------

# parse_exclude_tag <reason>: on success sets TAG_KIND (blocked-by|permanent)
# and TAG_IDS (array of bead ids) and returns 0. Returns 1 — and sets
# TAG_ERR to a human-readable cause — when the reason carries zero tags, more
# than one, an empty id list, or an id that is not a bead-id shape. Pure bash
# (BASH_REMATCH), so --check can call it in the bd-less clean room.
parse_exclude_tag() {
  local reason="$1" rest="$1" count=0 raw="" id
  TAG_KIND=""; TAG_IDS=(); TAG_ERR=""
  while [[ "$rest" =~ \[(blocked-by|permanent):([^]]*)\] ]]; do
    count=$((count+1))
    TAG_KIND="${BASH_REMATCH[1]}"
    raw="${BASH_REMATCH[2]}"
    rest="${rest#*"${BASH_REMATCH[0]}"}"
  done
  if [ "$count" -eq 0 ]; then
    TAG_ERR="no tracking tag — every EXCLUDE reason needs exactly one [blocked-by: <id> ...] or [permanent: <id> ...]"
    return 1
  fi
  if [ "$count" -gt 1 ]; then
    TAG_ERR="$count tracking tags — exactly one is allowed, so the entry states ONE promotion rule"
    return 1
  fi
  read -r -a TAG_IDS <<< "$raw"
  if [ "${#TAG_IDS[@]}" -eq 0 ]; then
    TAG_ERR="[$TAG_KIND:] tag cites no bead id"
    return 1
  fi
  for id in "${TAG_IDS[@]}"; do
    if ! [[ "$id" =~ ^[A-Za-z][A-Za-z0-9-]*-[A-Za-z0-9.]+$ ]]; then
      TAG_ERR="'$id' is not a bead-id shape (expected e.g. SABLE-wqe2e or market-brief-package-ssd8)"
      return 1
    fi
  done
  return 0
}

# check_exclude_tags: well-formedness pass over the whole EXCLUDE table.
# Populates EXCLUDE_TAG_BAD with "<suite>: <cause>" lines. Needs no bd, so
# this is the half of SABLE-wqe2e that the clean room CAN enforce.
check_exclude_tags() {
  EXCLUDE_TAG_BAD=()
  local name
  for name in "${!EXCLUDE[@]}"; do
    parse_exclude_tag "${EXCLUDE[$name]}" || EXCLUDE_TAG_BAD+=("$name: $TAG_ERR")
  done
}

# bead_status <id>: prints the bead's status, or the literal __unresolved__
# when bd cannot resolve the id at all (typo'd, deleted, wrong tracker).
bead_status() {
  bd show "$1" --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("__unresolved__"); raise SystemExit
if isinstance(d, dict):
    d = [d]
print((d[0].get("status") if d else None) or "__unresolved__")
'
}

# check_beads: the FRESHNESS gate. Local-only by design — the ci-verify clean
# room has no bd (SABLE-59zu), so there it prints an explicit SKIP and exits 0
# rather than silently passing. A silent no-op here would reproduce exactly the
# false-green SABLE-wqe2e closes.
check_beads() {
  if ! command -v bd >/dev/null 2>&1; then
    echo "SKIP: bd not on PATH — shell-run-set --check-beads cannot resolve EXCLUDE tracking beads here."
    echo "SKIP: this is expected in the ci-verify clean room (SABLE-59zu); freshness is a LOCAL gate and --check still enforces tag well-formedness (SABLE-wqe2e)."
    return 0
  fi

  check_exclude_tags
  if [ "${#EXCLUDE_TAG_BAD[@]}" -gt 0 ]; then
    echo "::error::shell-run-set --check-beads: ${#EXCLUDE_TAG_BAD[@]} malformed EXCLUDE tracking tag(s):"
    printf '  - %s\n' "${EXCLUDE_TAG_BAD[@]}"
    return 1
  fi

  local name id status all_closed closed_ids
  local promote=() unresolved=()
  for name in "${!EXCLUDE[@]}"; do
    parse_exclude_tag "${EXCLUDE[$name]}"
    local kind="$TAG_KIND"
    local ids=("${TAG_IDS[@]}")
    all_closed=1; closed_ids=""
    for id in "${ids[@]}"; do
      status="$(bead_status "$id")"
      case "$status" in
        __unresolved__)
          unresolved+=("$name — cites $id, which does not resolve in the bead store")
          all_closed=0 ;;
        closed)
          closed_ids="$closed_ids $id" ;;
        *)
          all_closed=0 ;;
      esac
    done
    if [ "$kind" = "blocked-by" ] && [ "$all_closed" -eq 1 ]; then
      promote+=("$name — every blocker is CLOSED:$closed_ids")
    fi
  done

  local rc=0
  if [ "${#unresolved[@]}" -gt 0 ]; then
    echo "::error::shell-run-set --check-beads: ${#unresolved[@]} EXCLUDE entr(y/ies) cite an UNRESOLVABLE bead id — the reason is unfalsifiable, so the exclusion is unaudited (SABLE-wqe2e):"
    printf '  - %s\n' "${unresolved[@]}"
    rc=1
  fi
  if [ "${#promote[@]}" -gt 0 ]; then
    echo "::error::shell-run-set --check-beads: ${#promote[@]} EXCLUDE entr(y/ies) are PROMOTION CANDIDATES — the blockers they cite are all closed, so the suite is excluded on the authority of a defect that no longer exists (SABLE-wqe2e):"
    printf '  - %s\n' "${promote[@]}"
    echo "::error::remedy: run each named suite; if green, MOVE it from EXCLUDE into ALLOW (and give it a COVERS entry). This will not clear on re-run — the exclusion is stale, not flaky."
    rc=1
  fi
  [ "$rc" -eq 0 ] && echo "shell-run-set --check-beads: ${#EXCLUDE[@]} exclusion(s) checked — 0 stale blockers, 0 unresolvable bead ids"
  return "$rc"
}

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
  # SABLE-wqe2e: the bd-free half of the exclusion-rot guard. --check runs in
  # the clean room and cannot ask whether a tracking bead is CLOSED, but it CAN
  # insist every exclusion states its promotion rule in a parseable form — which
  # is what makes the local --check-beads freshness gate possible at all.
  check_exclude_tags
  if [ "${#EXCLUDE_TAG_BAD[@]}" -gt 0 ]; then
    echo "::error::shell-run-set --check: ${#EXCLUDE_TAG_BAD[@]} EXCLUDE entr(y/ies) lack a well-formed tracking tag — each reason needs exactly one [blocked-by: <id> ...] or [permanent: <id> ...] so the local --check-beads gate can tell a temporary exclusion from a structural one (SABLE-wqe2e). This will not clear on re-run: the tag is missing, not flaky."
    printf '  - %s\n' "${EXCLUDE_TAG_BAD[@]}"
    return 1
  fi
  local n_uncl="${#MANIFEST_UNCL_NAMES[@]}" n_stale="${#MANIFEST_STALE_NAMES[@]}"
  if [ "$n_uncl" -gt 0 ] || [ "$n_stale" -gt 0 ]; then
    echo "::error::shell-run-set --check: $n_uncl unclassified, $n_stale stale run-set entr(y/ies) — classify in ALLOW or EXCLUDE (with reason) before merge — this will not clear on re-run: the classification is missing, not flaky (SABLE-lcevs)"
    return 1
  fi
  # SABLE-jd5fj.16: the iron-rule real-bd suites' loud-skip contract. No bd
  # needed (pure grep), so this runs in the clean room right alongside the
  # tag check above.
  check_loud_skip
  if [ "${#LOUD_SKIP_BAD[@]}" -gt 0 ]; then
    echo "::error::shell-run-set --check: ${#LOUD_SKIP_BAD[@]} iron-rule real-bd suite(s) fail the loud-skip contract — a suite that self-skips its real-bd leg must never print a clean green summary indistinguishable from having run it (SABLE-jd5fj.16). This will not clear on re-run: the marker is missing, not flaky."
    printf '  - %s\n' "${LOUD_SKIP_BAD[@]}"
    return 1
  fi
  echo "shell-run-set --check: 0 unclassified, 0 stale, 0 loud-skip violations — gate is fail-closed and clean"
  return 0
}

# check-loud-skip: standalone CLI wrapper around check_loud_skip, for direct
# invocation (humans, or tests that want the real production registry
# checked in a fresh subprocess rather than sourced into their own state).
# check() already runs this same check as part of the enforced gate.
check_loud_skip_cli() {
  check_loud_skip
  if [ "${#LOUD_SKIP_BAD[@]}" -gt 0 ]; then
    echo "::error::shell-run-set --check-loud-skip: ${#LOUD_SKIP_BAD[@]} iron-rule real-bd suite(s) fail the loud-skip contract (SABLE-jd5fj.16):"
    printf '  - %s\n' "${LOUD_SKIP_BAD[@]}"
    return 1
  fi
  echo "shell-run-set --check-loud-skip: ${#IRON_RULE_REALBD_SUITES[@]} iron-rule suite(s) checked — all present in ALLOW, all carry a loud Skipped marker"
  return 0
}

run_set() {
  local failed=() skipped_realbd=() name rc out_file n_skip
  # SABLE-jd5fj.16: the top-level rollup below used to report only pass/fail
  # — "ci-verify shell run-set: all N suites GREEN" says nothing about a
  # suite that passed BY SKIPPING its real-bd leg. That is exactly how the
  # gap stayed invisible: each suite's own "Skipped: $N" line scrolled by in
  # its own ::group::, but nothing pulled it into the summary a reader
  # actually looks at. Tee each iron-rule suite's output to a scratch file so
  # it can be grepped for that marker AFTER streaming (unchanged) to the log.
  out_file="$(mktemp)"
  for name in "${ALLOW[@]}"; do
    if [ ! -f "$TESTDIR/$name" ]; then
      echo "::error::run-set names $name but it is missing from hooks/test/"; failed+=("$name (missing)"); continue
    fi
    echo "::group::$name"
    bash "$TESTDIR/$name" 2>&1 | tee "$out_file"
    rc=${PIPESTATUS[0]}
    echo "::endgroup::"
    [ $rc -eq 0 ] || failed+=("$name (rc=$rc)")
    if in_array "$name" "${IRON_RULE_REALBD_SUITES[@]}"; then
      n_skip=$(grep -oE 'Skipped: [0-9]+' "$out_file" | tail -1 | grep -oE '[0-9]+' || true)
      if [ -n "${n_skip:-}" ] && [ "$n_skip" -gt 0 ]; then
        skipped_realbd+=("$name (Skipped: $n_skip real-bd subtest(s) — bd absent in this clean room, SABLE-59zu; runs for real only at chuck's local impact tier, SABLE-jd5fj.16)")
      fi
    fi
  done
  rm -f "$out_file"
  echo "======================================================================"
  if [ ${#failed[@]} -eq 0 ]; then
    echo "ci-verify shell run-set: all ${#ALLOW[@]} suites GREEN"
  else
    echo "ci-verify shell run-set: ${#failed[@]} of ${#ALLOW[@]} suites RED:"
    printf '  - %s\n' "${failed[@]}"
  fi
  if [ ${#skipped_realbd[@]} -gt 0 ]; then
    echo "::warning::ci-verify shell run-set: ${#skipped_realbd[@]} iron-rule suite(s) skipped their real-bd leg here — NOT counted as coverage, only as a non-failing exit (SABLE-jd5fj.16):"
    printf '  - %s\n' "${skipped_realbd[@]}"
  fi
  [ ${#failed[@]} -eq 0 ]
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
    --manifest)        manifest ;;
    --check)           check ;;
    --check-beads)     check_beads ;;
    --check-loud-skip) check_loud_skip_cli ;;
    --run)             run_set ;;
    *) echo "usage: $0 --run | --manifest | --check | --check-beads | --check-loud-skip" >&2; exit 2 ;;
  esac
fi
