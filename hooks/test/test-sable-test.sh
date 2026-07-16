#!/usr/bin/env bash
# test-sable-test.sh — Unit + integration tests for bin/sable-test (SABLE-jfg6.4 /
# contract D4).
#
# bin/sable-test is the evidence seam SABLE owns for session types that fire NO
# hooks (Agent-subagent / gc-managed). It runs a test command, propagates the
# exit code, and writes TDD evidence at the shared lib-evidence-key.sh key ONLY
# on a green (exit 0) run. The catastrophic failure mode is false-permissive —
# green evidence recorded for a RED suite — so the central guarantee under test
# is: a red run writes NOTHING.
#
# Run with:
#   bash hooks/test/test-sable-test.sh
#
# Matrix (bead SABLE-jfg6.4):
#   S3-U1  green run writes evidence at the derived key, exit 0
#   S3-U2  red run propagates the exit code AND writes no evidence
#   S3-U3  absent-session (no CLAUDE_SESSION_ID) green run writes at the key
#          tdd-gate derives — the ppid fallback, never the empty-session path
#   S3-E1  a non-zero exit other than 1 is propagated verbatim (no cl: swallowed)
#   S3-E4  (regression) evidence line carries the REPO=<path> tag tdd-gate reads

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SABLE_TEST="$REPO_DIR/bin/sable-test"
GATE_HOOK="$REPO_DIR/hooks/tdd-gate.sh"
LIB="$REPO_DIR/hooks/multi-manager/lib-evidence-key.sh"

if [ ! -x "$SABLE_TEST" ]; then
  echo "FAIL: bin/sable-test not executable at $SABLE_TEST"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# Absent-session key agreement note: every consumer here (bin/sable-test, the
# tdd-gate.sh hook, and the inline `bash -c '. lib; sable_evidence_key "" ""'`
# derivations) runs as an EXTERNAL child process of THIS test shell, so each
# re-reads its real $PPID as this shell — the SAME ppid fallback token. That is
# exactly the real-world invariant: sibling processes of one session controller
# agree on the absent-session key.
fake_session() { printf 'sable-test-%s-%s' "$$" "$RANDOM"; }

# ---------- S3-U1: green run writes evidence at the derived key ----------
SID1=$(fake_session)
EV1="/tmp/tdd-evidence-${SID1}"
rm -f "$EV1"
OUT1=$(CLAUDE_SESSION_ID="$SID1" env -u CLAUDE_AGENT_ID "$SABLE_TEST" bash -c 'exit 0' 2>&1); RC1=$?
if [ "$RC1" -eq 0 ]; then pass "S3-U1a: green run propagates exit 0"; else fail "S3-U1a: green run propagates exit 0" "rc=$RC1 out=[$OUT1]"; fi
if [ -s "$EV1" ]; then pass "S3-U1b: green run writes evidence at the session key"; else fail "S3-U1b: green run writes evidence at the session key" "no $EV1"; fi
rm -f "$EV1"

# ---------- S3-U2: red run propagates exit AND writes nothing ----------
SID2=$(fake_session)
EV2="/tmp/tdd-evidence-${SID2}"
rm -f "$EV2"
OUT2=$(CLAUDE_SESSION_ID="$SID2" env -u CLAUDE_AGENT_ID "$SABLE_TEST" bash -c 'exit 1' 2>&1); RC2=$?
if [ "$RC2" -eq 1 ]; then pass "S3-U2a: red run propagates exit 1"; else fail "S3-U2a: red run propagates exit 1" "rc=$RC2"; fi
if [ ! -e "$EV2" ]; then pass "S3-U2b: red run writes NO evidence (false-permissive guard)"; else fail "S3-U2b: red run writes NO evidence" "unexpected: $(cat "$EV2" 2>/dev/null)"; fi
rm -f "$EV2"

# ---------- S3-E1: a non-1 non-zero exit is propagated verbatim ----------
SID3=$(fake_session)
EV3="/tmp/tdd-evidence-${SID3}"
rm -f "$EV3"
CLAUDE_SESSION_ID="$SID3" env -u CLAUDE_AGENT_ID "$SABLE_TEST" bash -c 'exit 42' >/dev/null 2>&1; RC3=$?
if [ "$RC3" -eq 42 ]; then pass "S3-E1a: exit 42 propagated verbatim"; else fail "S3-E1a: exit 42 propagated verbatim" "rc=$RC3"; fi
if [ ! -e "$EV3" ]; then pass "S3-E1b: exit 42 (red) writes no evidence"; else fail "S3-E1b: exit 42 writes no evidence" "unexpected: $(cat "$EV3" 2>/dev/null)"; fi
rm -f "$EV3"

