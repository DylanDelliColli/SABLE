#!/usr/bin/env bash
# git-pre-push-attestation.sh — fail-closed git-bound push-gate proof check.

set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib-push-attestation.sh
. "$DIR/lib-push-attestation.sh"

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "sable: REFUSED — push verification did not run: git hook cannot resolve the worktree." >&2
  exit 1
}
if [ "${SABLE_ALLOW_UNVERIFIED_PUSH:-}" = "1" ]; then
  echo "sable: WARNING — allowing push via explicit SABLE_ALLOW_UNVERIFIED_PUSH=1; verification did not run." >&2
  exit 0
fi
if VERDICT="$(sable_consume_push_attestation "$REPO")"; then
  echo "sable: push gate proof accepted for $(git -C "$REPO" rev-parse --short HEAD): $VERDICT" >&2
  exit 0
fi
echo "sable: REFUSED — push verification did not run for the current HEAD, or its single-use proof is missing, stale, or mismatched. The remote ref was not updated. Run git push in the foreground so the configured PreToolUse gate can finish, then let this git-owned hook consume its proof. For an intentional emergency only, set SABLE_ALLOW_UNVERIFIED_PUSH=1; that degraded path is loud and auditable." >&2
exit 1
