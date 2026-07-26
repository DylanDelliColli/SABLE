#!/usr/bin/env bash
# preview-already-verified.sh — dedup guard for SABLE-r3i6: sable-merge-gate
# promotes a merge-preview commit BYTE-IDENTICAL onto tmux-only, so the
# tmux-only push re-verifies a SHA that already passed ci-verify on its
# ci-verify/<bead>-<sha7> preview ref. This script answers "has this exact
# SHA already completed successfully as a ci-verify/** preview run?" so the
# tmux-only job can short-circuit instead of re-running the full suite.
#
# Usage: preview-already-verified.sh <sha>
#   prints 'verified <head_branch>' and exits 0 if a completed, successful
#   ci-verify run exists for <sha> on a ci-verify/** branch.
#   prints 'unverified' and exits 1 otherwise — including on any query
#   failure (fail OPEN: never skip the suite on uncertainty).
set -uo pipefail

SHA="${1:-}"
if [ -z "$SHA" ]; then
  echo "usage: $0 <sha>" >&2
  echo "unverified"
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  # SABLE-apt5a: without this check, a missing gh fell through into the
  # generic "any gh failure -> unverified" branch below, indistinguishable
  # in the job log from a real API error. Name it here so a missing gh is
  # loud in the log instead of silently and permanently defeating the
  # SABLE-r3i6 dedup optimization. Still fail-open (same stdout/exit-code
  # contract as every other unverified case) -- naming it does not change
  # what the caller does.
  echo "gh not installed -- dedup guard cannot query the Actions API, fail-open (SABLE-apt5a)" >&2
  echo "unverified"
  exit 1
fi

REPO_ARG="${GITHUB_REPOSITORY:-:owner/:repo}"

RESPONSE="$(gh api "repos/${REPO_ARG}/actions/runs?head_sha=${SHA}&per_page=100" 2>/dev/null)"
RC=$?

if [ $RC -ne 0 ] || [ -z "$RESPONSE" ]; then
  echo "unverified"
  exit 1
fi

MATCH_REF="$(printf '%s' "$RESPONSE" | jq -r --arg sha "$SHA" '
  [ .workflow_runs[]?
    | select(.head_sha == $sha)
    | select(.name == "ci-verify")
    | select(.head_branch | startswith("ci-verify/"))
    | select(.status == "completed" and .conclusion == "success")
  ] | first | .head_branch // empty
' 2>/dev/null)"
JQ_RC=$?

if [ $JQ_RC -ne 0 ]; then
  echo "unverified"
  exit 1
fi

if [ -n "$MATCH_REF" ]; then
  echo "verified $MATCH_REF"
  exit 0
fi

echo "unverified"
exit 1
