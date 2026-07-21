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

# ---------- SABLE-rzsb.5 / SABLE-j10xa: 'env' prefix must not blind the detector ----------
# The fleet's hermetic-run contract wraps test commands as
# 'env -u VAR1 -u VAR2 ... <suite>' to scrub identity env vars before running.
# Every real 'env' option-arity form must be stripped so the REAL interpreter
# underneath is what gets classified — not merely the '-i VAR=x' shape the
# bead was originally filed under.

run_hook_writes "env -i VAR=x bash <suite> recognized"        "env -i FOO=1 bash hooks/test/test-foo.sh"
run_hook_writes "env VAR=x python3 <suite>.py recognized"     "env FOO=1 python3 bin/test_foo.py"
run_hook_writes "env -u NAME bash <suite> (single -u) recognized" \
  "env -u CLAUDE_AGENT_NAME bash hooks/test/test-foo.sh"
run_hook_writes "env -u A -u B -u C bash <suite> (the mandated multi -u contract shape) recognized" \
  "env -u CLAUDE_AGENT_NAME -u SABLE_WORKER_PANE -u SABLE_BEAD bash hooks/test/test-foo.sh"
run_hook_writes "env -uNAME (attached form) bash <suite> recognized" \
  "env -uCLAUDE_AGENT_NAME bash hooks/test/test-foo.sh"
run_hook_writes "env --unset=NAME (attached form) python -m pytest recognized" \
  "env --unset=CLAUDE_AGENT_NAME python -m pytest tests/"
run_hook_writes "env --unset NAME (two-token form) bash <suite> recognized" \
  "env --unset CLAUDE_AGENT_NAME bash hooks/test/test-foo.sh"
run_hook_writes "env -- bash <suite> (terminator) recognized" \
  "env -- bash hooks/test/test-foo.sh"
run_hook_writes "env -u NAME npx vitest run recognized (composes with the npx unwrap)" \
  "env -u CLAUDE_AGENT_NAME npx vitest run"

run_hook_silent "bare 'env' with no command not recognized"    "env"
run_hook_silent "'env' with only a VAR=val assignment, no command, not recognized" "env FOO=1"

# ---------- Regression: plain (unwrapped) invocations classify unchanged ----------
run_hook_writes "regression: plain bash <suite> still recognized"   "bash hooks/test/test-foo.sh"
run_hook_writes "regression: plain source <suite> still recognized" "source hooks/test/test-foo.sh"
run_hook_writes "regression: plain npx vitest still recognized"     "npx vitest run"

# ---------- SABLE-0w0ou: 'sable-test <cmd>' unwrapped like npx ----------
# bin/sable-test wraps a test command and propagates its exit code; the real
# command underneath must be what this hook classifies, so a sable-test-
# wrapped run is never invisible to it (previously: no case for 'sable-test'
# at all — it fell through every matcher, same failure shape as the
# unrecognized 'env' prefix above).

run_hook_writes "sable-test <suite> unwrapped and recognized" \
  "sable-test bash hooks/test/test-foo.sh"

run_hook_silent "sable-test wrapping a non-test script still not recognized" \
  "sable-test bash setup.sh"

run_hook_silent "bare 'sable-test' with no command not recognized" \
  "sable-test"

# Combined shape: sable-test wrapping an env-prefixed hermetic run — the case
# that silently rots if the two unwraps aren't composed in the right order
# (sable-test unwrap MUST run before the env-grammar strip).
run_hook_writes "sable-test env -u A -u B <suite> (combined wrapper shape) recognized" \
  "sable-test env -u CLAUDE_AGENT_NAME -u SABLE_WORKER_PANE bash hooks/test/test-foo.sh"

