#!/usr/bin/env bash
# Columbo skeleton — see SABLE-6zt (v3 test-spec: pre-dispatch family activation
# inversion; the overlap cases below are the net-new-file portion of that bead).
#
# pre-dispatch-overlap.sh currently has NO test coverage of any kind. This
# skeleton is the contract for its first suite, scoped to the v3 activation
# matrix (manager-subagent dispatches govern; worker/bare-id dispatches stand
# down).
#
# Worker: implement each todo case in house style — crafted PreToolUse:Agent
# hook-input JSON via python3, scratch claims/state dirs, a stub bd shim on
# PATH recording invocations, assert_allow/assert_deny helpers (model:
# hooks/test/test-pre-dispatch-claim.sh). Then rename this file
# test-pre-dispatch-overlap.skel.sh → test-pre-dispatch-overlap.sh.
#
# Run with:
#   bash hooks/test/test-pre-dispatch-overlap.skel.sh   (always exits 1 — skeleton)

set -uo pipefail

TODO=0
todo() { TODO=$((TODO+1)); echo "TODO (unimplemented case): $1"; }

# Why: overlap is the anti-collision gate for parallel workers — exactly the v3
# risk profile (one manager, many concurrent workers in one session).
todo "overlap: manager-subagent dispatch against an overlapping active claim warns/denies (NEW FILE)"

# Why: completes the activation matrix for the previously untested hook —
# agent_type=Explore and bare agent_id payloads must produce no overlap action.
todo "overlap: worker-type and bare-id dispatches stand down (NEW FILE)"

echo
echo "=========================================="
echo "SKELETON: $TODO cases unimplemented — this is a contract, not a passing suite."
echo "See SABLE-6zt for inputs/expected per case."
echo "=========================================="
exit 1
