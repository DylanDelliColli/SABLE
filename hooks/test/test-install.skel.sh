#!/usr/bin/env bash
# Columbo skeleton — see SABLE-iw0 (v3 test-spec: install.sh consolidation,
# implementation bead SABLE-ppy).
#
# install.sh absorbs the cockpit layer (roles, agents.yaml, /plan + /execute
# skills, interlock + role-anchor hooks); bin/sable-cockpit-install becomes a
# deprecation stub; the v1 Zellij surface (sable-cockpit, sable-status,
# sable.kdl) is deleted outright; a CC>=2.1.172 version-floor warning lands.
#
# Worker: implement each todo case in house style against a SCRATCH HOME —
# reuse the redirection pattern and the count_marker/count_in_event/valid_json
# helpers from hooks/test/test-cockpit-install.sh (which shrinks to stub
# coverage once SABLE-ppy lands). Real install.sh execution, no filesystem
# mocking; claude version shim via a prepended PATH dir. Then rename
# test-install.skel.sh → test-install.sh.
#
# Run with:
#   bash hooks/test/test-install.skel.sh   (always exits 1 — skeleton)

set -uo pipefail

TODO=0
todo() { TODO=$((TODO+1)); echo "TODO (unimplemented case): $1"; }

# Why: the Lincoln layer is no longer optional — a partial install produces a
# cockpit that half-exists (skills present, interlock absent = advisory modes).
todo "fresh install lands the full Lincoln layer in scratch HOME"

# Why: duplicate hook entries double-fire every PreToolUse; the python merge
# proven in sable-cockpit-install add_hooks must survive the move.
todo "second run is idempotent: hooks registered exactly once"

# Why: install must merge, not overwrite — wiping a user's own hooks is the
# classic installer regression.
todo "pre-existing user settings survive the merge (non-clobber)"

# Why: v3 hard-requires nested subagents; a silent install on an old CC
# produces managers whose Agent tool never appears — undiagnosable unwarned.
todo "version floor: claude below 2.1.172 warns, at/above stays silent, absent does not crash"

# Why: a stub that silently succeeds forks two divergent install paths.
todo "deprecation stub: sable-cockpit-install refuses with pointer"

# Why: the deletion boundary is precise — Zellij viewer dies, sable-mode and
# the state machinery survive; overshooting the delete bricks the interlock.
todo "v1 Zellij surface is gone; mode machinery survives"

# Why: proves the installed copies are runnable artifacts — catches lost exec
# bits and path-relative sourcing breaks (lib-identity.sh sourced from
# installed locations).
todo "integration: installed hook copies pass their own suite"

echo
echo "=========================================="
echo "SKELETON: $TODO cases unimplemented — this is a contract, not a passing suite."
echo "See SABLE-iw0 for inputs/expected per case."
echo "=========================================="
exit 1