# ---------- SABLE-x8mx7: bare inline VAR=value prefix must be unwrapped ----------
# A bare 'NAME=value ... <cmd>' assignment prefix is the shell's own
# env-for-one-command form (e.g. the fleet's sandbox-pinning contract:
# 'SABLE_LIB_DIR=/scratch python3 -m pytest ...'). Previously only the
# explicit 'env VAR=val cmd' wrapper was unwrapped, so a bare prefix left the
# head as the VAR=value token, matched no runner, and silently produced NO
# evidence -- a scoped test run was invisible and tdd-gate denied the close.
# Strip leading NAME=value tokens the same way the env-wrapper branch does.

run_hook_writes "bare VAR=val python3 -m pytest recognized" \
  "SABLE_LIB_DIR=/scratch python3 -m pytest tests/"

run_hook_writes "bare VAR=val pytest recognized" \
  "FOO=1 pytest tests/"

run_hook_writes "bare VAR=val bash <suite> recognized" \
  "SABLE_LIB_DIR=/scratch bash hooks/test/test-foo.sh"

run_hook_writes "multiple bare VAR=val assignments before the command recognized" \
  "FOO=1 BAR=2 python3 bin/test_foo.py"

run_hook_writes "bare VAR=val npx vitest recognized (composes with the npx unwrap)" \
  "FOO=1 npx vitest run"

# Combined shape: a bare assignment prefix in front of an env-wrapped hermetic
# run must still resolve through both (assignment strip runs BEFORE the env
# grammar strip / sable-test unwrap).
run_hook_writes "bare VAR=val env -u A bash <suite> (assignment + env wrapper) recognized" \
  "SABLE_LIB_DIR=/scratch env -u CLAUDE_AGENT_NAME bash hooks/test/test-foo.sh"

run_hook_writes "bare VAR=val sable-test <suite> (assignment + sable-test wrapper) recognized" \
  "SABLE_LIB_DIR=/scratch sable-test bash hooks/test/test-foo.sh"

# Negative: a bare assignment in front of a NON-test command still records
# nothing (the strip exposes the real command, which is correctly ignored).
run_hook_silent "bare VAR=val in front of a non-test command not recognized" \
  "SABLE_LIB_DIR=/scratch python3 deploy.py"

# Negative: an assignment-only segment (no command at all) records nothing --
# the shell would set the var and run nothing.
run_hook_silent "assignment-only segment (no command) not recognized" \
  "SABLE_LIB_DIR=/scratch"

# repo-tagging must survive the strip: a bare-prefixed same-repo run is still
# tagged with the hook's own cwd, and the recorded CMD is the REAL command
# (the VAR=value token stripped), not the assignment.
VARPREFIX_SID="tdd-ev-varprefix-$$-$RANDOM"
VARPREFIX_EV="/tmp/tdd-evidence-${VARPREFIX_SID}"
rm -f "$VARPREFIX_EV"
python3 -c "
import json, sys
print(json.dumps({'tool_input': {'command': 'SABLE_LIB_DIR=/scratch pytest tests/'}, 'session_id': sys.argv[1], 'cwd': '/home/ddc/dev-environment/market-brief-package'}))
" "$VARPREFIX_SID" | bash "$HOOK" >/dev/null 2>&1 || true
if grep -q 'REPO=/home/ddc/dev-environment/market-brief-package CMD=pytest tests/$' "$VARPREFIX_EV" 2>/dev/null; then
  pass "bare VAR=val prefix: evidence tags cwd and records the stripped real command"
else
  fail "bare VAR=val prefix: evidence tags cwd and records the stripped real command" "got: $(cat "$VARPREFIX_EV" 2>/dev/null)"
fi
rm -f "$VARPREFIX_EV"

# ---------- INTEGRATION: bare VAR=val hermetic run + real tdd-gate.sh close ----------
# The acceptance criterion for SABLE-x8mx7: a worker who scopes a test run with
# the mandated sandbox-pinning form ('SABLE_LIB_DIR=<scratch> python3 -m pytest')
# must be able to close its bead afterward. Real writer hook + real gate hook,
# same session, no mocks -- the exact composition the unit tests cannot prove.
VP_STUB_DIR=$(mktemp -d)
cat > "$VP_STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"SABLE-stub","notes":"integration stub"}]
JSON
  exit 0
