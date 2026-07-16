#!/usr/bin/env bash
# test-tdd-evidence.sh — Unit tests for tdd-evidence.sh
#
# Pipes synthetic PreToolUse:Bash JSON to the hook and checks whether the
# evidence file at /tmp/tdd-evidence-<session> was appended to.
#
# Run with:
#   bash hooks/test/test-tdd-evidence.sh
#
# Each test uses a fresh fake SESSION_ID so checks are isolated.
# Cleans up the evidence file after each assertion.

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/tdd-evidence.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""

# Generate a unique fake session id per test so we never collide with the
# real Claude Code session's evidence file.
fake_session() {
  printf 'tdd-evidence-test-%s-%s' "$$" "$RANDOM"
}

make_input() {
  # $1 = command, $2 = session_id
  python3 -c "
import json, sys
print(json.dumps({'tool_input': {'command': sys.argv[1]}, 'session_id': sys.argv[2]}))
" "$1" "$2"
}

# run_hook_writes <test-name> <command>
# Asserts an evidence file was written (matches a "test runner" command).
run_hook_writes() {
  local name="$1" command="$2"
  local sid evidence
  sid=$(fake_session)
  evidence="/tmp/tdd-evidence-${sid}"
  rm -f "$evidence"
  make_input "$command" "$sid" | bash "$HOOK" >/dev/null 2>&1 || true
  if [ -s "$evidence" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (no evidence written for: $command)"
    echo "FAIL: $name"
    echo "  Expected: evidence file written for '$command'"
    echo "  Got:      empty/missing $evidence"
  fi
  rm -f "$evidence"
}

# run_hook_silent <test-name> <command>
# Asserts no evidence was written (command did not match a test runner).
run_hook_silent() {
  local name="$1" command="$2"
  local sid evidence
  sid=$(fake_session)
  evidence="/tmp/tdd-evidence-${sid}"
  rm -f "$evidence"
  make_input "$command" "$sid" | bash "$HOOK" >/dev/null 2>&1 || true
  if [ ! -s "$evidence" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (unexpected evidence: $(cat "$evidence"))"
    echo "FAIL: $name"
    echo "  Expected: no evidence (command is not a test runner): '$command'"
    echo "  Got:      $(cat "$evidence")"
  fi
  rm -f "$evidence"
}

# ---------- Existing test runners (regression) ----------

run_hook_writes "vitest run is recognized"           "npx vitest run"
run_hook_writes "pytest is recognized"               "pytest tests/"
run_hook_writes "npm test is recognized"             "npm test"
run_hook_writes "python -m pytest is recognized"     "python -m pytest"

# ---------- New: SABLE shell test harness ----------

run_hook_writes "bash hooks/test/test-foo.sh recognized" \
  "bash hooks/test/test-foo.sh"

run_hook_writes "bash with relative path to test-foo.sh recognized" \
  "bash test-foo.sh"

run_hook_writes "bash with cd prefix to hooks/test recognized" \
  "cd /home/ddc/dev-env/SABLE && bash hooks/test/test-bead-description-gate.sh"

run_hook_writes "bash with absolute path to hooks/test recognized" \
  "bash /home/ddc/dev-env/SABLE/hooks/test/test-tdd-evidence.sh"

run_hook_writes "python3 bin/test_foo.py recognized" "python3 bin/test_foo.py"

# ---------- Negative cases ----------

run_hook_silent "python3 deploy.py not recognized" "python3 deploy.py"

run_hook_silent "git status not recognized"          "git status"
run_hook_silent "ls not recognized"                  "ls hooks/"
run_hook_silent "bash setup.sh not recognized"       "bash setup.sh"
run_hook_silent "bash deploy-script.sh not recognized" "bash deploy-script.sh"
run_hook_silent "bd close not recognized as test"    "bd close SABLE-xxx"

# ---------- SABLE-dhfj: runner keyword must be a real invocation token ----------
# A blind substring match over the whole joined segment text fires on any
# command that merely MENTIONS a runner keyword — grepping for it, echoing
# it, or passing it inside an unrelated flag value — without ever running a
# test. Live repro: a grep whose PATTERN argument contained
# 'pytest|npm test|vitest' registered as test evidence.

run_hook_silent "grep for 'vitest' in a pattern arg not recognized" \
  "grep vitest f"

run_hook_silent "echo of the words 'npm test' not recognized" \
  "echo npm test"

run_hook_silent "bd create --description mentioning pytest not recognized" \
  "bd create --title=x --description mentions-pytest-but-does-not-run-it"

# ---------- SABLE-d72/lcs: per-agent evidence keying ----------
# When agent_id is present (a nested subagent), evidence is keyed by
# session_id + agent_id so one worker's test run cannot satisfy another worker's
# gate in the same (shared) session. Main sessions (no agent_id) keep the
# session-global key, preserving single-agent behavior.

pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# make_input_agent <command> <session_id> <agent_id>
make_input_agent() {
  python3 -c "
import json, sys
d = {'tool_input': {'command': sys.argv[1]}, 'session_id': sys.argv[2]}
if sys.argv[3]:
    d['agent_id'] = sys.argv[3]
print(json.dumps(d))
" "$1" "$2" "$3"
}

PA_SID="tdd-ev-peragent-$$-$RANDOM"
EV_MAIN="/tmp/tdd-evidence-${PA_SID}"
EV_A="/tmp/tdd-evidence-${PA_SID}-agentA"
EV_B="/tmp/tdd-evidence-${PA_SID}-agentB"
rm -f "$EV_MAIN" "$EV_A" "$EV_B"

make_input_agent "pytest tests/" "$PA_SID" "agentA" | bash "$HOOK" >/dev/null 2>&1 || true
if [ -s "$EV_A" ]; then pass "per-agent: evidence keyed to <session>-<agent_id>"; else fail "per-agent: evidence keyed to <session>-<agent_id>" "no $EV_A"; fi
if [ ! -s "$EV_MAIN" ]; then pass "per-agent: agent A evidence does NOT leak into the session-global file"; else fail "per-agent: agent A evidence does NOT leak into the session-global file" "$EV_MAIN written"; fi
if [ ! -s "$EV_B" ]; then pass "per-agent: agent A evidence does NOT leak into agent B's file"; else fail "per-agent: agent A evidence does NOT leak into agent B's file" "$EV_B written"; fi
rm -f "$EV_MAIN" "$EV_A" "$EV_B"

PA_SID2="tdd-ev-main-$$-$RANDOM"
EV_MAIN2="/tmp/tdd-evidence-${PA_SID2}"
rm -f "$EV_MAIN2"
make_input_agent "pytest tests/" "$PA_SID2" "" | bash "$HOOK" >/dev/null 2>&1 || true
if [ -s "$EV_MAIN2" ]; then pass "main-session (empty agent_id) uses the session-global key (single-agent unchanged)"; else fail "main-session (empty agent_id) uses the session-global key" "no $EV_MAIN2"; fi
rm -f "$EV_MAIN2"

# ---------- market-brief-package-sqcr: compound-command + cross-repo blind spots ----------
# Direct/interpreter-agnostic script execution (no 'bash '/'sh ' prefix), and
# cross-repo runs tagged with the repo they actually ran against, so tdd-gate
# can recognize companion-repo evidence for a cross-repo bead (73t4-style: a
# SABLE-hooks fix tracked as a market-brief-package bead).

run_hook_writes "direct execution (./test-foo.sh, no interpreter prefix) recognized" \
  "./hooks/test/test-foo.sh"

run_hook_writes "direct execution with absolute path recognized" \
  "/home/ddc/dev-environment/SABLE/hooks/test/test-foo.sh"

run_hook_silent "direct execution of a non-test script not recognized" \
  "./hooks/setup.sh"

# ---------- SABLE-f6aw: trailing redirect must not break the last-token check ----------
# 'script.sh 2>&1 | tail -N' puts '2>&1' — not the script path — as the pipe
# segment's last shlex token. The matcher must still find the script path.

run_hook_writes "bash script with trailing 2>&1 before a pipe recognized" \
  "bash hooks/test/test-foo.sh 2>&1 | tail -8"

run_hook_writes "direct execution with trailing 2>&1 before a pipe recognized" \
  "./hooks/test/test-foo.sh 2>&1 | tail -8"

run_hook_writes "bash script with trailing '> out.log' redirect recognized" \
  "bash hooks/test/test-foo.sh > out.log"

run_hook_writes "bash script with trailing '2>&1' and no pipe recognized" \
  "bash hooks/test/test-foo.sh 2>&1"

# repo-tagging: a cd-compound into a companion repo must tag the evidence
# line with that repo, not the hook's own cwd.
CROSSREPO_SID="tdd-ev-crossrepo-$$-$RANDOM"
CROSSREPO_EV="/tmp/tdd-evidence-${CROSSREPO_SID}"
rm -f "$CROSSREPO_EV"
make_input "cd /home/ddc/dev-environment/SABLE && bash hooks/test/test-tree-claim.sh" "$CROSSREPO_SID" | bash "$HOOK" >/dev/null 2>&1 || true
if grep -q 'REPO=/home/ddc/dev-environment/SABLE' "$CROSSREPO_EV" 2>/dev/null; then
  pass "cross-repo (cd-compound): evidence line tagged with the companion repo"
else
  fail "cross-repo (cd-compound): evidence line tagged with the companion repo" "got: $(cat "$CROSSREPO_EV" 2>/dev/null)"
fi
rm -f "$CROSSREPO_EV"

# repo-tagging: git -C <repo> establishes the effective repo for a later
# segment in the same compound command (the bug report's literal shape).
GITC_SID="tdd-ev-gitc-$$-$RANDOM"
GITC_EV="/tmp/tdd-evidence-${GITC_SID}"
rm -f "$GITC_EV"
make_input "git -C /home/ddc/dev-environment/SABLE fetch && bash /home/ddc/dev-environment/SABLE/hooks/test/test-tree-claim.sh" "$GITC_SID" | bash "$HOOK" >/dev/null 2>&1 || true
if grep -q 'REPO=/home/ddc/dev-environment/SABLE' "$GITC_EV" 2>/dev/null; then
  pass "cross-repo (git -C prefix): evidence line tagged with the companion repo"
else
  fail "cross-repo (git -C prefix): evidence line tagged with the companion repo" "got: $(cat "$GITC_EV" 2>/dev/null)"
fi
rm -f "$GITC_EV"

# repo-tagging: an absolute path naming its own hooks/ tree is a self-tagging
# signal, with no cd/-C needed at all.
ABS_SID="tdd-ev-abs-$$-$RANDOM"
ABS_EV="/tmp/tdd-evidence-${ABS_SID}"
rm -f "$ABS_EV"
make_input "bash /home/ddc/dev-environment/SABLE/hooks/test/test-tree-claim.sh" "$ABS_SID" | bash "$HOOK" >/dev/null 2>&1 || true
if grep -q 'REPO=/home/ddc/dev-environment/SABLE' "$ABS_EV" 2>/dev/null; then
  pass "cross-repo (self-tagging absolute path): evidence line tagged with the companion repo"
else
  fail "cross-repo (self-tagging absolute path): evidence line tagged with the companion repo" "got: $(cat "$ABS_EV" 2>/dev/null)"
fi
rm -f "$ABS_EV"

# same-repo run (no cd/-C, relative path): tagged with the hook's own cwd.
SAMEREPO_SID="tdd-ev-samerepo-$$-$RANDOM"
SAMEREPO_EV="/tmp/tdd-evidence-${SAMEREPO_SID}"
rm -f "$SAMEREPO_EV"
python3 -c "
import json, sys
print(json.dumps({'tool_input': {'command': 'pytest tests/'}, 'session_id': sys.argv[1], 'cwd': '/home/ddc/dev-environment/market-brief-package'}))
" "$SAMEREPO_SID" | bash "$HOOK" >/dev/null 2>&1 || true
if grep -q 'REPO=/home/ddc/dev-environment/market-brief-package' "$SAMEREPO_EV" 2>/dev/null; then
  pass "same-repo run tagged with the hook's own cwd (no cd/-C in the command)"
else
  fail "same-repo run tagged with the hook's own cwd" "got: $(cat "$SAMEREPO_EV" 2>/dev/null)"
fi
rm -f "$SAMEREPO_EV"

# ---------- SABLE-jfg6.4 (D4): shared evidence-key lib — forms + absent-session ----------
# This WRITER now derives its path via hooks/multi-manager/lib-evidence-key.sh,
# the SAME function tdd-gate.sh (the reader) calls — so the two can never drift
# (the tfkv mismatch class). Lock in the derivation forms and the absent-session
# ppid fallback (never the empty-session garbage path), plus proof the writer
# actually records at that fallback (the old code early-exited on an empty sid).
D4LIB="$(cd "$(dirname "$0")/.." && pwd)/multi-manager/lib-evidence-key.sh"
# Resolve the key in a single-command subprocess (exec-in-place), so its $PPID is
# this shell — the SAME frame the hook resolves when run as a bare statement.
d4_key() { bash -c '. "$0"; sable_evidence_key "$1" "$2"' "$D4LIB" "$1" "$2"; }

K_BOTH=$(d4_key "sidX" "aidY")
if [ "$K_BOTH" = "/tmp/tdd-evidence-sidX-aidY" ]; then pass "D4 lib: (sid,aid) -> /tmp/tdd-evidence-<sid>-<aid>"; else fail "D4 lib: (sid,aid) form" "got [$K_BOTH]"; fi
K_SID=$(d4_key "sidX" "")
if [ "$K_SID" = "/tmp/tdd-evidence-sidX" ]; then pass "D4 lib: (sid, empty aid) -> /tmp/tdd-evidence-<sid>"; else fail "D4 lib: (sid only) form" "got [$K_SID]"; fi
K_ABS=$(d4_key "" "")
case "$K_ABS" in /tmp/tdd-evidence-ppid-*) pass "D4 lib: absent sid -> ppid fallback (not the empty-session garbage path)" ;; *) fail "D4 lib: absent sid -> ppid fallback" "got [$K_ABS]" ;; esac
if [ "$K_ABS" != "/tmp/tdd-evidence-" ] && [ -n "${K_ABS#/tmp/tdd-evidence-}" ]; then pass "D4 lib: absent-sid key is non-empty (never /tmp/tdd-evidence-)"; else fail "D4 lib: absent-sid key non-empty" "got [$K_ABS]"; fi
K_ABS_AID=$(d4_key "" "aidZ")
case "$K_ABS_AID" in /tmp/tdd-evidence-ppid-*-aidZ) pass "D4 lib: absent sid keeps the agent suffix (ppid-<n>-<aid>)" ;; *) fail "D4 lib: absent sid keeps agent suffix" "got [$K_ABS_AID]" ;; esac

