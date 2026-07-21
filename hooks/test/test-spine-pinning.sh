#!/usr/bin/env bash
# test-spine-pinning.sh — SABLE-9boz4
#
# Full spine-bin pinning for python-importing tools. sable-spawn-worker and
# sable-msg import sibling repo modules (sable_pane_lib.py, ...) from bin/, so
# a naive copy-pin (SABLE-mkj6k's regular-file mechanism) severs the import —
# caught live by a smoke test and rolled back during the 2026-07-21 y6ik3
# window (sable-msg was never converted, avoiding a fleet-wide comms kill).
#
# --pin-snapshot instead freezes the WHOLE bin/ directory as one versioned
# unit (~/.local/lib/sable-<sha>/) and atomically repoints the entry symlink
# into it, so the import closure travels with the pin. This is the
# integration proof: real repo, real scratch HOME, no mocks.
#
#   (1) pin BOTH real python-importing bins named in the bead — sable-msg and
#       sable-spawn-worker — via --pin-snapshot,
#   (2) execute BOTH successfully from OUTSIDE the repo (proves the import
#       actually resolves, not just that files exist — a bare file-presence
#       check would have missed the exact ImportError the 2026-07-21 y6ik3
#       window hit live),
#   (3) a plain re-install does not revert either pin,
#   (4) sable-doctor flags an artificially drifted (hand-tampered) snapshot,
#   (5) --repin rolls the pin back to the live repo and clears the drift.
#
# Run with:
#   bash hooks/test/test-spine-pinning.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="$REPO/bin/sable-bin-install"
DOCTOR="$REPO/bin/sable-doctor"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$INSTALL" ] || { fail "sable-bin-install is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "sable-bin-install is executable"
[ -f "$DOCTOR" ] || { fail "sable-doctor present"; echo "Tests: 2 | Passed: 1 | Failed: 1"; exit 1; }
pass "sable-doctor is present"

SCRATCH_HOME="$(mktemp -d)"
BIN_DEST="$SCRATCH_HOME/.local/bin"
CLAUDE_DEST="$SCRATCH_HOME/.claude"
mkdir -p "$BIN_DEST" "$CLAUDE_DEST"

# ============================================================================
# (1)+(2) pin BOTH sable-msg and sable-spawn-worker — the two real
# python-importing spine bins the bead names — via --pin-snapshot, and
# execute EACH successfully from OUTSIDE the repo. An ImportError here would
# exit non-zero with a traceback, not print usage — a bare file-presence
# check would have missed exactly what the 2026-07-21 y6ik3 window hit live.
# ============================================================================
PIN_OUT="$(bash "$INSTALL" --dir "$BIN_DEST" --pin-snapshot sable-msg sable-spawn-worker 2>&1)"
PIN_RC=$?

if [ "$PIN_RC" -eq 0 ]; then
  pass "--pin-snapshot sable-msg sable-spawn-worker exits 0"
else
  fail "--pin-snapshot sable-msg sable-spawn-worker exits 0" "rc=$PIN_RC out=$PIN_OUT"
fi

