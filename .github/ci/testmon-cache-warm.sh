#!/usr/bin/env bash
# testmon-cache-warm.sh — best-effort .testmondata cache warming for
# ci-verify (SABLE-cmar4.3 second revise). Deliberately separate from the
# gate's own correctness-verifying pytest step (ci-verify.yml "pytest — full
# bin/ suite") so that pytest-testmon's own internal defect can never fail
# the gate. All the decision logic — running the full suite with
# --testmon-noselect and classifying the known pytest-testmon
# extensionless-file crash as a tolerated non-failure — lives in
# bin/tier_selection.py's run_cache_warm()/classify_cache_warm_outcome(), so
# it is unit- and integration-tested directly as Python rather than
# re-derived in shell here. See that module's comments for the full defect
# writeup.
set -euo pipefail

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$CI_DIR/../.." && pwd)"

exec python3 "$REPO/bin/tier_selection.py" --cache-warm
