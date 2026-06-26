#!/usr/bin/env bash
# test-sable-discover.sh — Discovery mode skill + fan-out (SABLE-7v1r.2).
#
# Two legs:
#   1. Golden-fixture exercise of the deterministic fan-out (sable-discover-emit):
#      a 3-candidate triage (one no-go) writes 2 charters with epic linkage + a
#      decision record carrying every verdict and the no-go rationale verbatim;
#      the no-go gets NO charter.
#   2. Doc-lint that skills/sable-discover/SKILL.md expresses the binding contract
#      (four beats, comparative-default + escalation, business-lens guardrail,
#      office-hours reuse, fan-out helper, epic-intention shells, no impl beads).
#
# Run with:  bash hooks/test/test-sable-discover.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
EMIT="$REPO/bin/sable-discover-emit"
SKILL="$REPO/skills/sable-discover/SKILL.md"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; }
has()  { if grep -q "$2" "$SKILL"; then pass "SKILL $3"; else fail "SKILL $3 (missing: $2)"; fi; }

# --- leg 1: golden-fixture fan-out -----------------------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export SABLE_CHARTERS_DIR="$TMP/charters"
FIX="$TMP/triage.json"
cat > "$FIX" <<'JSON'
{"session":"2026-06-26 triage","title":"Q3","candidates":[
  {"title":"Real-Time Alerts","verdict":"go","rationale":"clear demand","epic_intention":"SABLE-e1","charter":{"target_user_and_wedge":"ops wedge"}},
  {"title":"Bulk Export","verdict":"reshape","rationale":"scope wide","epic_intention":"SABLE-e2","charter":{"problem_statement":"manual"}},
  {"title":"Themes","verdict":"no-go","rationale":"zero pull, high cost"}]}
JSON
python3 "$EMIT" --json "$FIX" >/dev/null 2>"$TMP/err" || { fail "sable-discover-emit exited nonzero"; cat "$TMP/err"; }

CDIR="$SABLE_CHARTERS_DIR"
[ -f "$CDIR/real-time-alerts.md" ] && pass "survivor charter written (go)" || fail "missing go survivor charter"
[ -f "$CDIR/bulk-export.md" ] && pass "survivor charter written (reshape)" || fail "missing reshape survivor charter"
[ -f "$CDIR/themes.md" ] && fail "no-go must NOT get a charter" || pass "no-go gets no charter"
if grep -q "epic_intention: SABLE-e1" "$CDIR/real-time-alerts.md"; then pass "charter carries epic linkage"; else fail "charter missing epic_intention linkage"; fi
DEC="$(ls "$CDIR"/*-decisions.md 2>/dev/null | head -1)"
if [ -n "$DEC" ] && grep -q "zero pull, high cost" "$DEC"; then pass "decision record keeps no-go rationale verbatim"; else fail "decision record missing no-go rationale"; fi
if [ -n "$DEC" ] && grep -q "verdict: no-go" "$DEC"; then pass "decision record records the no-go verdict"; else fail "decision record missing no-go verdict"; fi

# --- leg 2: doc-lint the skill contract ------------------------------------
has _ "office-hours"                              "reuses the office-hours engine"
has _ "Diverge"                                  "expresses beat 1 (diverge)"
has _ "Interrogate"                              "expresses beat 2 (interrogate)"
has _ "Triage"                                   "expresses beat 3 (triage)"
has _ "Fan out"                                  "expresses beat 4 (fan out)"
has _ "comparative"                              "states comparative-by-default"
has _ "escalat"                                  "states the escalation option"
has _ "business lens"                            "states the business-lens guardrail"
has _ "sable-discover-emit"                       "drives the fan-out helper"
has _ "bd create --type=epic"                     "creates bare epic-intention shells"
has _ "no implementation beads"                   "authors no implementation beads"
has _ "tier discovery"                            "sets the discovery sub-mode"
has _ "sable-charter ingest"                       "hands off to Full via charter ingestion"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