# Writer at the fallback: an absent-session (empty session_id) test command must
# now RECORD evidence at the ppid key. Bare-statement frame so the hook's $PPID
# == this shell == the frame d4_key resolves in.
D4_ABS_KEY=$(d4_key "" "")
rm -f "$D4_ABS_KEY"
make_input "pytest tests/" "" | bash "$HOOK" >/dev/null 2>&1 || true
if [ -s "$D4_ABS_KEY" ]; then pass "D4 writer: absent-session test command records evidence at the ppid fallback key"; else fail "D4 writer: absent-session records at ppid key" "no $D4_ABS_KEY"; fi
rm -f "$D4_ABS_KEY"

# ---------- SABLE-2nak: unspaced separator (same class as SABLE-sxhx) ----------
# Plain shlex.split only splits ;/&&/||/| when they are whitespace-delimited
# from adjacent tokens, so an UNSPACED separator fused two commands into one
# token and the segmenter never split them, silently dropping the first
# script's evidence. Both scripts in the compound command must be recorded.

UNSPACED_AND_SID="tdd-ev-unspaced-and-$$-$RANDOM"
UNSPACED_AND_EV="/tmp/tdd-evidence-${UNSPACED_AND_SID}"
rm -f "$UNSPACED_AND_EV"
make_input "bash test-a.sh&&bash test-b.sh" "$UNSPACED_AND_SID" | bash "$HOOK" >/dev/null 2>&1 || true
if grep -q 'CMD=bash test-a.sh$' "$UNSPACED_AND_EV" 2>/dev/null && grep -q 'CMD=bash test-b.sh$' "$UNSPACED_AND_EV" 2>/dev/null; then
  pass "unspaced && separator: both test-a.sh and test-b.sh recorded"
