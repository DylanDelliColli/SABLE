#!/usr/bin/env bash
# Contract for the shared one-process JSON encoder used by high-volume hook
# suites.  It preserves Python json.dumps semantics while deleting one Python
# startup per synthetic hook payload.

set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="$DIR/lib-json-input-encoder.sh"

if [ ! -r "$LIB" ]; then
  echo "FAIL: shared JSON encoder library is absent: $LIB"
  exit 2
fi
# shellcheck source=lib-json-input-encoder.sh
. "$LIB"

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

ROOT="$(mktemp -d)"
cleanup() {
  sable_json_encoder_stop >/dev/null 2>&1 || true
  rm -rf "$ROOT"
}
trap cleanup EXIT

sable_json_encoder_start "$ROOT"
ENCODER_PID="$SABLE_JSON_ENCODER_PID"

COMMAND=$'git push "snowman ☃"\nnext\\tail\tend'
CWD=$'/tmp/post space/é'
STDOUT=$'out\n"quoted"\\tail'
STDERR=$'err\t☃'
AGENT_TYPE=$'tarzan"\\\n☃'

PRE_ORDINARY=$(sable_json_encoder_encode \
  pre "$COMMAND" "$CWD" "" "" "" "" 0)
PRE_ORDINARY_EXPECTED='{"tool_input": {"command": "git push \"snowman \u2603\"\nnext\\tail\tend"}, "cwd": "/tmp/post space/\u00e9"}'

PRE_MEMBER=$(sable_json_encoder_encode \
  pre "$COMMAND" "$CWD" "" "" 'opaque-member-id' "$AGENT_TYPE" 1)
PRE_MEMBER_EXPECTED='{"tool_input": {"command": "git push \"snowman \u2603\"\nnext\\tail\tend"}, "cwd": "/tmp/post space/\u00e9", "agent_id": "opaque-member-id", "agent_type": "tarzan\"\\\n\u2603"}'

POST_EMPTY=$(sable_json_encoder_encode \
  post 'git push' '/tmp/empty' '' '' '' '' 0)
POST_EMPTY_EXPECTED='{"tool_input": {"command": "git push"}, "cwd": "/tmp/empty", "tool_response": {"stdout": "", "stderr": ""}}'

POST_ORDINARY=$(sable_json_encoder_encode \
  post "$COMMAND" "$CWD" "$STDOUT" "$STDERR" "" "" 0)
POST_ORDINARY_EXPECTED='{"tool_input": {"command": "git push \"snowman \u2603\"\nnext\\tail\tend"}, "cwd": "/tmp/post space/\u00e9", "tool_response": {"stdout": "out\n\"quoted\"\\tail", "stderr": "err\t\u2603"}}'

POST_MEMBER=$(sable_json_encoder_encode \
  post "$COMMAND" "$CWD" "$STDOUT" "$STDERR" 'opaque-member-id' "$AGENT_TYPE" 1)
POST_MEMBER_EXPECTED='{"agent_id": "opaque-member-id", "agent_type": "tarzan\"\\\n\u2603", "tool_input": {"command": "git push \"snowman \u2603\"\nnext\\tail\tend"}, "cwd": "/tmp/post space/\u00e9", "tool_response": {"stdout": "out\n\"quoted\"\\tail", "stderr": "err\t\u2603"}}'

if [ "$PRE_ORDINARY" = "$PRE_ORDINARY_EXPECTED" ] \
   && [ "$PRE_MEMBER" = "$PRE_MEMBER_EXPECTED" ] \
   && [ "$POST_EMPTY" = "$POST_EMPTY_EXPECTED" ] \
   && [ "$POST_ORDINARY" = "$POST_ORDINARY_EXPECTED" ] \
   && [ "$POST_MEMBER" = "$POST_MEMBER_EXPECTED" ]; then
  pass "pre/post ordinary/member schemas preserve exact escaping and empty-field semantics"
else
  fail "pre/post ordinary/member schemas preserve exact escaping and empty-field semantics" \
    "pre=$PRE_ORDINARY member=$PRE_MEMBER empty=$POST_EMPTY post=$POST_ORDINARY post-member=$POST_MEMBER"
fi

STARTS=$(grep -c '^start$' "$SABLE_JSON_ENCODER_TRACE")
REQUESTS=$(grep -c '^request$' "$SABLE_JSON_ENCODER_TRACE")
if [ "$STARTS" -eq 1 ] && [ "$REQUESTS" -eq 5 ]; then
  pass "one encoder process serves all five schema/escaping requests"
else
  fail "one encoder process serves all five schema/escaping requests" \
    "starts=$STARTS requests=$REQUESTS"
fi

if kill -0 "$ENCODER_PID" 2>/dev/null; then
  pass "encoder is alive while requests are being served"
else
  fail "encoder is alive while requests are being served"
fi

sable_json_encoder_stop
if kill -0 "$ENCODER_PID" 2>/dev/null; then
  fail "encoder stop reaps the process instead of orphaning it" "pid=$ENCODER_PID is still alive"
else
  pass "encoder stop reaps the process instead of orphaning it"
fi

echo
echo "Tests: $((PASS + FAIL)) | Passed: $PASS | Failed: $FAIL"
[ "$FAIL" -eq 0 ]
