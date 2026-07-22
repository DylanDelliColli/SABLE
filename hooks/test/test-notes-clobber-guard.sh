#!/usr/bin/env bash
# test-notes-clobber-guard.sh — UNIT tests for
# hooks/multi-manager/notes-clobber-guard.sh (SABLE-sm269).
#
# Table-driven over the hook's decision: crafted PreToolUse JSON on stdin, a
# stubbed `bd` on PATH standing in for the bead store, and an EXACT assertion
# on the emitted decision (the parsed permissionDecision value, or the absence
# of any output at all for a silent allow) — not a substring sniff, so a hook
# that emitted a differently-shaped JSON blob would fail here rather than pass
# on an incidental match.
#
# The real-bd half of this bead's spec (does the CONTENT actually survive?)
# lives in hooks/test/test-notes-clobber-guard-e2e.sh. Deciding correctly and
# persisting correctly are different claims; this file only makes the first.
#
# Run with:
#   bash hooks/test/test-notes-clobber-guard.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/notes-clobber-guard.sh"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

if [ ! -f "$HOOK" ]; then
  fail "hook present at hooks/multi-manager/notes-clobber-guard.sh" "not found at $HOOK"
  echo "Tests: 1 | Passed: 0 | Failed: 1"
  exit 1
fi

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

# ---------------------------------------------------------------------------
# Stub bd: a fixture bead store. `bd show <id> --json` prints the same
# single-element list shape the real bd emits; unknown ids exit non-zero with
# nothing on stdout (the unresolvable-lookup case).
# ---------------------------------------------------------------------------
STUB_DIR="$FIXTURE_DIR/bin"
mkdir -p "$STUB_DIR"
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
# Only `bd show <id> --json` is exercised by the guard.
[ "${1:-}" = "show" ] || exit 1
case "${2:-}" in
  SABLE-nonempty)  printf '[{"id":"SABLE-nonempty","notes":"WIP-CLAIMS: a.sh,b.sh"}]\n' ;;
  SABLE-nonempty2) printf '[{"id":"SABLE-nonempty2","notes":"a second bead with real notes"}]\n' ;;
  SABLE-empty)     printf '[{"id":"SABLE-empty","notes":""}]\n' ;;
  SABLE-nokey)     printf '[{"id":"SABLE-nokey"}]\n' ;;
  SABLE-blank)     printf '[{"id":"SABLE-blank","notes":"   \\n  "}]\n' ;;
  SABLE-badjson)   printf 'this is not json\n' ;;
  *)               exit 1 ;;
esac
STUB
chmod +x "$STUB_DIR/bd"

json() { # <command> -> PreToolUse hook input
  python3 -c "
import json, sys
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': sys.argv[1]}, 'hook_event_name': 'PreToolUse'}))
" "$1"
}

# run_hook <command> [--no-bd] -> stdout of the hook
run_hook() {
  local cmd="$1" nobd="${2:-}" path
  if [ "$nobd" = "--no-bd" ]; then
    path="$FIXTURE_DIR/empty-bin:/usr/bin:/bin"
    mkdir -p "$FIXTURE_DIR/empty-bin"
  else
    path="$STUB_DIR:$PATH"
  fi
  json "$cmd" | env PATH="$path" bash "$HOOK" 2>/dev/null
}

# decision_of <hook stdout> -> the exact permissionDecision string, or
# '<none>' for no output at all, or '<malformed>' if it is not the documented
# hookSpecificOutput shape.
decision_of() {
  printf '%s' "$1" | python3 -c "
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    print('<none>'); sys.exit(0)
try:
    d = json.loads(raw)
except Exception:
    print('<malformed>'); sys.exit(0)
hso = d.get('hookSpecificOutput')
if not isinstance(hso, dict) or hso.get('hookEventName') != 'PreToolUse':
    print('<malformed>'); sys.exit(0)
print(hso.get('permissionDecision') or '<malformed>')
"
}

# assert_decision <label> <expected: deny|allow|<none>> <command> [--no-bd]
assert_decision() {
  local label="$1" expected="$2" cmd="$3" nobd="${4:-}" out got
  out="$(run_hook "$cmd" "$nobd")"
  got="$(decision_of "$out")"
  if [ "$got" = "$expected" ]; then
    pass "$label"
  else
    fail "$label" "expected decision '$expected', got '$got' (raw: ${out:-<empty>})"
  fi
}

# assert_mentions <label> <needle> <command>
assert_mentions() {
  local label="$1" needle="$2" cmd="$3" out
  out="$(run_hook "$cmd")"
  if printf '%s' "$out" | grep -qF -- "$needle"; then
    pass "$label"
  else
    fail "$label" "output did not mention '$needle': ${out:-<empty>}"
  fi
}

