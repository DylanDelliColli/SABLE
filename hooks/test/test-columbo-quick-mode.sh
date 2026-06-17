#!/usr/bin/env bash
# test-columbo-quick-mode.sh — text-contract for columbo's quick mode (SABLE-kwr.1).
# Quick-tier /sable-plan invokes /columbo --quick inline; this guards that the
# skill documents the non-interview, extend-existing-tests contract.
# Integration (a live /columbo --quick run producing a delta against an existing
# test) is covered by the SABLE-kwr epic acceptance scenario, not automatable here.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
SKILL="$REPO/skills/columbo/SKILL.md"
fails=0
has() { grep -q -- "$2" "$1" 2>/dev/null && printf '  ok  %s\n' "$3" || { printf '  FAIL %s\n' "$3"; fails=$((fails+1)); }; }

has "$SKILL" '--quick' 'modes list includes --quick'
has "$SKILL" 'Quick mode' 'has a Quick mode section'
has "$SKILL" 'NON-INTERVIEW' 'quick mode is non-interview'
has "$SKILL" 'EXTENDING existing test files' 'biases to extending existing tests'
has "$SKILL" 'no existing coverage' 'documents the new-test fallback'
has "$SKILL" 'inline' 'returns the spec inline (no beads/skeletons)'
# both test layers named within the quick-mode contract
has "$SKILL" 'unit+integration mandate' 'requires unit + integration layers'

if [ "$fails" -eq 0 ]; then printf 'PASS test-columbo-quick-mode\n'; else printf 'FAIL test-columbo-quick-mode (%d)\n' "$fails"; exit 1; fi