else
  fail "unspaced && separator: both test-a.sh and test-b.sh recorded" "got: $(cat "$UNSPACED_AND_EV" 2>/dev/null)"
fi
rm -f "$UNSPACED_AND_EV"

UNSPACED_SEMI_SID="tdd-ev-unspaced-semi-$$-$RANDOM"
UNSPACED_SEMI_EV="/tmp/tdd-evidence-${UNSPACED_SEMI_SID}"
rm -f "$UNSPACED_SEMI_EV"
make_input "bash test-a.sh;bash test-b.sh" "$UNSPACED_SEMI_SID" | bash "$HOOK" >/dev/null 2>&1 || true
if grep -q 'CMD=bash test-a.sh$' "$UNSPACED_SEMI_EV" 2>/dev/null && grep -q 'CMD=bash test-b.sh$' "$UNSPACED_SEMI_EV" 2>/dev/null; then
  pass "unspaced ; separator: both test-a.sh and test-b.sh recorded"
else
  fail "unspaced ; separator: both test-a.sh and test-b.sh recorded" "got: $(cat "$UNSPACED_SEMI_EV" 2>/dev/null)"
fi
rm -f "$UNSPACED_SEMI_EV"

# ---------- Summary ----------

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed tests:$FAIL_NAMES"
  exit 1
fi
exit 0
