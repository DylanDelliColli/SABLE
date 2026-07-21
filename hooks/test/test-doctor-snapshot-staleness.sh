#!/usr/bin/env bash
# test-doctor-snapshot-staleness.sh — SABLE-0jplo
#
# sable-doctor reported "clean — N installed files match repo HEAD" while the
# pinned merge-gate snapshot was three files BEHIND HEAD. The pin was honestly
# named (5e47cc4 content in a sable-5e47cc4/ directory), so doctor's snapshot
# check — which compares the snapshot against the repo AT THE SHA BAKED INTO
# ITS OWN DIRECTORY NAME (SABLE-9boz4/rucuh) — found nothing wrong and said so.
# That check only ever answered "is this snapshot still what it CLAIMS to be?";
# nobody was answering "is what it claims to be still CURRENT?". A reader
# trusting doctor would conclude SABLE-jd5fj.4 was active in that runtime; the
# switch code was absent from the pin entirely.
#
# The invariant this suite pins down: "doctor reports clean" must imply "the
# pinned content is byte-identical to repo HEAD for every file in the snapshot
# closure" — and, critically, that verdict must not change across a `bash
# install.sh` run. install.sh CORRECTLY does not touch a pin (SABLE-mkj6k
# pin-preservation); a detector that goes quiet around it is the defect.
#
# INTEGRATION: real fs, real git, real sable-bin-install, real install.sh, no
# mocks. Every install runs against a throwaway clone + throwaway HOME — this
# suite NEVER touches the real ~/.claude, ~/.local/bin or ~/.local/lib. HOME is
# redirected for every invocation, which also scopes SABLE_LIB_DIR's default
# (bin/sable-bin-install: LIB_DIR="${SABLE_LIB_DIR:-$HOME/.local/lib}") — do not
# remove that redirection; unscoped pinning suites have written unmerged code
# into the live merge gate before (SABLE-33hw3).
#
# Run with:
#   bash hooks/test/test-doctor-snapshot-staleness.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TARGET_BIN="sable-merge-gate"      # the real snapshot-shaped spine bin
CLOSURE_LIB="sable_gate_promote_lib.py"  # a sibling module inside its pin unit

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

finish() {
  echo
  echo "=========================================="
  echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
  echo "=========================================="
  if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
  exit 0
}

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# ---- a throwaway clone we are allowed to advance HEAD in -------------------
FIXREPO="$WORK/repo"
if ! git clone -q "$REPO" "$FIXREPO" 2>"$WORK/clone.log"; then
  fail "clone the SABLE repo into a sandbox" "$(cat "$WORK/clone.log")"
  finish
fi
git -C "$FIXREPO" config user.email "test@example.com"
git -C "$FIXREPO" config user.name "Test"

# Overlay the working tree's tracked files onto the clone. `git clone` copies
# COMMITTED state, so without this the suite would exercise the last commit
# instead of the code under test — a green that means nothing while you are
# mid-change, which is the same species of false green this bead is about.
if ! (cd "$REPO" && git ls-files -z | tar cf - --null -T - 2>/dev/null) \
     | (cd "$FIXREPO" && tar xf - 2>/dev/null); then
  fail "overlay the working tree onto the sandbox clone"
  finish
fi
git -C "$FIXREPO" add -A
git -C "$FIXREPO" commit -q -m "overlay working tree" --allow-empty

# install.sh Step 1/8 hard-requires bd on PATH (mirrors test-install-preserves-pins.sh)
STUB="$WORK/stub"
mkdir -p "$STUB"
printf '#!/bin/sh\nexit 0\n' > "$STUB/bd"
chmod +x "$STUB/bd"

SANDBOX="$WORK/home"
mkdir -p "$SANDBOX/.claude" "$SANDBOX/.local/bin" "$SANDBOX/.local/lib"

run_install_sh() {
  # --from-here: this suite commonly runs from a linked SABLE worker worktree,
  # and the clone inherits that shape; the HOME installed into is throwaway.
  PATH="$STUB:$PATH" HOME="$SANDBOX" SABLE_LIB_DIR="$SANDBOX/.local/lib" \
    bash "$FIXREPO/install.sh" --from-here >"$1" 2>&1
}

doctor_report() {
  PATH="$STUB:$PATH" HOME="$SANDBOX" \
    python3 "$FIXREPO/bin/sable-doctor" \
      --repo "$FIXREPO" --claude-dir "$SANDBOX/.claude" \
      --bin-dir "$SANDBOX/.local/bin" >"$1" 2>&1
  return $?
}

# ---- 1. a real install, then a real snapshot pin ---------------------------
run_install_sh "$WORK/install-1.log"
if [ $? -eq 0 ]; then
  pass "install.sh --from-here completes against the sandbox scope"
else
  fail "install.sh completes" "$(tail -20 "$WORK/install-1.log")"
fi

PATH="$STUB:$PATH" HOME="$SANDBOX" SABLE_LIB_DIR="$SANDBOX/.local/lib" \
  bash "$FIXREPO/bin/sable-bin-install" --dir "$SANDBOX/.local/bin" \
  --pin-snapshot "$TARGET_BIN" >"$WORK/pin.log" 2>&1
PIN_TARGET="$(readlink -f "$SANDBOX/.local/bin/$TARGET_BIN" 2>/dev/null || true)"
case "$PIN_TARGET" in
  "$SANDBOX/.local/lib/sable-"*) pass "$TARGET_BIN is snapshot-pinned into the sandbox lib dir" ;;
  *) fail "$TARGET_BIN snapshot pin" "resolved to: ${PIN_TARGET:-<nothing>} (log: $(cat "$WORK/pin.log"))"; finish ;;
