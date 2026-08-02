#!/usr/bin/env bash
# test-sable-clean-room-verify-integration.sh — proves a bd/dolt-less PATH
# makes install.sh-touching suites behave correctly: skip-or-stub, not a
# false-pass (SABLE-59zu, narrowed scope v2).
#
# INTEGRATION (real composition): drives the REAL install.sh through the REAL
# bin/sable-clean-room-verify, with real bd/dolt (if any are ambient on this
# machine) genuinely removed from PATH, not shadowed. The standalone
# test-sable-bin-install.sh remains the full write/assertion authority; this
# suite probes only the clean-room composition boundary. Two real behaviors,
# both "not false-pass":
#
#   1. Bare `install.sh` (no clean-room handling of its own) FAILS LOUD at
#      Step 1/8 with an honest "bd is not on PATH" — it does not silently
#      report success while having verified nothing.
#   2. A purpose-built install-aware probe handles bd absence via its own stub
#      and traverses the real installer's dry-run through Step 8. The same
#      probe without that stub must fail at Step 1 (planted negative control).
#
# Run with:
#   bash hooks/test/test-sable-clean-room-verify-integration.sh
#
# sable-test-load: nested-runner -- defining E2E proves the real installer suite under scrubbed PATH

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$REPO/bin/sable-clean-room-verify"
INSTALL_SH="$REPO/install.sh"
BIN_INSTALL_SUITE="$REPO/hooks/test/test-sable-bin-install.sh"
SHELL_RUN_SET="$REPO/.github/ci/shell-run-set.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$TOOL" ] || { fail "bin/sable-clean-room-verify exists and is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "bin/sable-clean-room-verify exists and is executable"
[ -f "$INSTALL_SH" ] || { fail "install.sh exists"; echo "Tests: 2 | Passed: 1 | Failed: 1"; exit 1; }
[ -f "$BIN_INSTALL_SUITE" ] || { fail "hooks/test/test-sable-bin-install.sh exists"; echo "Tests: 2 | Passed: 1 | Failed: 1"; exit 1; }
[ -f "$SHELL_RUN_SET" ] || { fail ".github/ci/shell-run-set.sh exists"; echo "Tests: 2 | Passed: 1 | Failed: 1"; exit 1; }

# ---------- precondition: the scrubbed PATH genuinely lacks bd/dolt ----------
out="$(bash "$TOOL" bash -c 'command -v bd >/dev/null 2>&1 && echo STILL-HAS-BD || echo bd-gone; command -v dolt >/dev/null 2>&1 && echo STILL-HAS-DOLT || echo dolt-gone' 2>&1)"
if printf '%s' "$out" | grep -q "bd-gone" && printf '%s' "$out" | grep -q "dolt-gone"; then
    pass "precondition: bd and dolt are genuinely unreachable under the scrubbed PATH"
else
    fail "precondition: bd and dolt are genuinely unreachable under the scrubbed PATH" "$out"
fi

# ---------- 1. bare install.sh: no clean-room handling -> fails LOUD, not a false-pass ----------
# --from-here bypasses the (unrelated) linked-worktree refusal so Step 1/8's
# bd check is what's actually exercised. HOME is a throwaway dir as a
# belt-and-suspenders guard: install.sh does no filesystem writes before its
# bd check (verified by reading the script), but this keeps the assertion
# true even if that ordering ever changes.
TMPHOME="$(mktemp -d)"
out="$(HOME="$TMPHOME" bash "$TOOL" bash "$INSTALL_SH" --from-here 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qi "bd (beads) is not on PATH"; then
    pass "bare install.sh under a bd-less PATH fails LOUD with an honest message (not a false-pass)"
else
    fail "bare install.sh under a bd-less PATH fails LOUD with an honest message (not a false-pass)" "rc=$rc out=${out:0:400}"
fi
if [ ! -e "$TMPHOME/.claude" ]; then
    pass "the failed run wrote nothing to HOME/.claude"
else
    fail "the failed run wrote nothing to HOME/.claude"
fi
rm -rf "$TMPHOME"

# TDD guard for SABLE-y4nom.7.7: this integration must not execute the full
# bin-install authority a second time. That suite remains a standalone ALLOW
# entry; this file owns only the clean-room composition boundary.
_nested_prefix='out="$('
_nested_target='BIN_INSTALL_SUITE'
nested_full_invocations="$(awk -v prefix="$_nested_prefix" -v target="$_nested_target" \
    'index($0, prefix) && index($0, target) { n++ } END { print n+0 }' "$0")"
if [ "$nested_full_invocations" -eq 0 ]; then
    pass "clean-room integration invokes the standalone full bin-install suite exactly zero times"
else
    fail "clean-room integration invokes the standalone full bin-install suite exactly zero times" \
         "nested_full_invocations=$nested_full_invocations"
fi

# The full write/assertion suite is still an independent shell authority. This
# integration no longer re-runs it; pinning its single ALLOW identity here keeps
# the decomposition reviewable and prevents an optimization-by-omission.
standalone_allow_count="$(awk '/^ALLOW=\(/,/^\)/' "$SHELL_RUN_SET" \
    | grep -Ec '^[[:space:]]*test-sable-bin-install\.sh[[:space:]]*$')"
if [ "$standalone_allow_count" -eq 1 ]; then
    pass "test-sable-bin-install.sh remains exactly one standalone full-suite authority"
else
    fail "test-sable-bin-install.sh remains exactly one standalone full-suite authority" \
         "standalone_allow_count=$standalone_allow_count"
fi

# ---------- 2. purpose-built clean-room-aware installer probe ---------------
# The recorder is outside the scrubbed PATH and proves both planted modes ran
# exactly once. The probe first verifies the wrapper really removed ambient bd;
# only its positive mode then prepends a private no-op bd, matching the seam the
# standalone bin-install suite uses before entering the real installer.
PROBE_ROOT="$(mktemp -d)"
PROBE="$PROBE_ROOT/install-aware-probe.sh"
PROBE_RUNNER="$PROBE_ROOT/record-probe"
PROBE_STUB="$PROBE_ROOT/stub"
PROBE_HOME="$PROBE_ROOT/home"
PROBE_CALLS="$PROBE_ROOT/calls.log"
BASH_BIN="$(command -v bash)"
mkdir -p "$PROBE_STUB" "$PROBE_HOME"
cat > "$PROBE_STUB/bd" <<'EOF'
#!/bin/sh
exit 0
EOF
chmod +x "$PROBE_STUB/bd"
cat > "$PROBE_RUNNER" <<'EOF'
#!/bin/sh
bash_bin="$1"
shift
printf '%s\n' "${2:-missing-mode}" >> "${PROBE_CALLS:?}"
exec "$bash_bin" "$@"
EOF
chmod +x "$PROBE_RUNNER"
cat > "$PROBE" <<'EOF'
#!/usr/bin/env bash
set -uo pipefail
mode="$1" install_sh="$2" probe_stub="$3" probe_home="$4"
if command -v bd >/dev/null 2>&1; then
    echo "PROBE-FAIL: ambient bd survived the clean-room scrub"
    exit 41
fi
echo "PROBE: ambient bd absent"
if [ "$mode" = "with-stub" ]; then
    PATH="$probe_stub:$PATH"
    command -v bd >/dev/null 2>&1 || { echo "PROBE-FAIL: private bd stub absent"; exit 42; }
    echo "PROBE: private bd stub visible"
fi
HOME="$probe_home" PATH="$PATH" bash "$install_sh" --from-here --dry-run
EOF
chmod +x "$PROBE"

out="$(PROBE_CALLS="$PROBE_CALLS" bash "$TOOL" \
    "$PROBE_RUNNER" "$BASH_BIN" "$PROBE" with-stub \
    "$INSTALL_SH" "$PROBE_STUB" "$PROBE_HOME" 2>&1)"; rc=$?
if [ "$rc" -eq 0 ] \
   && printf '%s' "$out" | grep -q 'PROBE: ambient bd absent' \
   && printf '%s' "$out" | grep -q 'PROBE: private bd stub visible' \
   && printf '%s' "$out" | grep -q 'Step 1/8: Verify bd is installed' \
   && printf '%s' "$out" | grep -q 'Step 8/8: Claude settings.json'; then
    pass "positive control: clean-room-aware probe adds its own bd stub and traverses the real installer"
else
    fail "positive control: clean-room-aware probe adds its own bd stub and traverses the real installer" \
         "rc=$rc out=${out:0:800}"
fi

out="$(PROBE_CALLS="$PROBE_CALLS" bash "$TOOL" \
    "$PROBE_RUNNER" "$BASH_BIN" "$PROBE" without-stub \
    "$INSTALL_SH" "$PROBE_STUB" "$PROBE_HOME" 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] \
   && printf '%s' "$out" | grep -q 'PROBE: ambient bd absent' \
   && printf '%s' "$out" | grep -qi 'bd (beads) is not on PATH' \
   && ! printf '%s' "$out" | grep -q 'Step 8/8: Claude settings.json'; then
    pass "negative control: the same probe without its private bd stub fails loudly at installer Step 1"
else
    fail "negative control: the same probe without its private bd stub fails loudly at installer Step 1" \
         "rc=$rc out=${out:0:800}"
fi

with_stub_calls="$(grep -c '^with-stub$' "$PROBE_CALLS" 2>/dev/null || true)"
without_stub_calls="$(grep -c '^without-stub$' "$PROBE_CALLS" 2>/dev/null || true)"
total_probe_calls="$(wc -l < "$PROBE_CALLS")"
if [ "$with_stub_calls" -eq 1 ] && [ "$without_stub_calls" -eq 1 ] \
   && [ "$total_probe_calls" -eq 2 ]; then
    pass "probe controls executed exactly once each (2 total invocations)"
else
    fail "probe controls executed exactly once each (2 total invocations)" \
         "with_stub=$with_stub_calls without_stub=$without_stub_calls total=$total_probe_calls"
fi
rm -rf "$PROBE_ROOT"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
