#!/usr/bin/env bash
# test-worker-dispatch-template.sh — contract assertions for
# templates/worker-dispatch.md (SABLE-uz9.12). The native-spawn + pre-push-gate
# topology needs a "gate mode": the worker completes, runs unit+integration
# tests, rebases, then STOPS before push and returns evidence; the manager
# reviews and pushes. The self-push form survives as the non-gated default.
#
# Prose template — grep-based contract checks (no integration; the manager
# roles exercise the reference, and live worker runs cover the behavior).
#
# Run with:
#   bash hooks/test/test-worker-dispatch-template.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DOC="$REPO/templates/worker-dispatch.md"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
has() { if grep -qiF -- "$2" "$DOC" 2>/dev/null; then pass "$1"; else fail "$1" "missing: $2"; fi; }
hasre() { if grep -qiE -- "$2" "$DOC" 2>/dev/null; then pass "$1"; else fail "$1" "missing pattern: $2"; fi; }

[ -f "$DOC" ] || { echo "FAIL: $DOC missing"; exit 2; }

has  "documents a gate mode"                       "gate mode"
hasre "gate mode tells the worker to stop before push"   "stop[^.]*before[^.]*push|STOP.*do not push|do NOT push"
has  "gate mode emits the structured Worktree line"      "Worktree:"
hasre "gate mode returns the parked commit (SHA)"        "\bSHA\b|commit hash|parked commit"
hasre "gate mode returns test evidence not a PR URL"     "test (output|evidence)"
hasre "retains the self-push form for low-stakes lanes"  "self-push|PR URL"
hasre "says the manager performs the push on approval"   "manager (pushes|reviews|performs)|git -C"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
