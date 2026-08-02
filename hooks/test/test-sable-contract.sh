#!/usr/bin/env bash
# test-sable-contract.sh — unit tests for the active-contracts writer (SABLE-9ozz).
#
# sable-contract is the WRITE side of the active-protocol surface: flips and
# managers append/replace live contracts here, and session-role-anchor.sh SURFACES
# them at SessionStart so a restarted pane reconciles against the current protocol
# instead of its historical static identity. The surface is colocated with the
# mode-state file in <repo>/.claude/sable/state/ so both live/protocol surfaces
# travel together.
#
# Run with:
#   bash hooks/test/test-sable-contract.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="$REPO/bin/sable-contract"

# Hermeticity: never let an ambient override leak in (SABLE-j3bi).
unset SABLE_ACTIVE_CONTRACTS SABLE_MODE_STATE 2>/dev/null || true

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$TOOL" ] || { fail "bin/sable-contract exists and is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "bin/sable-contract exists and is executable"

# ---------- SABLE_ACTIVE_CONTRACTS override: path/add/show/set/clear ----------
TMP="$(mktemp -d)"
CFILE="$TMP/active-contracts.md"

out="$(SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" path)"
if [ "$out" = "$CFILE" ]; then pass "path honors SABLE_ACTIVE_CONTRACTS"; else fail "path honors SABLE_ACTIVE_CONTRACTS" "got: $out"; fi

# ---------- SABLE-st8su: repeated execution-entry seeding is idempotent ----------
# A contract's timestamp is publication metadata, not part of its identity.  The
# payload after `] ` is compared byte-for-byte: an exact repeat is a quiet no-op,
# while even a whitespace-different payload remains a distinct contract.
IDEMP="$TMP/idempotent-contracts.md"
SABLE_ACTIVE_CONTRACTS="$IDEMP" bash "$TOOL" add "seed contract [literal].*" >/dev/null 2>&1
cp -f "$IDEMP" "$TMP/idempotent-before.md"
SABLE_ACTIVE_CONTRACTS="$IDEMP" bash "$TOOL" add "seed contract [literal].*" >/dev/null 2>&1; rc=$?
n="$(grep -c '^- ' "$IDEMP" 2>/dev/null || echo 0)"
if [ "$rc" -eq 0 ] && [ "$n" = "1" ] && cmp -s "$TMP/idempotent-before.md" "$IDEMP"; then
  pass "add of an exact existing contract is a byte-identical rc0 no-op"
else
  fail "add of an exact existing contract is a byte-identical rc0 no-op" "rc=$rc count=$n"
fi
SABLE_ACTIVE_CONTRACTS="$IDEMP" bash "$TOOL" add "seed contract [literal].* " >/dev/null 2>&1
n="$(grep -c '^- ' "$IDEMP" 2>/dev/null || echo 0)"
if [ "$n" = "2" ]; then pass "add preserves whitespace-different contracts as distinct"; else fail "add preserves whitespace-different contracts as distinct" "count=$n"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" add "sable-merge-gate is the SOLE merge path" >/dev/null 2>&1
if grep -q 'sable-merge-gate is the SOLE merge path' "$CFILE"; then pass "add writes a contract"; else fail "add writes a contract"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" add "interim worker cap is 2 per manager" >/dev/null 2>&1
n="$(grep -c '^- ' "$CFILE" 2>/dev/null || echo 0)"
if [ "$n" = "2" ]; then pass "add appends (does not overwrite)"; else fail "add appends (does not overwrite)" "count=$n"; fi

out="$(SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" show 2>/dev/null)"
if printf '%s' "$out" | grep -q 'interim worker cap is 2'; then pass "show prints contracts"; else fail "show prints contracts" "got: ${out:0:120}"; fi