# ===========================================================================
# The core table: the four decisions the bead's spec names.
# ===========================================================================
assert_decision "destructive --notes against NON-EMPTY notes is DENIED" \
  deny 'bd update SABLE-nonempty --notes "replacement text"'

assert_decision "--notes against EMPTY notes is ALLOWED (destroys nothing, stays silent)" \
  '<none>' 'bd update SABLE-empty --notes "replacement text"'

assert_decision "--append-notes against NON-EMPTY notes is ALLOWED (never denied)" \
  '<none>' 'bd update SABLE-nonempty --append-notes "extra text"'

assert_decision "unresolvable bead id fails OPEN (allow, not deny)" \
  allow 'bd update --notes "replacement text"'

assert_decision "non-bd command is untouched" \
  '<none>' 'git status'

# ===========================================================================
# Fail-open must be LOUD, not silent (Standing Discipline 7). A guard that
# allows silently on error is indistinguishable from one that allowed on
# purpose — that is the whole defect class this hook exists to close.
# ===========================================================================
assert_mentions "unresolvable id reports COULD NOT ASSESS (fail-open is loud)" \
  "COULD NOT ASSESS" 'bd update --notes "replacement text"'

assert_decision "unparseable command line (unbalanced quote) fails OPEN" \
  allow 'bd update "dangling --notes x'

assert_mentions "unparseable command line reports COULD NOT ASSESS" \
  "COULD NOT ASSESS" 'bd update "dangling --notes x'

assert_decision "bead the store cannot resolve fails OPEN" \
  allow 'bd update SABLE-missing --notes "x"'

assert_mentions "unresolvable lookup reports COULD NOT ASSESS and names the bead" \
  "SABLE-missing" 'bd update SABLE-missing --notes "x"'

assert_decision "unparseable bd show output fails OPEN" \
  allow 'bd update SABLE-badjson --notes "x"'

# ===========================================================================
# The deny message has to teach the way out, not just refuse.
# ===========================================================================
assert_mentions "deny message points at --append-notes" \
  "--append-notes" 'bd update SABLE-nonempty --notes "x"'

assert_mentions "deny message names the bead whose notes would be destroyed" \
  "SABLE-nonempty" 'bd update SABLE-nonempty --notes "x"'

# ===========================================================================
# Shapes that must not slip past, and shapes that must not over-deny.
# ===========================================================================
assert_decision "--notes=VALUE (equals form) is caught" \
  deny 'bd update SABLE-nonempty --notes=replacement'

assert_decision "bd update behind a shell separator is caught" \
  deny 'cd /tmp && bd update SABLE-nonempty --notes "x"'

assert_decision "bd update behind an env prefix is caught" \
  deny 'env FOO=1 bd update SABLE-nonempty --notes "x"'

assert_decision "other flags before --notes do not hide the target id" \
  deny 'bd update SABLE-nonempty --status open --notes "x"'

assert_decision "multi-id update denies when ANY target has notes" \
  deny 'bd update SABLE-empty SABLE-nonempty2 --notes "x"'

assert_decision "multi-id update stays silent when NO target has notes" \
  '<none>' 'bd update SABLE-empty SABLE-nokey --notes "x"'

assert_decision "missing notes key is treated as empty (no over-deny)" \
  '<none>' 'bd update SABLE-nokey --notes "x"'

assert_decision "whitespace-only notes carry nothing worth protecting" \
  '<none>' 'bd update SABLE-blank --notes "x"'

assert_decision "bd show is not a write and is untouched" \
  '<none>' 'bd show SABLE-nonempty --json'

assert_decision "bd create --notes is not an update and is untouched" \
  '<none>' 'bd create --title="t" --notes "x"'

assert_decision "--append-notes plus --notes in one invocation is not denied" \
  '<none>' 'bd update SABLE-nonempty --append-notes "a" --notes "b"'

assert_decision "no bd on PATH exits silently (the guarded write cannot happen either)" \
  '<none>' 'bd update SABLE-nonempty --notes "x"' --no-bd

# ===========================================================================
# Empty / degenerate hook input must never crash or deny (#16047 empty-stdin
# degradation is a live upstream condition in this fleet).
# ===========================================================================
OUT="$(printf '' | env PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)"
if [ -z "$OUT" ]; then pass "empty stdin exits silently"; else fail "empty stdin exits silently" "got: $OUT"; fi

OUT="$(printf 'not json' | env PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)"
if [ -z "$OUT" ]; then pass "non-JSON stdin exits silently"; else fail "non-JSON stdin exits silently" "got: $OUT"; fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
