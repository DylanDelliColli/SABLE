#!/usr/bin/env bash
# test-tdd-gate.sh — Unit tests for tdd-gate.sh, focused on the bd-close
# argument parser that decides whether [no-test] escape hatch applies.
#
# The gate parses the close command, strips flags, counts remaining tokens
# (bead IDs), and only consults the [no-test] notes hatch when count == 1.
# Any flag form that fails to strip silently inflates the count and
# bypasses the hatch — that's the regression these tests guard against.
#
# Run with:
#   bash hooks/test/test-tdd-gate.sh
#
# Strategy:
#   - Each test invokes the gate with a synthetic single-bead close command.
#   - Setup creates a fake bead lookup by writing a fixture JSON (we can't
#     mock `bd show`, so we test parsing behavior by checking exit code +
#     output: the gate exits 0 on the no-test path. If parsing strips the
#     flag correctly, count==1, the gate consults bd show, finds [no-test],
#     and exits 0 silently. If parsing fails, count==2, the hatch is
#     skipped and the gate proceeds to the evidence check.
#   - We use a session id that has no evidence file, so a no-evidence path
#     produces a deny JSON. Then: silence == hatch worked, deny JSON ==
#     hatch was skipped (parser missed the flag).

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/tdd-gate.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

# Need a real, currently-open bead with [no-test] in notes for the hatch to
# fire. Use the bead this very work is being done under (SABLE-1n2) — its
# notes carry [no-test] context once we mark it. To stay self-contained,
# the test creates a throwaway test bead, marks notes, runs assertions,
# then closes it.

# Tests run as part of bd commits; can't create real beads in CI. Instead,
# stub `bd` on PATH to return a canned JSON response. The gate calls
# `bd show "$BEAD_ID" --json`; we intercept it.

PASS=0
FAIL=0
FAIL_NAMES=""

# Make a temp bin/bd that returns a canned `[no-test]` notes payload for
# any single-arg `show ... --json` call.
STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT

cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
# Stub bd that returns a single-bead [no-test] notes for show --json
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"SABLE-stub","notes":"[no-test] stub bead for parser tests"}]
JSON
  exit 0
fi
exit 0
EOF
chmod +x "$STUB_DIR/bd"

make_input() {
  # $1 = command, $2 = session_id (use a junk id with no evidence file)
  python3 -c "
import json, sys
print(json.dumps({'tool_input': {'command': sys.argv[1]}, 'session_id': sys.argv[2]}))
" "$1" "$2"
}

# Fresh fake session id per test; never collides with real evidence.
fake_session() {
  printf 'tdd-gate-test-%s-%s' "$$" "$RANDOM"
}

# run_gate <command> → <exit-code><tab><stdout>
run_gate() {
  local command="$1"
  local sid
  sid=$(fake_session)
  local evidence="/tmp/tdd-evidence-${sid}"
  rm -f "$evidence"  # ensure no-evidence path
  local out
  out=$(make_input "$command" "$sid" | env PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)
  echo -n "$out"
}