# ---------- SABLE-wx048: set must not silently destroy accumulated doctrine ----------
# The /sable-execute flip prescribed `set` as its FIRST call, so every flip erased
# everything accumulated since the last one (measured 2026-07-30: 25 of 29 committed
# lines of operator doctrine destroyed by one flip). `set` on a NON-EMPTY surface now
# refuses; the destructive path still exists but must be chosen on purpose.
SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" set "single replacing contract" >/dev/null 2>&1; rc=$?
n="$(grep -c '^- ' "$CFILE" 2>/dev/null || echo 0)"
if [ "$rc" -ne 0 ]; then pass "set on a NON-EMPTY surface refuses (nonzero)"; else fail "set on a NON-EMPTY surface refuses (nonzero)" "rc=$rc"; fi
if [ "$n" = "2" ] && grep -q 'sable-merge-gate is the SOLE merge path' "$CFILE"; then pass "refused set PRESERVES prior entries"; else fail "refused set PRESERVES prior entries" "count=$n"; fi

# NEGATIVE CONTROL: the fix must NARROW the destructive path, not remove it.
# An explicitly-forced set still replaces the surface, but only after publishing
# a byte-identical, uniquely named backup.  A fixed clock proves two force writes
# in the same second cannot overwrite one another.
cp -f "$CFILE" "$TMP/pre-force-generation-1.md"
FIXED_DATE_BIN="$TMP/fixed-date-bin"
mkdir -p "$FIXED_DATE_BIN"
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\\n" "2026-08-02T120000+0000"' > "$FIXED_DATE_BIN/date"
chmod +x "$FIXED_DATE_BIN/date"
PATH="$FIXED_DATE_BIN:$PATH" SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" set --force "single replacing contract" >/dev/null 2>&1; rc=$?
n="$(grep -c '^- ' "$CFILE" 2>/dev/null || echo 0)"
if [ "$rc" -eq 0 ] && [ "$n" = "1" ] && grep -q 'single replacing contract' "$CFILE"; then pass "set --force still replaces the surface (negative control)"; else fail "set --force still replaces the surface (negative control)" "rc=$rc count=$n"; fi
BACKUP_COUNT="$(find "$TMP" -maxdepth 1 -type f -name 'active-contracts.md.bak.*' | wc -l)"
if [ "$BACKUP_COUNT" = "1" ] && cmp -s "$TMP/pre-force-generation-1.md" "$TMP"/active-contracts.md.bak.*; then
  pass "set --force publishes a byte-identical backup before replacement"
else
  fail "set --force publishes a byte-identical backup before replacement" "backups=$BACKUP_COUNT"
fi

cp -f "$CFILE" "$TMP/pre-force-generation-2.md"
PATH="$FIXED_DATE_BIN:$PATH" SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" set --force "second replacing contract" >/dev/null 2>&1; rc=$?
BACKUP_COUNT="$(find "$TMP" -maxdepth 1 -type f -name 'active-contracts.md.bak.*' | wc -l)"
GEN1=0; GEN2=0
for backup in "$TMP"/active-contracts.md.bak.*; do
  [ -f "$backup" ] || continue
  cmp -s "$TMP/pre-force-generation-1.md" "$backup" && GEN1=$((GEN1 + 1))
  cmp -s "$TMP/pre-force-generation-2.md" "$backup" && GEN2=$((GEN2 + 1))
done
if [ "$rc" -eq 0 ] && [ "$BACKUP_COUNT" = "2" ] && [ "$GEN1" = "1" ] && [ "$GEN2" = "1" ]; then
  pass "same-second forced replacements preserve both backup generations"
else
  fail "same-second forced replacements preserve both backup generations" "rc=$rc backups=$BACKUP_COUNT gen1=$GEN1 gen2=$GEN2"
fi

# Backup failure must happen before the old surface is touched.  Removing
# directory write permission still permits truncating the existing writable
# file, so old code (replace-without-backup) reaches the dangerous path while a
# fail-closed backup-first implementation refuses and preserves exact bytes.
FAIL_DIR="$TMP/backup-failure"
mkdir -p "$FAIL_DIR"
FAIL_FILE="$FAIL_DIR/active-contracts.md"
printf '%s\n' '- [before] irreplaceable prior surface' > "$FAIL_FILE"
cp -f "$FAIL_FILE" "$TMP/backup-failure-before.md"
chmod 500 "$FAIL_DIR"
SABLE_ACTIVE_CONTRACTS="$FAIL_FILE" bash "$TOOL" set --force "must not land" >/dev/null 2>&1; rc=$?
chmod 700 "$FAIL_DIR"
if [ "$rc" -ne 0 ] && cmp -s "$TMP/backup-failure-before.md" "$FAIL_FILE"; then
  pass "backup publication failure aborts replacement without mutation"
