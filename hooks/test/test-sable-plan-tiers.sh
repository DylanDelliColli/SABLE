#!/usr/bin/env bash
# test-sable-plan-tiers.sh — text-contract for /sable-plan self-sizing (SABLE-kwr.2).
# Guards that the skill documents the tier proposal, the quick lane (inline
# /columbo --quick + single gate + one-way escalation), and keeps the full
# five-substage flow. Integration (a live quick plan producing one bead through
# the single gate) is the SABLE-kwr epic acceptance scenario, not automatable here.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
SKILL="$REPO/skills/sable-plan/SKILL.md"
fails=0
has() { grep -q -- "$2" "$1" 2>/dev/null && printf '  ok  %s\n' "$3" || { printf '  FAIL %s\n' "$3"; fails=$((fails+1)); }; }

has "$SKILL" 'self-sizing' 'frames planning as self-sizing'
has "$SKILL" 'Size the ask' 'has the tier-proposal step'
has "$SKILL" 'AskUserQuestion' 'proposes the tier (human confirms)'
has "$SKILL" 'Quick tier' 'has a Quick tier section'
has "$SKILL" 'Full tier' 'has a Full tier section'
has "$SKILL" 'set planning --tier quick' 'quick lane drives tier into mode-state'
has "$SKILL" '/columbo --quick' 'quick lane runs columbo inline for tests'
has "$SKILL" 'consolidated gate' 'quick lane has a single consolidated gate'
has "$SKILL" 'Escalation' 'documents the one-way quick->full escalation'
# full flow preserved
for s in FRAMING RESEARCH ARCHITECTURE TEST-STRATEGY DECOMPOSITION; do
  has "$SKILL" "$s" "full flow still names substage $s"
done

if [ "$fails" -eq 0 ]; then printf 'PASS test-sable-plan-tiers\n'; else printf 'FAIL test-sable-plan-tiers (%d)\n' "$fails"; exit 1; fi