esac
PINNED_SHA_DIR="$(dirname "$PIN_TARGET")"

# ---- 2. advance the repo so the pinned bin's CLOSURE changes ---------------
# A sibling module inside the pin unit, not the entry point: the pin unit is
# the whole snapshot directory, so staleness must be visible for any file in it.
printf '\n# SABLE-0jplo staleness fixture — post-pin change\n' >> "$FIXREPO/bin/$CLOSURE_LIB"
printf '\n# SABLE-0jplo staleness fixture — post-pin change\n' >> "$FIXREPO/bin/$TARGET_BIN"
git -C "$FIXREPO" add -A
git -C "$FIXREPO" commit -q -m "advance the pinned bin's closure"

# the pin itself is untouched and still HONESTLY named — this is not tampering
if [ "$(readlink -f "$SANDBOX/.local/bin/$TARGET_BIN")" = "$PIN_TARGET" ]; then
  pass "the pin still points at its original snapshot after the repo advances"
else
  fail "pin unchanged by the repo advancing" "now: $(readlink -f "$SANDBOX/.local/bin/$TARGET_BIN")"
fi

# ---- 3. doctor must SEE the staleness -------------------------------------
doctor_report "$WORK/doctor-1.log"
RC1=$?
if [ "$RC1" -ne 0 ]; then
  pass "doctor exits non-zero while the pinned snapshot is behind HEAD"
else
  fail "doctor must not report success for a stale pin" "$(cat "$WORK/doctor-1.log")"
fi
if grep -q "SNAPSHOT-STALE" "$WORK/doctor-1.log"; then
  pass "doctor #1 reports SNAPSHOT-STALE for the pinned bin"
else
  fail "doctor #1 reports SNAPSHOT-STALE" "$(cat "$WORK/doctor-1.log")"
fi
if grep -q "$CLOSURE_LIB" "$WORK/doctor-1.log" && grep -q "$TARGET_BIN" "$WORK/doctor-1.log"; then
  pass "doctor #1 NAMES both stale files in the snapshot closure"
else
  fail "doctor #1 names the stale files" "$(cat "$WORK/doctor-1.log")"
fi

# ---- 4. the install path must not change that verdict ----------------------
# THE REGRESSION GUARD. Both doctor invocations live in this one test so the
# TRANSITION is what is asserted, not either state alone.
run_install_sh "$WORK/install-2.log"
if [ $? -eq 0 ]; then
  pass "the second install.sh run completes"
else
  fail "second install.sh completes" "$(tail -20 "$WORK/install-2.log")"
fi

if [ "$(readlink -f "$SANDBOX/.local/bin/$TARGET_BIN")" = "$PIN_TARGET" ]; then
  pass "install.sh preserves the pin (SABLE-mkj6k) — so the staleness is still real"
else
  fail "install.sh must preserve the pin" "now: $(readlink -f "$SANDBOX/.local/bin/$TARGET_BIN")"
fi

doctor_report "$WORK/doctor-2.log"
RC2=$?
if [ "$RC2" -eq "$RC1" ]; then
  pass "doctor's exit status is UNCHANGED across the install.sh run"
else
  fail "doctor exit status unchanged across install.sh" "before=$RC1 after=$RC2"
fi
if grep -q "SNAPSHOT-STALE" "$WORK/doctor-2.log"; then
  pass "doctor #2 STILL reports SNAPSHOT-STALE after install.sh"
else
  fail "doctor #2 still reports SNAPSHOT-STALE" "$(cat "$WORK/doctor-2.log")"
fi
if grep -q "$CLOSURE_LIB" "$WORK/doctor-2.log" && grep -q "$TARGET_BIN" "$WORK/doctor-2.log"; then
  pass "doctor #2 STILL names the stale files after install.sh"
else
  fail "doctor #2 still names the stale files" "$(cat "$WORK/doctor-2.log")"
fi

# ---- 5. the invariant, asserted directly ----------------------------------
# "doctor reports clean" must imply "the pinned content is byte-identical to
# repo HEAD for every file in the snapshot closure". Re-pin at current HEAD and
# assert the entry goes clean — the other half of the biconditional, without
# which "always report stale" would pass everything above.
PATH="$STUB:$PATH" HOME="$SANDBOX" SABLE_LIB_DIR="$SANDBOX/.local/lib" \
  bash "$FIXREPO/bin/sable-bin-install" --dir "$SANDBOX/.local/bin" \
  --pin-snapshot "$TARGET_BIN" >"$WORK/pin-2.log" 2>&1
doctor_report "$WORK/doctor-3.log"
if grep -q "SNAPSHOT-STALE" "$WORK/doctor-3.log"; then
  fail "a pin re-taken at HEAD must not read as stale" "$(cat "$WORK/doctor-3.log")"
else
  pass "re-pinning at HEAD clears SNAPSHOT-STALE (no cry-wolf)"
fi
NEW_PIN="$(readlink -f "$SANDBOX/.local/bin/$TARGET_BIN")"
if [ "$NEW_PIN" != "$PIN_TARGET" ] && [ "$(dirname "$NEW_PIN")" != "$PINNED_SHA_DIR" ]; then
  pass "the re-pin actually moved to a new snapshot directory"
else
  fail "re-pin moved the snapshot" "still: $NEW_PIN"
fi

finish