else
  fail "backup publication failure aborts replacement without mutation" "rc=$rc preserved=$(cmp -s "$TMP/backup-failure-before.md" "$FAIL_FILE"; echo $?)"
fi

# set on an EMPTY/absent surface is the legitimate first write and must still work.
EMPTYC="$TMP/first-write.md"
SABLE_ACTIVE_CONTRACTS="$EMPTYC" bash "$TOOL" set "first contract on a fresh surface" >/dev/null 2>&1; rc=$?
n="$(grep -c '^- ' "$EMPTYC" 2>/dev/null || echo 0)"
if [ "$rc" -eq 0 ] && [ "$n" = "1" ]; then pass "set on an absent surface still writes (first-write path)"; else fail "set on an absent surface still writes (first-write path)" "rc=$rc count=$n"; fi
EMPTY_BACKUPS="$(find "$TMP" -maxdepth 1 -type f -name 'first-write.md.bak.*' | wc -l)"
if [ "$EMPTY_BACKUPS" = "0" ]; then pass "first write does not invent an empty backup"; else fail "first write does not invent an empty backup" "backups=$EMPTY_BACKUPS"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" clear >/dev/null 2>&1
if [ ! -f "$CFILE" ]; then pass "clear removes the surface"; else fail "clear removes the surface"; fi

# ---------- error paths ----------
SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" show >/dev/null 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "show on empty surface exits nonzero"; else fail "show on empty surface exits nonzero" "rc=$rc"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" add >/dev/null 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "add without text exits nonzero"; else fail "add without text exits nonzero" "rc=$rc"; fi

SABLE_ACTIVE_CONTRACTS="$CFILE" bash "$TOOL" set >/dev/null 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "set without text exits nonzero"; else fail "set without text exits nonzero" "rc=$rc"; fi

# ---------- SABLE_MODE_STATE dirname colocation ----------
out="$(SABLE_MODE_STATE="$TMP/sub/mode-state.json" bash "$TOOL" path)"
if [ "$out" = "$TMP/sub/active-contracts.md" ]; then pass "path colocates via SABLE_MODE_STATE dirname"; else fail "path colocates via SABLE_MODE_STATE dirname" "got: $out"; fi

# ---------- git-repo resolution mirrors mode-state's state dir ----------
GITREPO="$(mktemp -d)"
git -C "$GITREPO" init -q >/dev/null 2>&1
cpath="$(cd "$GITREPO" && bash "$TOOL" path)"
expected="$GITREPO/.claude/sable/state/active-contracts.md"
if [ "$cpath" = "$expected" ]; then pass "git-repo path resolves under .claude/sable/state"; else fail "git-repo path resolves under .claude/sable/state" "got: $cpath want: $expected"; fi
( cd "$GITREPO" && bash "$TOOL" set "canonical prior contract" >/dev/null 2>&1 )
( cd "$GITREPO" && bash "$TOOL" set --force "canonical replacement" >/dev/null 2>&1 )
CANON_BACKUP="$(find "$GITREPO/.claude/sable/state" -maxdepth 1 -type f -name 'active-contracts.md.bak.*' -print -quit 2>/dev/null)"
if [ -n "$CANON_BACKUP" ] && git -C "$GITREPO" check-ignore -q "$CANON_BACKUP"; then
  pass "canonical backup is covered by the state gitignore"
else
  fail "canonical backup is covered by the state gitignore" "backup=${CANON_BACKUP:-missing}"
fi
mpath="$(cd "$GITREPO" && "$REPO/bin/sable-mode" path)"
if [ "$(dirname "$cpath")" = "$(dirname "$mpath")" ]; then pass "contracts colocated with mode-state dir (no drift)"; else fail "contracts colocated with mode-state dir (no drift)" "c=$cpath m=$mpath"; fi

rm -rf "$TMP" "$GITREPO"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