# ---------- S3-E4: evidence line carries the REPO=<path> tag ----------
SID4=$(fake_session)
EV4="/tmp/tdd-evidence-${SID4}"
rm -f "$EV4"
CLAUDE_SESSION_ID="$SID4" env -u CLAUDE_AGENT_ID "$SABLE_TEST" bash -c 'exit 0' >/dev/null 2>&1
if grep -q 'REPO=' "$EV4" 2>/dev/null && grep -q 'CMD=' "$EV4" 2>/dev/null; then
  pass "S3-E4: evidence line carries REPO=<path> and CMD= tags (tdd-gate companion-repo reader relies on REPO=)"
else
  fail "S3-E4: evidence line carries REPO=<path> and CMD= tags" "got: $(cat "$EV4" 2>/dev/null)"
fi
rm -f "$EV4"

# ---------- S3-U3: absent-session green run writes at the ppid fallback ----------
# No CLAUDE_SESSION_ID in the env (the hccq trap). The lib must derive a
# deterministic, non-empty ppid key — NEVER the empty-session garbage path — and
# sable-test must write there. We confirm the exact path via derive_key (a
# sibling subprocess with the SAME $PPID frame), so this also proves the key
# sable-test writes is byte-identical to the key another consumer derives.
ABS_KEY=$(env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_ID bash -c '. "$0"; sable_evidence_key "" ""' "$LIB")
# ^ derived with THIS shell as parent frame; sable-test below shares it.
rm -f "$ABS_KEY"
case "$ABS_KEY" in
  /tmp/tdd-evidence-ppid-*) pass "S3-U3a: absent-session key is the ppid fallback, not the empty-session garbage path" ;;
  *) fail "S3-U3a: absent-session key is the ppid fallback" "got: [$ABS_KEY]" ;;
esac
if [ "$ABS_KEY" != "/tmp/tdd-evidence-" ] && [ -n "${ABS_KEY#/tmp/tdd-evidence-}" ]; then
  pass "S3-U3b: absent-session key is non-empty (never /tmp/tdd-evidence-)"
else
  fail "S3-U3b: absent-session key is non-empty" "got: [$ABS_KEY]"
fi
env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_ID "$SABLE_TEST" bash -c 'exit 0' >/dev/null 2>&1; RC_ABS=$?
if [ "$RC_ABS" -eq 0 ] && [ -s "$ABS_KEY" ]; then
  pass "S3-U3c: absent-session green run writes evidence at the ppid fallback key"
else
  fail "S3-U3c: absent-session green run writes evidence at the ppid fallback key" "rc=$RC_ABS exists=$([ -s "$ABS_KEY" ] && echo yes || echo no) key=[$ABS_KEY]"
fi
rm -f "$ABS_KEY"

# ---------- INTEGRATION: sable-test writer <-> real tdd-gate.sh reader ----------
# Real composition: sable-test records evidence, then the REAL tdd-gate.sh hook
# is asked to allow a close for the SAME session. A two-bead close routes past
# the [no-test] hatch straight to the evidence check, so this exercises the
# writer/reader key agreement end-to-end. Present-session AND absent-session.
STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT
cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"SABLE-stub","notes":"integration stub"}]
JSON
  exit 0
fi
exit 0
EOF
chmod +x "$STUB_DIR/bd"

gate_input() { # <command> <session_id>   (session_id may be empty)
  python3 -c "
import json, sys
d = {'tool_input': {'command': sys.argv[1]}}
if sys.argv[2]:
    d['session_id'] = sys.argv[2]
print(json.dumps(d))
" "$1" "$2"
}

# (a) present-session: sable-test green -> gate allows the close
INT_SID="sable-test-int-$$-$RANDOM"
INT_EV="/tmp/tdd-evidence-${INT_SID}"
rm -f "$INT_EV"
CLAUDE_SESSION_ID="$INT_SID" env -u CLAUDE_AGENT_ID "$SABLE_TEST" bash -c 'exit 0' >/dev/null 2>&1
GATE_OUT=$(gate_input 'bd close SABLE-stub SABLE-other' "$INT_SID" | env PATH="$STUB_DIR:$PATH" bash "$GATE_HOOK" 2>/dev/null)
if [ -z "$GATE_OUT" ]; then
  pass "integration: sable-test green evidence unblocks the real tdd-gate close (present session)"
else
  fail "integration: sable-test green unblocks the real gate (present session)" "gate denied: $GATE_OUT"
fi
rm -f "$INT_EV"

# (b) present-session RED: sable-test wrote nothing -> gate DENIES (negative space)
INT_SID_R="sable-test-intred-$$-$RANDOM"
INT_EV_R="/tmp/tdd-evidence-${INT_SID_R}"
rm -f "$INT_EV_R"
CLAUDE_SESSION_ID="$INT_SID_R" env -u CLAUDE_AGENT_ID "$SABLE_TEST" bash -c 'exit 1' >/dev/null 2>&1 || true
GATE_OUT_R=$(gate_input 'bd close SABLE-stub SABLE-other' "$INT_SID_R" | env PATH="$STUB_DIR:$PATH" bash "$GATE_HOOK" 2>/dev/null)
if echo "$GATE_OUT_R" | grep -q '"permissionDecision": "deny"'; then
  pass "integration: a RED sable-test run leaves the gate DENYING (no false-permissive)"
