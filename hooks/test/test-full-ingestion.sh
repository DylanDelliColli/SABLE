#!/usr/bin/env bash
# test-full-ingestion.sh — Full's charter-ingestion seam (SABLE-7v1r.3).
#
# End-to-end on the sable-charter bin: a charter bound to an epic via
# epic_intention is resolved by `sable-charter ingest <epic>` and emits framing
# fields; an epic with no charter exits nonzero so Full falls back to cold
# framing. Plus a doc-lint that the sable-plan FRAMING substage wires the seam.
#
# Run with:  bash hooks/test/test-full-ingestion.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin/sable-charter"
SKILL="$REPO/skills/sable-plan/SKILL.md"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export SABLE_CHARTERS_DIR="$TMP/charters"

CJSON="$TMP/c.json"
cat > "$CJSON" <<'JSON'
{"slug":"alerts","title":"Alerts","epic_intention":"SABLE-e1","target_user_and_wedge":"ops wedge","success_metric":"faster ack","non_goals":"no mobile","problem_statement":"miss events","demand_evidence":"tickets"}
JSON
python3 "$BIN" write-charter --json "$CJSON" >/dev/null

# ingest by matching epic -> success + framing JSON carries the charter's wedge
if OUT="$(python3 "$BIN" ingest SABLE-e1 2>/dev/null)" && printf '%s' "$OUT" | grep -q '"wedge": "ops wedge"'; then
  pass "ingest resolves the charter for a matching epic and emits framing"
else
  fail "ingest did not resolve framing for SABLE-e1"
fi

# unmatched epic -> nonzero (Full falls back to cold framing)
if python3 "$BIN" ingest SABLE-nope >/dev/null 2>&1; then
  fail "ingest should exit nonzero for an epic with no charter"
else
  pass "ingest exits nonzero for an epic with no charter (cold-framing fallback)"
fi

# doc-lint: sable-plan FRAMING documents the ingestion seam
if grep -q "sable-charter ingest" "$SKILL"; then pass "sable-plan documents sable-charter ingest"; else fail "sable-plan missing sable-charter ingest"; fi
if grep -q "substage set research" "$SKILL"; then pass "sable-plan documents the FRAMING skip to research"; else fail "sable-plan missing substage set research skip"; fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