# assert_hatch_used <name> <command>
# Expects silent allow (gate parsed args correctly, found [no-test], exited 0).
assert_hatch_used() {
  local name="$1" command="$2"
  local out
  out=$(run_gate "$command")
  if [ -z "$out" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: silent allow (parser stripped the flag, hatch consulted)"
    echo "  Got:      $out"
  fi
}

# assert_hatch_skipped <name> <command>
# Expects deny JSON (parser failed to strip, count > 1, hatch skipped, no
# evidence → deny). Used to confirm baseline state of bugs PRE-fix.
assert_hatch_skipped() {
  local name="$1" command="$2"
  local out
  out=$(run_gate "$command")
  if echo "$out" | grep -q '"permissionDecision": "deny"'; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: deny JSON (parser missed flag, hatch skipped)"
    echo "  Got:      $out"
  fi
}

# ---------- Currently-working forms (regression guard) ----------

assert_hatch_used "bare ID, no flags"                 "bd close SABLE-stub"
assert_hatch_used "--reason=text (equals, unquoted)"  'bd close SABLE-stub --reason=because'
assert_hatch_used "--reason=\"text\" (equals, quoted)" 'bd close SABLE-stub --reason="because of foo"'

# ---------- New: space-separated flag forms (SABLE-1n2 fix) ----------

assert_hatch_used "--reason text (space, unquoted)" \
  'bd close SABLE-stub --reason because'

assert_hatch_used "--reason \"text\" (space, double-quoted)" \
  'bd close SABLE-stub --reason "because of foo"'

assert_hatch_used "--reason '\''text'\'' (space, single-quoted)" \
  "bd close SABLE-stub --reason 'because of foo'"

assert_hatch_used "multiple flags, mixed quote styles" \
  'bd close SABLE-stub --reason "first" --suggest-next'

# ---------- Multi-bead close path (intentionally skipped — hatch only fires for single-ID) ----------

assert_hatch_skipped "two beads with --reason= still routes to evidence check" \
  'bd close SABLE-stub SABLE-other --reason=cleanup'

assert_hatch_skipped "two beads with --reason \"text\" still routes to evidence check" \
  'bd close SABLE-stub SABLE-other --reason "cleanup batch"'

# ---------- Piped / redirected / chained single-bead close (SABLE-sqz fix) ----------
# Pipes, redirects, and command chains in the close command must not inflate
# the bead-ID count. Only tokens shaped like bead IDs (e.g. SABLE-stub,
# SABLE-rjv.1, BEADS-abc) should be counted.

assert_hatch_used "single bead piped to tail" \
  'bd close SABLE-stub | tail -3'

assert_hatch_used "single bead redirected stderr to stdout" \
  'bd close SABLE-stub 2>&1'

assert_hatch_used "single bead piped + tail with stderr merge" \
  'bd close SABLE-stub 2>&1 | tail -3'

assert_hatch_used "single bead redirected to file" \
  'bd close SABLE-stub > /tmp/out'

assert_hatch_used "single bead chained with &&" \
  'bd close SABLE-stub && echo done'

assert_hatch_used "single bead chained with semicolon" \
  'bd close SABLE-stub ; echo done'

assert_hatch_used "single bead with --reason and a pipe" \
  'bd close SABLE-stub --reason "shipped" | tail -3'

# Multi-bead with pipes still routes to evidence check (preserves existing
# behavior — pipes don't magically convert multi-bead into single-bead).
assert_hatch_skipped "two beads piped to tail still routes to evidence check" \
  'bd close SABLE-stub SABLE-other 2>&1 | tail -3'

# Dotted child bead IDs are recognized (SABLE-rjv.1, SABLE-rjv.12).
assert_hatch_used "dotted child bead ID alone" \
  'bd close SABLE-rjv.1'

assert_hatch_used "dotted child bead ID piped" \
  'bd close SABLE-rjv.1 2>&1'

# ---------- New: lowercase-prefix rigs (SABLE-i2m fix) ----------
# Bead prefixes can be lowercase (twine-*, chess-*) on some rigs.
# The ID_PATTERN regex must accept any-case prefixes so the [no-test]
# escape hatch fires for single-close on lowercase IDs.

assert_hatch_used "lowercase bare ID" \
  'bd close twine-stub'

assert_hatch_used "lowercase ID with --reason" \
  'bd close twine-stub --reason "docs only"'

assert_hatch_used "lowercase ID piped" \
  'bd close twine-stub 2>&1 | tail -3'

assert_hatch_used "lowercase dotted child ID" \
  'bd close chess-ab.1'

assert_hatch_skipped "two lowercase beads still route to evidence check" \
  'bd close twine-stub twine-other --reason=cleanup'

# ---------- New: multi-hyphen prefix rigs (market-brief-package-* fix) ----------
# Monorepo rigs use hyphenated prefixes (market-brief-package-*). The ID_PATTERN
# regex must bind the suffix to the LAST hyphen segment so the [no-test] escape
# hatch fires for single-close on multi-hyphen IDs. Before the fix the prefix
# class excluded hyphens, so these IDs matched zero tokens, ID_COUNT was 0, the
# hatch was skipped, and every non-code close was wrongly denied.

assert_hatch_used "multi-hyphen bare ID" \
  'bd close market-brief-package-stub'

assert_hatch_used "multi-hyphen ID with --reason" \
  'bd close market-brief-package-stub --reason "docs only"'

assert_hatch_used "multi-hyphen ID piped" \
  'bd close market-brief-package-stub 2>&1 | tail -3'

assert_hatch_used "multi-hyphen dotted child ID" \
  'bd close market-brief-package-kqnu.3'

assert_hatch_skipped "two multi-hyphen beads still route to evidence check" \
  'bd close market-brief-package-stub market-brief-package-other --reason=cleanup'

# ---------- New: flag values that look like IDs (SABLE-3uw / SABLE-9we fix) ----------
# Flag values such as 'docs-only' or 'shipped-v2' match the bead-ID shape
# (PREFIX-suffix). They must NOT inflate ID_COUNT.

assert_hatch_used "single bead, --reason docs-only (space-separated)" \
  'bd close SABLE-stub --reason docs-only'

assert_hatch_used "single bead, --reason shipped-v2 (space-separated)" \
  'bd close SABLE-stub --reason shipped-v2'

assert_hatch_used "single bead, --reason \"docs-only\" (space, double-quoted)" \
  'bd close SABLE-stub --reason "docs-only"'

assert_hatch_used "single bead, --reason '\''docs-only'\'' (space, single-quoted)" \
  "bd close SABLE-stub --reason 'docs-only'"

assert_hatch_used "single bead, multiple flags with flag-value IDs" \
  'bd close SABLE-stub --reason docs-only --suggest-next follow-up-v2'

# Multi-bead close with flag values must still route to evidence check
assert_hatch_skipped "two beads + flag value still routes to evidence check" \
  'bd close SABLE-stub SABLE-other --reason docs-only'

# ---------- SABLE-p84b: [no-test] in DESCRIPTION (not notes) fires the hatch ----------
# The escape hatch must scan BOTH the notes and the description field for the
# [no-test] marker. A docs/config bead whose worker put [no-test] in the
# DESCRIPTION (a natural place, and where sable-spawn-worker's auto-prompt
# surfaces bead text) was stranded pre-fix: notes-only scan missed it, the
# close was denied, then mis-reported as success (SABLE-u0c6). These tests use
# dedicated stubs so `bd show --json` returns the marker in only one field.

# Compact pass/fail helpers (also used by the sections below).
pa_pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
pa_fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

P84B_DIR=$(mktemp -d)

# Stub: [no-test] in DESCRIPTION only, notes empty.
cat > "$P84B_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"SABLE-desconly","notes":"","description":"Update the docs config. [no-test] pure docs change."}]
JSON
  exit 0
fi
exit 0
EOF
chmod +x "$P84B_DIR/bd"

run_gate_stub() { # <command> <stub_dir>
  local command="$1" stub_dir="$2" sid
  sid=$(fake_session)
  rm -f "/tmp/tdd-evidence-${sid}"  # ensure no-evidence path
  make_input "$command" "$sid" | env PATH="$stub_dir:$PATH" bash "$HOOK" 2>/dev/null
}

out=$(run_gate_stub 'bd close SABLE-desconly' "$P84B_DIR")
if [ -z "$out" ]; then
  pa_pass "p84b: [no-test] in DESCRIPTION only (no notes, no evidence) → hatch fires, close allowed"
else
  pa_fail "p84b: [no-test] in DESCRIPTION only → hatch fires" "expected silent allow; got: ${out:-<empty>}"
fi

# Stub: [no-test] in NOTES only, description empty — the pre-existing path must
# keep working after the change.
cat > "$P84B_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"SABLE-notesonly","notes":"[no-test] docs bead","description":"Update the docs."}]
JSON
  exit 0
fi
exit 0
EOF
out=$(run_gate_stub 'bd close SABLE-notesonly' "$P84B_DIR")
if [ -z "$out" ]; then
  pa_pass "p84b: [no-test] in NOTES only still fires the hatch (no regression)"
else
  pa_fail "p84b: [no-test] in NOTES only still fires the hatch" "expected silent allow; got: ${out:-<empty>}"
fi

# Stub: NO marker in EITHER field, no evidence → must still DENY.
cat > "$P84B_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"SABLE-nomarker","notes":"a normal code bead","description":"Implement the feature; needs tests."}]
JSON
  exit 0
fi
exit 0
EOF
out=$(run_gate_stub 'bd close SABLE-nomarker' "$P84B_DIR")
if echo "$out" | grep -q '"permissionDecision": "deny"'; then
  pa_pass "p84b: no [no-test] marker in either field + no evidence → still DENIED"
else
  pa_fail "p84b: no marker in either field → still DENIED" "expected deny JSON; got: ${out:-<empty>}"
fi
rm -rf "$P84B_DIR"

# ---------- SABLE-d72/lcs: per-agent evidence keying ----------
# The gate must read the SAME per-agent key tdd-evidence.sh writes: with agent_id
# present, /tmp/tdd-evidence-<sid>-<agent_id>; without, the session-global file.
# A two-bead close routes past the [no-test] hatch to the evidence check, so we
# exercise the keying directly. Worker A's evidence must NOT let worker B close.
# (pa_pass/pa_fail are defined in the SABLE-p84b section above.)

make_input_agent() {
  # $1 = command, $2 = session_id, $3 = agent_id
  python3 -c "
import json, sys
d = {'tool_input': {'command': sys.argv[1]}, 'session_id': sys.argv[2]}
if sys.argv[3]:
    d['agent_id'] = sys.argv[3]
print(json.dumps(d))
" "$1" "$2" "$3"
}
run_gate_agent() { # <command> <session_id> <agent_id>
  make_input_agent "$1" "$2" "$3" | env PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null
}

PG_SID="tdd-gate-peragent-$$-$RANDOM"
PG_MAIN="/tmp/tdd-evidence-${PG_SID}"
PG_A="/tmp/tdd-evidence-${PG_SID}-agentA"
PG_B="/tmp/tdd-evidence-${PG_SID}-agentB"
rm -f "$PG_MAIN" "$PG_A" "$PG_B"
echo "ran tests" > "$PG_A"   # only agent A has evidence
CLOSE2='bd close SABLE-stub SABLE-other'   # two beads → skip [no-test] hatch → evidence check

out=$(run_gate_agent "$CLOSE2" "$PG_SID" "agentA")
if [ -z "$out" ]; then pa_pass "per-agent gate: close under agent A (ran tests) is allowed"; else pa_fail "per-agent gate: close under agent A is allowed" "got: $out"; fi

out=$(run_gate_agent "$CLOSE2" "$PG_SID" "agentB")
if echo "$out" | grep -q '"permissionDecision": "deny"'; then pa_pass "per-agent gate: close under agent B (no own evidence) is BLOCKED — cross-agent leak closed"; else pa_fail "per-agent gate: close under agent B is BLOCKED" "got: ${out:-<empty>}"; fi

out=$(run_gate_agent "$CLOSE2" "$PG_SID" "")
if echo "$out" | grep -q '"permissionDecision": "deny"'; then pa_pass "per-agent gate: main-session close uses the session-global key (agent A evidence does not satisfy it)"; else pa_fail "per-agent gate: main-session uses the session-global key" "got: ${out:-<empty>}"; fi

echo "ran tests" > "$PG_MAIN"
out=$(run_gate_agent "$CLOSE2" "$PG_SID" "")
if [ -z "$out" ]; then pa_pass "per-agent gate: main-session close allowed by session-global evidence (single-agent unchanged)"; else pa_fail "per-agent gate: main-session allowed by session-global evidence" "got: $out"; fi
rm -f "$PG_MAIN" "$PG_A" "$PG_B"

# ---------- market-brief-package-sqcr: companion-repo evidence acceptance ----------
# A cross-repo bead (73t4 pattern: a SABLE-hooks fix tracked as a
# market-brief-package bead) declares its companion repo in notes
# ("Companion repo: <path>"). tdd-evidence.sh tags cross-repo test runs with
# REPO=<path>; the gate must accept that tag even when it lives under a
# DIFFERENT agent_id than the one closing the bead (a nested sub-call may
# have run the companion suite) — but must NOT special-case a bead that
# never declared a companion repo.

CR_STUB_DIR=$(mktemp -d)
cat > "$CR_STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"market-brief-package-companion","notes":"Companion repo: /home/ddc/dev-environment/SABLE"}]
JSON
  exit 0
fi
exit 0
EOF
chmod +x "$CR_STUB_DIR/bd"

CR_SID="tdd-gate-companion-$$-$RANDOM"
CR_CLOSE='bd close market-brief-package-companion market-brief-package-other'
rm -f /tmp/tdd-evidence-"${CR_SID}"*

# (a) companion declared, but no REPO=-tagged evidence anywhere → still denied
CR_OUT=$(make_input "$CR_CLOSE" "$CR_SID" | env PATH="$CR_STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)
if echo "$CR_OUT" | grep -q '"permissionDecision": "deny"'; then
  pa_pass "companion-repo: no matching REPO= evidence anywhere → still denied"
else
  pa_fail "companion-repo: no matching REPO= evidence anywhere → still denied" "got: ${CR_OUT:-<empty>}"
fi

# (b) REPO=-tagged evidence recorded under a DIFFERENT agent_id in the same
# session → accepted, even though the exact-key file for this close is empty.
echo "$(date -Iseconds) REPO=/home/ddc/dev-environment/SABLE CMD=bash hooks/test/test-tree-claim.sh" > "/tmp/tdd-evidence-${CR_SID}-agentX"
CR_OUT2=$(make_input "$CR_CLOSE" "$CR_SID" | env PATH="$CR_STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)
if [ -z "$CR_OUT2" ]; then
  pa_pass "companion-repo: REPO=-tagged evidence under a different agent_id accepted"
else
  pa_fail "companion-repo: REPO=-tagged evidence under a different agent_id accepted" "got: $CR_OUT2"
fi
rm -f /tmp/tdd-evidence-"${CR_SID}"*

# (c) a bead with NO companion-repo declaration gets no special treatment.
cat > "$CR_STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"market-brief-package-plain","notes":"just a normal bead, no companion repo"}]
JSON
  exit 0
fi
exit 0
EOF
CR_SID2="tdd-gate-nocomp-$$-$RANDOM"
rm -f /tmp/tdd-evidence-"${CR_SID2}"*
CR_OUT3=$(make_input 'bd close market-brief-package-plain market-brief-package-other' "$CR_SID2" | env PATH="$CR_STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)
if echo "$CR_OUT3" | grep -q '"permissionDecision": "deny"'; then
  pa_pass "companion-repo: bead without a declaration gets no special treatment (still denied)"
else
  pa_fail "companion-repo: bead without a declaration gets no special treatment" "got: ${CR_OUT3:-<empty>}"
fi
rm -rf "$CR_STUB_DIR"

# ---------- SABLE-h853: scoped-run protocol acceptance ----------
# Operator-approved protocol change (2026-07-13): the full suite no longer
# runs pre-push per worker; workers submit SCOPED evidence (the bead's test
# files plus tests importing the touched modules, coverage off, fail-fast
# on) and the gate must accept it — it must NOT require or imply full-suite
# execution. The full suite becomes the merge-preview ci-verify GitHub
# Actions run's job (chuck-owned), named here so a future reader does not
# reintroduce a full-suite requirement into this hook.
#
# The hatch/evidence CHECK itself is scope-agnostic already (it only checks
# whether an evidence file exists, never what ran) — these assertions lock
# that in as an explicit, documented contract on the hook source itself
# (doc-content regression guard, same pattern as
# test-worker-dispatch-template.sh), plus a behavioral regression guard that
# a real scoped-run evidence line (explicit file args, coverage off,
# fail-fast on) is accepted end-to-end.

hasre_hook() {
  # $1 = name, $2 = ERE pattern to find in the hook's own source
  local name="$1" pattern="$2"
  if grep -qiE -- "$pattern" "$HOOK" 2>/dev/null; then
    pa_pass "$name"
  else
    pa_fail "$name" "missing pattern in $HOOK: $pattern"
  fi
}

hasre_hook "hook documents scoped-run acceptance (not full-suite)" \
  "scoped( pre-push)? (test )?run"
hasre_hook "hook names the merge-preview ci-verify gate as full-suite authority" \
  "merge-preview|ci-verify"
hasre_hook "hook states it does not require full-suite evidence" \
  "not require|never require|scope-agnostic|does not (check|require)"

# Behavioral regression guard: a scoped-run evidence line (explicit test
# file args + coverage-off + fail-fast flags, not a bare full-suite
# invocation) must still satisfy the gate.
SCOPED_SID="tdd-gate-scoped-$$-$RANDOM"
SCOPED_EV="/tmp/tdd-evidence-${SCOPED_SID}"
rm -f "$SCOPED_EV"
echo "$(date -Iseconds) REPO=$(pwd) CMD=pytest hooks/test/test_foo.py tests/test_related.py -x --no-cov" > "$SCOPED_EV"
SCOPED_OUT=$(make_input 'bd close SABLE-stub SABLE-other' "$SCOPED_SID" | env PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)
if [ -z "$SCOPED_OUT" ]; then
  pa_pass "scoped-run evidence (file args + --no-cov -x) is accepted"
else
  pa_fail "scoped-run evidence (file args + --no-cov -x) is accepted" "got: ${SCOPED_OUT:-<empty>}"
fi
rm -f "$SCOPED_EV"

# ---------- SABLE-f6aw: end-to-end — trailing-redirect evidence unblocks close ----------
# Real composition, not a fabricated evidence line: pipe a redirect-suffixed
# test command through the ACTUAL tdd-evidence.sh writer hook, then confirm
# the ACTUAL tdd-gate.sh reader hook allows the close on that evidence.
# Regression guard for the SABLE-f6aw last-token bug: pre-fix, tdd-evidence.sh
# silently dropped 'script.sh 2>&1 | tail -N' commands (the trailing redirect
# token displaced the script path from seg[-1]), so this same sequence denied.

EVIDENCE_HOOK="$(cd "$(dirname "$0")/.." && pwd)/tdd-evidence.sh"
F6AW_SID="tdd-f6aw-e2e-$$-$RANDOM"
F6AW_EV="/tmp/tdd-evidence-${F6AW_SID}"
rm -f "$F6AW_EV"

# Real writer: the actual redirect-suffixed test invocation, through tdd-evidence.sh.
make_input "bash hooks/test/test-foo.sh 2>&1 | tail -8" "$F6AW_SID" | bash "$EVIDENCE_HOOK" >/dev/null 2>&1 || true

# Real reader: a two-bead close routes past the [no-test] hatch to the evidence check.
F6AW_OUT=$(make_input 'bd close SABLE-stub SABLE-other' "$F6AW_SID" | env PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)
if [ -z "$F6AW_OUT" ]; then
  pa_pass "SABLE-f6aw e2e: redirect-suffixed test command recorded by the real writer hook unblocks the real gate hook"
else
  F6AW_EV_CONTENT=$(cat "$F6AW_EV" 2>/dev/null || echo '<missing>')
  pa_fail "SABLE-f6aw e2e: redirect-suffixed test command unblocks the gate" "got: ${F6AW_OUT:-<empty>}; evidence file: $F6AW_EV_CONTENT"
fi
rm -f "$F6AW_EV"

# ---------- SABLE-jfg6.4 (D4): key-agreement invariant across BOTH consumers ----------
# tdd-gate.sh (reader) and tdd-evidence.sh (writer) now derive the evidence path
# from ONE shared function (lib-evidence-key.sh). Same input => identical path,
# so a real green run recorded by the writer always unblocks the reader, and an
# empty session id can never split them onto different paths. Present AND absent
# session; the absent case exercises the ppid fallback. All writer/reader
# invocations here are bare STATEMENTS (reader output captured via a temp file,
# NOT a $(...)-wrapped pipeline — that forks an extra subshell and shifts the
# hook's $PPID), so writer and reader share this shell's $PPID: the real-world
# sibling-of-one-session-controller invariant, hence the same absent-session key.
D4_LIB="$(cd "$(dirname "$0")/.." && pwd)/multi-manager/lib-evidence-key.sh"
D4_EVHOOK="$(cd "$(dirname "$0")/.." && pwd)/tdd-evidence.sh"
d4_key() { bash -c '. "$0"; sable_evidence_key "$1" "$2"' "$D4_LIB" "$1" "$2"; }

# Lib-level forms (this file's verify target must stand alone).
K1=$(d4_key "s1" "a1"); if [ "$K1" = "/tmp/tdd-evidence-s1-a1" ]; then pa_pass "D4 gate-lib: (sid,aid) form"; else pa_fail "D4 gate-lib: (sid,aid) form" "got [$K1]"; fi
K2=$(d4_key "s1" "");   if [ "$K2" = "/tmp/tdd-evidence-s1" ];     then pa_pass "D4 gate-lib: (sid only) form"; else pa_fail "D4 gate-lib: (sid only) form" "got [$K2]"; fi
K3=$(d4_key "" "");     case "$K3" in /tmp/tdd-evidence-ppid-*) pa_pass "D4 gate-lib: absent sid -> ppid fallback" ;; *) pa_fail "D4 gate-lib: absent sid -> ppid fallback" "got [$K3]" ;; esac

# Cross-consumer PRESENT session: writer records, reader allows the close.
D4P_SID="tdd-d4present-$$-$RANDOM"
rm -f "/tmp/tdd-evidence-${D4P_SID}"
make_input "pytest tests/" "$D4P_SID" | bash "$D4_EVHOOK" >/dev/null 2>&1 || true
D4P_TMP=$(mktemp)
make_input 'bd close SABLE-stub SABLE-other' "$D4P_SID" | env PATH="$STUB_DIR:$PATH" bash "$HOOK" > "$D4P_TMP" 2>/dev/null
D4P_OUT=$(cat "$D4P_TMP"); rm -f "$D4P_TMP"
if [ -z "$D4P_OUT" ]; then pa_pass "D4 agreement (present): writer evidence unblocks the reader for the same session"; else pa_fail "D4 agreement (present)" "gate denied: $D4P_OUT"; fi
rm -f "/tmp/tdd-evidence-${D4P_SID}"

# Cross-consumer ABSENT session: writer + reader share $PPID, so the reader
# allows on the writer's ppid-key evidence — the tfkv fix in the hccq trap.
D4A_KEY=$(d4_key "" "")
rm -f "$D4A_KEY"
make_input "pytest tests/" "" | bash "$D4_EVHOOK" >/dev/null 2>&1 || true
D4A_TMP=$(mktemp)
make_input 'bd close SABLE-stub SABLE-other' "" | env PATH="$STUB_DIR:$PATH" bash "$HOOK" > "$D4A_TMP" 2>/dev/null
D4A_OUT=$(cat "$D4A_TMP"); rm -f "$D4A_TMP"
if [ -z "$D4A_OUT" ]; then pa_pass "D4 agreement (absent): writer + reader agree on the ppid key -> close allowed"; else pa_fail "D4 agreement (absent)" "gate denied: $D4A_OUT; key=[$D4A_KEY] exists=$([ -s "$D4A_KEY" ] && echo yes || echo no)"; fi
rm -f "$D4A_KEY"

# Absent-session NEGATIVE space: no writer run -> reader still DENIES (the gate
# does not go permissive just because the session id is absent).
D4N_KEY=$(d4_key "" "")
rm -f "$D4N_KEY"
D4N_TMP=$(mktemp)
make_input 'bd close SABLE-stub SABLE-other' "" | env PATH="$STUB_DIR:$PATH" bash "$HOOK" > "$D4N_TMP" 2>/dev/null
D4N_OUT=$(cat "$D4N_TMP"); rm -f "$D4N_TMP"
if echo "$D4N_OUT" | grep -q '"permissionDecision": "deny"'; then pa_pass "D4 negative (absent): no evidence at the derived key -> reader DENIES"; else pa_fail "D4 negative (absent)" "got: ${D4N_OUT:-<empty>}"; fi
rm -f "$D4N_KEY"

# ---------- SABLE-p84b INTEGRATION: real bd, [no-test] in description ----------
# Exercises the REAL bd --json read (notes + description) through the REAL gate
# hook — no stub bd on PATH. Creates a scratch bead in the shared project Dolt
# db with [no-test] in the DESCRIPTION and empty notes (--sandbox on every write
# so this test never pushes to the shared remote), then closes it. With NO
# session evidence file, the gate must ALLOW the single-bead close because the
# description carries the marker. Keeps a deny leg: a scratch bead with NEITHER
# marker NOR evidence must still be DENIED.

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP (integration): bd not found on PATH"
else
  # (a) allow leg — [no-test] lives in the DESCRIPTION only, notes empty.
  P84B_INT_ID=$(bd create --sandbox \
    --title="[int-test] tdd-gate p84b no-test-in-description scratch" \
    --description="Update hooks/tdd-gate.sh docs. [no-test] pure docs/config change, no runtime surface." \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$P84B_INT_ID" ]; then
    echo "SKIP (integration): could not create scratch bead — bd create output did not match ID pattern"
  else
    echo "Integration: created scratch bead $P84B_INT_ID"
    P84B_INT_SID="tdd-gate-p84b-int-$$-$RANDOM"
    rm -f "/tmp/tdd-evidence-${P84B_INT_SID}"  # ensure the no-evidence path
    # Real gate, real bd (no stub on PATH): marker is only in the description.
    P84B_INT_OUT=$(make_input "bd close $P84B_INT_ID" "$P84B_INT_SID" | bash "$HOOK" 2>/dev/null)
    if [ -z "$P84B_INT_OUT" ]; then
      pa_pass "p84b integration: real bd, [no-test] in description only, no evidence → gate ALLOWS close"
    else
      pa_fail "p84b integration: real bd, [no-test] in description only → gate ALLOWS close" "got: ${P84B_INT_OUT:-<empty>}"
    fi
    bd close "$P84B_INT_ID" --sandbox 2>/dev/null || true
  fi

  # (b) deny leg — real bead with NO marker in either field, no evidence.
  P84B_DENY_ID=$(bd create --sandbox \
    --title="[int-test] tdd-gate p84b no-marker scratch" \
    --description="Implement a real code change in hooks/foo.sh; this needs tests." \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)
  if [ -z "$P84B_DENY_ID" ]; then
    echo "SKIP (integration): could not create deny-leg scratch bead"
  else
    P84B_DENY_SID="tdd-gate-p84b-deny-$$-$RANDOM"
    rm -f "/tmp/tdd-evidence-${P84B_DENY_SID}"
    P84B_DENY_OUT=$(make_input "bd close $P84B_DENY_ID" "$P84B_DENY_SID" | bash "$HOOK" 2>/dev/null)
    if echo "$P84B_DENY_OUT" | grep -q '"permissionDecision": "deny"'; then
      pa_pass "p84b integration: real bd, no marker in either field, no evidence → gate DENIES close"
    else
      pa_fail "p84b integration: real bd, no marker + no evidence → gate DENIES close" "got: ${P84B_DENY_OUT:-<empty>}"
    fi
    bd close "$P84B_DENY_ID" --sandbox 2>/dev/null || true
  fi
fi

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