else
  fail "integration: a RED sable-test run leaves the gate DENYING" "got: ${GATE_OUT_R:-<empty>}"
fi
rm -f "$INT_EV_R"

# (c) absent-session: sable-test green -> real tdd-gate (absent session) allows.
# The ppid fallback keys on $PPID, so writer and reader agree ONLY when both run
# in the same process frame. Both are launched here as bare STATEMENTS (the gate
# via a temp-file redirect, NOT a $(...)-wrapped pipeline — a cmdsubst pipeline
# forks an extra subshell that shifts the hook's $PPID), so each resolves $PPID
# to THIS test shell: the same real-world invariant, and the same key.
ABS_INT_KEY=$(env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_ID bash -c '. "$0"; sable_evidence_key "" ""' "$LIB")
rm -f "$ABS_INT_KEY"
env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_ID "$SABLE_TEST" bash -c 'exit 0' >/dev/null 2>&1
ABS_GATE_OUT_FILE=$(mktemp)
gate_input 'bd close SABLE-stub SABLE-other' '' | env PATH="$STUB_DIR:$PATH" bash "$GATE_HOOK" > "$ABS_GATE_OUT_FILE" 2>/dev/null
GATE_OUT_A=$(cat "$ABS_GATE_OUT_FILE"); rm -f "$ABS_GATE_OUT_FILE"
if [ -z "$GATE_OUT_A" ]; then
  pass "integration: absent-session sable-test green unblocks the real gate (writer/reader agree on the ppid key)"
else
  fail "integration: absent-session sable-test green unblocks the real gate" "gate denied: $GATE_OUT_A; key=[$ABS_INT_KEY] exists=$([ -s "$ABS_INT_KEY" ] && echo yes || echo no)"
fi
rm -f "$ABS_INT_KEY"

# ---------- INTEGRATION: real bd --sandbox close succeeds after sable-test green ----------
# The worker's real workflow: run the suite through sable-test (green), then
# close its OWN bead. Exercises real bd (sandbox DB, no Dolt push). The gate is a
# Claude Code PreToolUse hook (not wired into bd itself in a shell test), so we
# drive the real gate hook against the REAL bead's real notes for the close —
# the genuine composition a worker self-close performs.
if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP (integration): bd not found on PATH — real-sandbox close test"
else
  SB_SID="sable-test-bd-$$-$RANDOM"
  SB_EV="/tmp/tdd-evidence-${SB_SID}"
  rm -f "$SB_EV"
  SCRATCH_ID=$(bd create --sandbox \
    --title="[int-test] sable-test green-close scratch bead" \
    --description="scratch bead for sable-test integration; safe to close" \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)
  if [ -z "${SCRATCH_ID:-}" ]; then
    echo "SKIP (integration): could not create sandbox scratch bead"
  else
    # Real green run through sable-test writes the evidence for this session.
    CLAUDE_SESSION_ID="$SB_SID" env -u CLAUDE_AGENT_ID "$SABLE_TEST" bash -c 'exit 0' >/dev/null 2>&1
    # Real gate hook, real bead notes (no [no-test]): a single-bead close must be
    # ALLOWED because sable-test recorded green evidence for this session.
    CLOSE_OUT=$(gate_input "bd close $SCRATCH_ID" "$SB_SID" | bash "$GATE_HOOK" 2>/dev/null)
    if [ -z "$CLOSE_OUT" ]; then
      pass "integration(real bd): sable-test green evidence lets the real gate allow the worker self-close of $SCRATCH_ID"
    else
      fail "integration(real bd): sable-test green evidence allows the real gate close" "gate denied: $CLOSE_OUT"
    fi
    # And the negative: a fresh session with no sable-test run is DENIED for the same bead.
    NOEV_OUT=$(gate_input "bd close $SCRATCH_ID" "sable-test-noev-$$-$RANDOM" | bash "$GATE_HOOK" 2>/dev/null)
    if echo "$NOEV_OUT" | grep -q '"permissionDecision": "deny"'; then
      pass "integration(real bd): same bead, a session with no sable-test evidence is DENIED"
    else
      fail "integration(real bd): session with no evidence is DENIED" "got: ${NOEV_OUT:-<empty>}"
    fi
    bd close "$SCRATCH_ID" --sandbox 2>/dev/null || true
  fi
  rm -f "$SB_EV"
fi

# ---------- Summary ----------
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