SNAPSHOT_DIR=""
for TOOL in sable-msg sable-spawn-worker; do
  if [ -L "$BIN_DEST/$TOOL" ]; then
    pass "$TOOL entry point is a symlink after pinning"
  else
    fail "$TOOL entry point is a symlink" "$(ls -l "$BIN_DEST/$TOOL" 2>/dev/null)"
  fi

  RESOLVED="$(readlink -f "$BIN_DEST/$TOOL" 2>/dev/null || true)"
  case "$RESOLVED" in
    "$REPO"/bin/*)
      fail "pinned $TOOL resolves OUTSIDE the live repo bin/" "resolved to $RESOLVED (still inside repo)"
      ;;
    *sable-*)
      pass "pinned $TOOL resolves into a versioned snapshot dir (outside repo bin/)"
      SNAPSHOT_DIR="$(dirname "$RESOLVED")"
      ;;
    *)
      fail "pinned $TOOL resolves into a versioned snapshot dir" "resolved to $RESOLVED"
      ;;
  esac

  OUTSIDE_CWD="$(mktemp -d)"
  RUN_OUT="$(cd "$OUTSIDE_CWD" && HOME="$SCRATCH_HOME" "$BIN_DEST/$TOOL" --help 2>&1)"
  RUN_RC=$?
  rm -rf "$OUTSIDE_CWD"

  if [ "$RUN_RC" -eq 0 ] && printf '%s' "$RUN_OUT" | grep -q "usage: $TOOL"; then
    pass "pinned $TOOL runs successfully from outside the repo (import resolved)"
  else
    fail "pinned $TOOL runs from outside the repo" "rc=$RUN_RC out=$RUN_OUT"
  fi
done

if [ -n "$SNAPSHOT_DIR" ] && [ -f "$SNAPSHOT_DIR/sable_pane_lib.py" ]; then
  pass "the snapshot carries sable_pane_lib.py alongside both entry points (one unit, not per-file)"
else
  fail "snapshot carries the sibling lib" "snapshot dir: $SNAPSHOT_DIR"
fi

# ============================================================================
# (3) a plain re-install does not revert either pin (SABLE-9boz4 extends the
# mkj6k pin-protection check to symlink-pinned snapshots, not just regular
# files)
# ============================================================================
PRE_MSG_TARGET="$(readlink -f "$BIN_DEST/sable-msg")"
PRE_SPAWN_TARGET="$(readlink -f "$BIN_DEST/sable-spawn-worker")"
REINSTALL_OUT="$(bash "$INSTALL" --dir "$BIN_DEST" 2>&1)"
POST_MSG_TARGET="$(readlink -f "$BIN_DEST/sable-msg")"
POST_SPAWN_TARGET="$(readlink -f "$BIN_DEST/sable-spawn-worker")"

if [ "$PRE_MSG_TARGET" = "$POST_MSG_TARGET" ] && [ "$PRE_SPAWN_TARGET" = "$POST_SPAWN_TARGET" ]; then
  pass "a plain re-install does not revert either snapshot pin"
else
  fail "plain re-install must not revert the snapshot pins" \
    "msg pre=$PRE_MSG_TARGET post=$POST_MSG_TARGET | spawn-worker pre=$PRE_SPAWN_TARGET post=$POST_SPAWN_TARGET"
fi

if printf '%s' "$REINSTALL_OUT" | grep -qi "pinned"; then
  pass "a loud notice is emitted when the snapshot pins are skipped"
else
  fail "loud notice on skipped snapshot pins" "output=[$REINSTALL_OUT]"
fi

# ============================================================================
# (4) sable-doctor flags an artificially drifted (hand-tampered) snapshot —
# real files, no mocks. A plain sha256-vs-repo-HEAD compare would be the WRONG
# check here (this pin is deliberately frozen at an older sha); doctor must
# compare the snapshot against the repo AT ITS OWN PINNED SHA.
# ============================================================================
printf '\n# hand-tampered — SABLE-9boz4 integration probe\n' >> "$SNAPSHOT_DIR/sable-msg"

DOCTOR_OUT="$(python3 "$DOCTOR" --repo "$REPO" --claude-dir "$CLAUDE_DEST" --bin-dir "$BIN_DEST" 2>&1)"
DOCTOR_RC=$?

if [ "$DOCTOR_RC" -ne 0 ]; then
  pass "sable-doctor exits non-zero once the snapshot is hand-tampered"
else
  fail "sable-doctor must exit non-zero on tampered snapshot" "rc=$DOCTOR_RC"
fi

if printf '%s' "$DOCTOR_OUT" | grep -q "pinned snapshot bins"; then
  pass "sable-doctor reports the 'pinned snapshot bins' category"
else
  fail "sable-doctor reports pinned snapshot bins category" "out=$DOCTOR_OUT"
fi

if printf '%s' "$DOCTOR_OUT" | grep -qi "SNAPSHOT-DRIFT.*sable-msg"; then
  pass "sable-doctor flags sable-msg's tampered snapshot as SNAPSHOT-DRIFT"
else
  fail "sable-doctor flags tampered snapshot as SNAPSHOT-DRIFT" "out=$DOCTOR_OUT"
fi

if printf '%s' "$DOCTOR_OUT" | grep -q -- "--pin-snapshot"; then
  pass "the drift remedy names --pin-snapshot (not the ordinary installer refresh)"
else
  fail "drift remedy names --pin-snapshot" "out=$DOCTOR_OUT"
fi

QUIET_OUT="$(python3 "$DOCTOR" --repo "$REPO" --claude-dir "$CLAUDE_DEST" --bin-dir "$BIN_DEST" --quiet 2>&1 >/dev/null)"
if printf '%s' "$QUIET_OUT" | grep -q "snapshot-pinned"; then
  pass "the --quiet SessionStart-hook path names the snapshot-pinned bin count"
else
  fail "--quiet path names snapshot-pinned bin count" "out=$QUIET_OUT"
fi

# ============================================================================
# (5) --repin rolls the pin back to the live repo and clears the drift
# ============================================================================
bash "$INSTALL" --dir "$BIN_DEST" --repin >/dev/null 2>&1

for TOOL in sable-msg sable-spawn-worker; do
  REPIN_TARGET="$(readlink -f "$BIN_DEST/$TOOL" 2>/dev/null || true)"
  if [ "$REPIN_TARGET" = "$REPO/bin/$TOOL" ]; then
    pass "--repin rolls $TOOL back to a live symlink into the repo"
  else
    fail "--repin rolls $TOOL back to the live repo" "resolved to $REPIN_TARGET"
  fi
done

CLEAN_JSON="$(python3 "$DOCTOR" --repo "$REPO" --claude-dir "$CLAUDE_DEST" --bin-dir "$BIN_DEST" --json 2>&1)"
if printf '%s' "$CLEAN_JSON" | grep -q '"category": "pinned snapshot bins"' \
   && printf '%s' "$CLEAN_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
bad = [r for r in data['results'] if r['category'] == 'pinned snapshot bins' and r['status'] != 'clean']
sys.exit(1 if bad else 0)
"; then
  pass "after --repin, sable-msg's pinned-snapshot-bins entry is clean again"
else
  fail "after --repin, sable-msg's entry is clean" "json=$CLEAN_JSON"
fi

# ============================================================================
# sibling_module_tamper_is_detected (SABLE-9boz4 REVISE, optimus review) --
# _check_snapshot_pin used to read ONLY the resolved entry-point file, never
# any other file inside the snapshot directory, so hand-tampering a SIBLING
# module (e.g. sable_pane_lib.py, the very file that justifies a
# directory-shaped pin unit) reported "clean" -- a false green in the drift
# detector whose entire job is to not have one. This pairs with (4) above,
# which tampers the ENTRY POINT: an instrument that catches one and not the
# other is the bug the pair exists to prove is fixed. Re-pin fresh first,
# since (5) just rolled the previous pin back to the live repo.
# ============================================================================
bash "$INSTALL" --dir "$BIN_DEST" --pin-snapshot sable-msg sable-spawn-worker >/dev/null 2>&1
SIBLING_RESOLVED="$(readlink -f "$BIN_DEST/sable-msg" 2>/dev/null || true)"
SIBLING_SNAPSHOT_DIR="$(dirname "$SIBLING_RESOLVED")"

printf '\n# hand-tampered SIBLING module (not the entry point) — SABLE-9boz4 REVISE probe\n' \
  >> "$SIBLING_SNAPSHOT_DIR/sable_pane_lib.py"

SIBLING_DOCTOR_OUT="$(python3 "$DOCTOR" --repo "$REPO" --claude-dir "$CLAUDE_DEST" --bin-dir "$BIN_DEST" 2>&1)"
SIBLING_DOCTOR_RC=$?

if [ "$SIBLING_DOCTOR_RC" -ne 0 ]; then
  pass "sibling_module_tamper_is_detected: sable-doctor exits non-zero when a SIBLING module (not the entry point) is tampered"
else
  fail "sibling_module_tamper_is_detected: sable-doctor must exit non-zero" "rc=$SIBLING_DOCTOR_RC out=$SIBLING_DOCTOR_OUT"
fi

if printf '%s' "$SIBLING_DOCTOR_OUT" | grep -qi "SNAPSHOT-DRIFT"; then
  pass "sibling_module_tamper_is_detected: sable-doctor reports SNAPSHOT-DRIFT"
else
  fail "sibling_module_tamper_is_detected: sable-doctor reports SNAPSHOT-DRIFT" "out=$SIBLING_DOCTOR_OUT"
fi

if printf '%s' "$SIBLING_DOCTOR_OUT" | grep -q "sable_pane_lib.py"; then
  pass "sibling_module_tamper_is_detected: sable-doctor NAMES the drifted sibling file (sable_pane_lib.py), not just the entry point"
else
  fail "sibling_module_tamper_is_detected: sable-doctor names the drifted sibling file" "out=$SIBLING_DOCTOR_OUT"
fi

# roll back to the live repo again so cleanup below finds no stray pins
bash "$INSTALL" --dir "$BIN_DEST" --repin >/dev/null 2>&1

rm -rf "$SCRATCH_HOME"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