fi
exit 0
EOF
chmod +x "$VP_STUB_DIR/bd"
VP_GATE_HOOK_FILE="$(cd "$(dirname "$0")/.." && pwd)/tdd-gate.sh"

VP_SID="tdd-ev-varprefix-gate-$$-$RANDOM"
VP_EV="/tmp/tdd-evidence-${VP_SID}"
rm -f "$VP_EV"
make_input "SABLE_LIB_DIR=/scratch python3 -m pytest tests/test_foo.py" "$VP_SID" | bash "$HOOK" >/dev/null 2>&1 || true
if [ -s "$VP_EV" ]; then
  pass "bare VAR=val hermetic run: real writer hook records evidence"
else
  fail "bare VAR=val hermetic run: real writer hook records evidence" "no $VP_EV"
fi
VP_GATE_OUT=$(make_input 'bd close SABLE-stub SABLE-other' "$VP_SID" | env PATH="$VP_STUB_DIR:$PATH" bash "$VP_GATE_HOOK_FILE" 2>/dev/null)
if [ -z "$VP_GATE_OUT" ]; then
  pass "bare VAR=val hermetic run: real gate ALLOWS the close (SABLE-x8mx7 acceptance criterion)"
else
  fail "bare VAR=val hermetic run: real gate ALLOWS the close" "gate denied: $VP_GATE_OUT"
fi
rm -f "$VP_EV"
rm -rf "$VP_STUB_DIR"

# ---------- INTEGRATION: env-u hermetic run + real tdd-gate.sh close ----------
# The acceptance criterion for SABLE-j10xa/rzsb.5: a worker that follows the
# fleet's mandated hermetic-run contract exactly must be able to close its
# bead afterward. Real writer hook + real gate hook, same session, no mocks.
EU_STUB_DIR=$(mktemp -d)
cat > "$EU_STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"SABLE-stub","notes":"integration stub"}]
JSON
  exit 0
fi
exit 0
EOF
chmod +x "$EU_STUB_DIR/bd"
GATE_HOOK_FILE="$(cd "$(dirname "$0")/.." && pwd)/tdd-gate.sh"

EU_SID="tdd-ev-envu-gate-$$-$RANDOM"
EU_EV="/tmp/tdd-evidence-${EU_SID}"
rm -f "$EU_EV"
make_input "env -u CLAUDE_AGENT_NAME -u SABLE_WORKER_PANE -u SABLE_BEAD bash hooks/test/test-foo.sh" "$EU_SID" | bash "$HOOK" >/dev/null 2>&1 || true
if [ -s "$EU_EV" ]; then
  pass "env-u hermetic run: real writer hook records evidence"
else
  fail "env-u hermetic run: real writer hook records evidence" "no $EU_EV"
fi
EU_GATE_OUT=$(make_input 'bd close SABLE-stub SABLE-other' "$EU_SID" | env PATH="$EU_STUB_DIR:$PATH" bash "$GATE_HOOK_FILE" 2>/dev/null)
if [ -z "$EU_GATE_OUT" ]; then
  pass "env-u hermetic run: real gate ALLOWS the close (SABLE-j10xa acceptance criterion)"
else
  fail "env-u hermetic run: real gate ALLOWS the close" "gate denied: $EU_GATE_OUT"
fi
rm -f "$EU_EV"
rm -rf "$EU_STUB_DIR"

# ---------- SABLE-5lli.1: S2 prerequisite -- PASS/FAIL exit-status field ----------
# tdd-evidence.sh fires PreToolUse (before the command runs), so it has no
# result to report on that path -- STATUS stays omitted, preserving today's
# CMD=-only evidence-registration behavior exactly. When the incoming payload
# DOES carry a completed result (a PostToolUse-shaped event, or here a
# synthetic fixture standing in for one), the same command must be recorded
# as either STATUS=PASS or STATUS=FAIL depending on its real exit status --
# never inferred from the command text, only from the result data.

