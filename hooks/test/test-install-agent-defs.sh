#!/usr/bin/env bash
# test-install-agent-defs.sh — Installer idempotency test for the agent definitions
# step added in SABLE-uz9.7.
#
# Two test layers:
#   1. Unit: run install.sh twice against a temp HOME; assert identical state
#      and all six agent definitions present after each run.
#   2. Integration: fresh-HOME install in a temp dir; assert agent definitions
#      present and hooks registered.
#
# Preserves non-SABLE agent files (a file not in the SABLE set must survive
# both runs unchanged).
#
# Run with:
#   bash hooks/test/test-install-agent-defs.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALLER="$REPO/install.sh"
# Producers only (tmux-only, SABLE-qa4d.5): managers are panes, not agent defs.
SABLE_AGENTS="columbo rudy sherlock victor"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# Helper: run install.sh in a temp HOME, suppressing interactive output
run_install() {
    local home_dir="$1"
    HOME="$home_dir" bash "$INSTALLER" >/dev/null 2>&1
}

# ──────────────────────────────────────────────────────────────────────────────
# Layer 1: Unit — idempotency (two runs, identical state)
# ──────────────────────────────────────────────────────────────────────────────
T1="$(mktemp -d)"
trap 'rm -rf "$T1"' EXIT

# Seed a non-SABLE agent file that must survive
mkdir -p "$T1/.claude/agents"
echo "my-custom-agent" > "$T1/.claude/agents/my-custom.md"

# First run
run_install "$T1"

# Check all six agents present after first run
for name in $SABLE_AGENTS; do
    f="$T1/.claude/agents/$name.md"
    if [ -f "$f" ]; then pass "run1: $name.md installed"; else fail "run1: $name.md installed" "missing: $f"; fi
done

# Check non-SABLE file preserved after first run
if grep -q "my-custom-agent" "$T1/.claude/agents/my-custom.md" 2>/dev/null; then
    pass "run1: non-SABLE agent file preserved"
else
    fail "run1: non-SABLE agent file preserved" "file was modified or removed"
fi

# Snapshot checksums after first run
snap1="$(for name in $SABLE_AGENTS; do md5sum "$T1/.claude/agents/$name.md" 2>/dev/null || echo "MISSING"; done)"

# Second run
run_install "$T1"

# Check all six agents present after second run
for name in $SABLE_AGENTS; do
    f="$T1/.claude/agents/$name.md"
    if [ -f "$f" ]; then pass "run2: $name.md still present"; else fail "run2: $name.md still present" "missing: $f"; fi
done

# Snapshot checksums after second run — must match first
snap2="$(for name in $SABLE_AGENTS; do md5sum "$T1/.claude/agents/$name.md" 2>/dev/null || echo "MISSING"; done)"
if [ "$snap1" = "$snap2" ]; then
    pass "idempotent: checksums identical across two runs"
else
    fail "idempotent: checksums identical across two runs" "run1 vs run2 differ"
fi

# Non-SABLE file must still be preserved after second run
if grep -q "my-custom-agent" "$T1/.claude/agents/my-custom.md" 2>/dev/null; then
    pass "run2: non-SABLE agent file preserved"
else
    fail "run2: non-SABLE agent file preserved" "file was modified or removed after second run"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Layer 2: Integration — fresh-HOME install; agents + hooks registered
# ──────────────────────────────────────────────────────────────────────────────
T2="$(mktemp -d)"
trap 'rm -rf "$T1" "$T2"' EXIT

run_install "$T2"

# All six agent definitions present
for name in $SABLE_AGENTS; do
    f="$T2/.claude/agents/$name.md"
    if [ -f "$f" ]; then pass "integration: $name.md present"; else fail "integration: $name.md present" "missing: $f"; fi
done

# Verify agent files are non-empty and carry the expected v3 marker
for name in $SABLE_AGENTS; do
    f="$T2/.claude/agents/$name.md"
    [ -f "$f" ] || continue
    if grep -q "v3 invocation" "$f" 2>/dev/null; then
        pass "integration: $name.md carries v3 invocation marker"
    else
        fail "integration: $name.md carries v3 invocation marker" "pattern not found in: $f"
    fi
done

# Hooks installed
for hook in tdd-gate.sh bead-description-gate.sh tdd-evidence.sh; do
    f="$T2/.claude/hooks/$hook"
    if [ -f "$f" ]; then pass "integration: hook $hook installed"; else fail "integration: hook $hook installed" "missing: $f"; fi
done

# Manager defs are NOT installed on a fresh HOME (managers are panes, SABLE-qa4d.5)
for name in optimus tarzan chuck; do
    if [ ! -e "$T2/.claude/agents/$name.md" ]; then pass "integration: $name.md not installed (managers are panes)"; else fail "integration: $name.md not installed (managers are panes)" "present: $T2/.claude/agents/$name.md"; fi
done

# ──────────────────────────────────────────────────────────────────────────────
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
