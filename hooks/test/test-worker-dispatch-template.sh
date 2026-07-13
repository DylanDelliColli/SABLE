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
has  "a done worker refuses post-completion scope expansion"  "done worker takes no new work"
has  "instructs refusing unsolicited/misrouted instructions"  "REFUSE"

# ---------- SABLE-h853: scoped pre-push runs replace full-suite-per-worker ----------
# Operator-approved protocol change (2026-07-13): workers no longer run the
# full suite pre-push. TDD red-green stays scoped to the bead's own test
# files (unchanged); pre-push verification is a SCOPED run (bead's test
# files + tests importing the touched modules, coverage off, fail-fast on).
# The full suite runs exactly once, PRE-merge, as a merge-preview ci-verify
# GitHub Actions run (chuck-owned) — never a worker responsibility.

hasre "documents the pre-push run as SCOPED, not full-suite"        "scoped( pre-push)? (test )?run"
hasre "scoped run = bead's test files plus tests importing touched modules" "import(ing|s)? the modules|touch(ed|es) the diff|diff touch(ed|es)"
hasre "scoped run specifies coverage off"                            "coverage off|coverage[- ]?off|no.?cov"
hasre "scoped run specifies fail-fast on"                            "fail-fast on|fail.fast"
hasre "names the merge-preview ci-verify gate as full-suite authority" "merge-preview|ci-verify"
hasre "states workers do not run the full suite pre-push"            "workers? (do not|don't|never) run the full suite|not the full suite"
has  "cites SABLE-o9aa as the merge-preview ci-verify gate's tracking bead" "SABLE-o9aa"
has  "uses the exact-locked phrase 'the merge-preview ci-verify gate (SABLE-o9aa)'" "the merge-preview ci-verify gate (SABLE-o9aa)"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
