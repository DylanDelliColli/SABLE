#!/usr/bin/env bash
# pytest-impact-tier.sh — impact-tier runner invocation for the bin/ pytest
# half (SABLE-cmar4.3). Thin CI-facing entrypoint, kept consistent with the
# other .github/ci/*.sh scripts (shell-run-set.sh, test-tiers.sh) that this
# repo's tier SSOT (SABLE-cmar4.1) already uses. All decision logic — the
# pytest-testmon / pytest-impact union and the .testmondata cache-miss
# fallback — lives in bin/tier_selection.py (see its module docstring), so it
# is unit- and integration-tested directly as Python rather than re-derived
# in shell here.
#
# NOT wired into the ci-verify.yml full-suite gate: that gate's own selection
# invariant (SABLE-7v3z — no bin/ test can exist-but-not-run) only stays sound
# once the full-snapshot backstop (SABLE-jd5fj.5) exists to catch anything an
# impact-scoped run under-selects. Until then, this script is the impact-tier
# building block for a faster, narrower context (e.g. a future pre-push/local
# invocation) — ci-verify.yml only takes the .testmondata *cache wiring* from
# this bead, via --testmon-noselect, so the map stays warm without narrowing
# what actually runs there.
set -euo pipefail

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$CI_DIR/../.." && pwd)"

exec python3 "$REPO/bin/tier_selection.py" "$@"