make_input_result() {
  # $1 = command, $2 = session_id, $3 = python dict literal for the result
  # payload (e.g. "{'exit_code': 1}"), $4 = result key name
  # ('tool_response' or 'tool_result')
  python3 -c "
import json, sys
result = eval(sys.argv[3])
d = {'tool_input': {'command': sys.argv[1]}, 'session_id': sys.argv[2], sys.argv[4]: result}
print(json.dumps(d))
" "$1" "$2" "$3" "$4"
}

# run_hook_status <name> <command> <result-dict-literal> <result-key> <expected STATUS|none>
run_hook_status() {
  local name="$1" command="$2" result_literal="$3" result_key="$4" expected="$5"
  local sid evidence
  sid=$(fake_session)
  evidence="/tmp/tdd-evidence-${sid}"
  rm -f "$evidence"
  make_input_result "$command" "$sid" "$result_literal" "$result_key" | bash "$HOOK" >/dev/null 2>&1 || true
  if [ "$expected" = "none" ]; then
    if [ -s "$evidence" ] && ! grep -q 'STATUS=' "$evidence"; then
      pass "$name"
    else
      fail "$name" "got: $(cat "$evidence" 2>/dev/null || echo '(missing)')"
    fi
  else
    if grep -qF "STATUS=$expected" "$evidence" 2>/dev/null; then
      pass "$name"
    else
      fail "$name" "expected STATUS=$expected, got: $(cat "$evidence" 2>/dev/null || echo '(missing)')"
    fi
  fi
  rm -f "$evidence"
}

run_hook_status "failing test command (tool_response.exit_code=1) records FAIL" \
  "pytest tests/" "{'exit_code': 1}" "tool_response" "FAIL"

run_hook_status "passing test command (tool_response.exit_code=0) records PASS" \
  "pytest tests/" "{'exit_code': 0}" "tool_response" "PASS"

run_hook_status "failing test command via tool_result.exit_code=1 records FAIL" \
  "npm test" "{'exit_code': 1}" "tool_result" "FAIL"

run_hook_status "passing test command via tool_result.exit_code=0 records PASS" \
  "npm test" "{'exit_code': 0}" "tool_result" "PASS"

run_hook_status "alt key exitCode=1 records FAIL" \
  "bash hooks/test/test-foo.sh" "{'exitCode': 1}" "tool_response" "FAIL"

run_hook_status "boolean success=False records FAIL" \
  "pytest tests/" "{'success': False}" "tool_response" "FAIL"

run_hook_status "boolean success=True records PASS" \
  "pytest tests/" "{'success': True}" "tool_response" "PASS"

# non-test command: still records NOTHING regardless of a present, failing
# result -- STATUS is only ever attached to a command that already matched
# the existing evidence-registration rules; it never becomes a NEW way for
# an unrelated command to register.
FAILNONTEST_SID="tdd-ev-failnontest-$$-$RANDOM"
FAILNONTEST_EV="/tmp/tdd-evidence-${FAILNONTEST_SID}"
rm -f "$FAILNONTEST_EV"
make_input_result "git status" "$FAILNONTEST_SID" "{'exit_code': 1}" "tool_response" | bash "$HOOK" >/dev/null 2>&1 || true
if [ ! -s "$FAILNONTEST_EV" ]; then
  pass "non-test command with a failing result present still records neither"
else
  fail "non-test command with a failing result present still records neither" "unexpected evidence: $(cat "$FAILNONTEST_EV")"
fi
rm -f "$FAILNONTEST_EV"

# IRON-RULE regression: a PreToolUse-shaped payload (no result field at all,
# today's exact production shape) still records the plain CMD= line with NO
# STATUS= suffix -- the pass/fail field is additive, not a replacement.
run_hook_status "PreToolUse-shaped payload (no result yet) omits STATUS entirely" \
  "pytest tests/" "{}" "__no_such_key__" "none"

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
