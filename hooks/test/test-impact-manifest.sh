#!/usr/bin/env bash
# test-impact-manifest.sh — unit tests for .github/ci/impact-manifest.sh's
# lib fan-out completeness checker (SABLE-cmar4.2, story S1 of SABLE-cmar4).
#
# THE MECHANISM UNDER TEST: sable_fanout_check() walks every ALLOW suite's
# covered production file(s) (COVERS, defaulting to the suite's own file),
# greps their source/. includes for hooks/multi-manager/lib-*.sh references
# — recursing into whatever THOSE libs source too, so a suite that only
# reaches a lib transitively (lib-A sources lib-B) is still caught — and
# ERRORS if any such lib lacks a LIB_FANOUT entry naming this suite. This is
# what keeps LIB_FANOUT from silently drifting out of sync with the real
# sourcing graph the way a hand-audited doc could (the exact SABLE-7v3z/
# SABLE-lcevs "instrument narrower than the phenomenon" failure class, one
# layer deeper).
#
# Fixture: a throwaway directory tree carrying REAL, unmodified copies of
# .github/ci/shell-run-set.sh and .github/ci/impact-manifest.sh (the actual
# production files — impact-manifest.sh sources shell-run-set.sh exactly as
# it does in production), plus a minimal hooks/multi-manager/ and
# hooks/test/ built per case. ALLOW/EXCLUDE/COVERS/LIB_FANOUT are replaced
# wholesale in the fixture's shell-run-set.sh copy via targeted regex
# substitution (same technique test-tier-ssot-consumers.sh and
# test-shell-run-set-strict.sh already use for their fixture SSOT
# mutations). No git needed — both scripts resolve REPO from their own
# script location.
#
# Run with:
#   bash hooks/test/test-impact-manifest.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PROD_RUNSET="$REPO/.github/ci/shell-run-set.sh"
PROD_MANIFEST="$REPO/.github/ci/impact-manifest.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-test-impact-manifest.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

# new_fixture <name>: sets up <TMPROOT>/<name>/.github/ci/{shell-run-set.sh,
# impact-manifest.sh} (real copies) + empty hooks/test/ + hooks/multi-manager/
# dirs. Echoes the fixture root path.
new_fixture() {
  local dir="$TMPROOT/$1"
  mkdir -p "$dir/.github/ci" "$dir/hooks/test" "$dir/hooks/multi-manager"
  cp "$PROD_RUNSET" "$dir/.github/ci/shell-run-set.sh"
  cp "$PROD_MANIFEST" "$dir/.github/ci/impact-manifest.sh"
  echo "$dir"
}

# set_manifest <fixture> <allow-csv> <exclude-json> <covers-json> <fanout-json>
# Wholesale-replaces ALLOW/EXCLUDE/COVERS/LIB_FANOUT in the fixture's
# shell-run-set.sh copy. JSON values keep key/value pairs unambiguous (a
# COVERS or LIB_FANOUT value may itself contain spaces — multiple covered
# files, or multiple fanned-out suites).
set_manifest() {
  local fixture="$1" allow_csv="$2" exclude_json="$3" covers_json="$4" fanout_json="$5"
  python3 - "$fixture" "$allow_csv" "$exclude_json" "$covers_json" "$fanout_json" <<'PYEOF'
import re, sys, json

fixture, allow_csv, exclude_json, covers_json, fanout_json = sys.argv[1:6]
p = f"{fixture}/.github/ci/shell-run-set.sh"
s = open(p).read()

def replace_block(s, header, body_lines, header_end='\n'):
    pattern = re.escape(header) + r'.*?\n\)\n'
    body = "\n".join(body_lines)
    replacement = f"{header}\n{body}\n)\n"
    new_s, n = re.subn(pattern, replacement.replace('\\', '\\\\'), s, count=1, flags=re.DOTALL)
    assert n == 1, header
    return new_s

allow_items = [a for a in allow_csv.split(",") if a]
s = replace_block(s, "ALLOW=(", [f"  {a}" for a in allow_items])

exclude = json.loads(exclude_json)
s = replace_block(s, "declare -A EXCLUDE=(", [f'  [{k}]="{v}"' for k, v in exclude.items()])

covers = json.loads(covers_json)
s = replace_block(s, "declare -A COVERS=(", [f'  [{k}]="{v}"' for k, v in covers.items()])

fanout = json.loads(fanout_json)
s = replace_block(s, "declare -A LIB_FANOUT=(", [f'  [{k}]="{v}"' for k, v in fanout.items()])

open(p, "w").write(s)
PYEOF
}

mk_lib() {
  # mk_lib <fixture> <lib-name> [source-line]
  local fixture="$1" name="$2" srcline="${3:-}"
  {
    echo '#!/usr/bin/env bash'
    [ -n "$srcline" ] && echo "$srcline"
    echo 'true'
  } > "$fixture/hooks/multi-manager/$name"
}

mk_hook() {
  # mk_hook <fixture> <hook-name> <source-line>
  local fixture="$1" name="$2" srcline="$3"
  {
    echo '#!/usr/bin/env bash'
    echo "$srcline"
    echo 'true'
  } > "$fixture/hooks/multi-manager/$name"
}

mk_suite() {
  local path="$1"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$path"
}

# ---------------------------------------------------------------------------
# Case (a): a suite (via its covered file) sources a lib entirely absent
# from LIB_FANOUT => --check errors, naming the lib.
# ---------------------------------------------------------------------------
FIX_A="$(new_fixture case-a)"
mk_lib "$FIX_A" lib-a.sh
mk_hook "$FIX_A" hook-a.sh '. "$(dirname "${BASH_SOURCE[0]}")/lib-a.sh"'
mk_suite "$FIX_A/hooks/test/test-fixture-a.sh"

set_manifest "$FIX_A" "test-fixture-a.sh" '{}' \
  '{"test-fixture-a.sh": "hooks/multi-manager/hook-a.sh"}' \
  '{}'

OUT_A1=$(bash "$FIX_A/.github/ci/impact-manifest.sh" --check 2>&1); RC_A1=$?
if [ "$RC_A1" -ne 0 ] && printf '%s' "$OUT_A1" | grep -q 'lib-a.sh has no LIB_FANOUT entry'; then
  pass "(a) suite sources a lib with no LIB_FANOUT entry at all -> --check errors, names the lib"
else
  fail "(a) suite sources a lib with no LIB_FANOUT entry at all -> --check errors, names the lib" "rc=$RC_A1 out=$OUT_A1"
fi

# Fixing it (adding the missing entry) clears the error.
set_manifest "$FIX_A" "test-fixture-a.sh" '{}' \
  '{"test-fixture-a.sh": "hooks/multi-manager/hook-a.sh"}' \
  '{"hooks/multi-manager/lib-a.sh": "test-fixture-a.sh"}'

RC_A2=0
bash "$FIX_A/.github/ci/impact-manifest.sh" --check >/dev/null 2>&1 || RC_A2=$?
if [ "$RC_A2" -eq 0 ]; then
  pass "(a) after adding the LIB_FANOUT entry, --check exits 0"
else
  fail "(a) after adding the LIB_FANOUT entry, --check exits 0" "rc=$RC_A2"
fi

# Also cover the "lib IS declared but this suite is missing from its list"
# branch — same fixture, but LIB_FANOUT names a DIFFERENT suite only.
set_manifest "$FIX_A" "test-fixture-a.sh" '{}' \
  '{"test-fixture-a.sh": "hooks/multi-manager/hook-a.sh"}' \
  '{"hooks/multi-manager/lib-a.sh": "some-other-suite.sh"}'

OUT_A3=$(bash "$FIX_A/.github/ci/impact-manifest.sh" --check 2>&1); RC_A3=$?
if [ "$RC_A3" -ne 0 ] && printf '%s' "$OUT_A3" | grep -q "missing from hooks/multi-manager/lib-a.sh's LIB_FANOUT entry"; then
  pass "(a) lib IS declared but this suite is missing from its list -> --check errors"
else
  fail "(a) lib IS declared but this suite is missing from its list -> --check errors" "rc=$RC_A3 out=$OUT_A3"
fi

# ---------------------------------------------------------------------------
# Case (b): both dot-include forms are detected — "source foo.sh" and
# ". foo.sh" — via two suites, each covering a hook that uses one form.
# ---------------------------------------------------------------------------
FIX_B="$(new_fixture case-b)"
mk_lib "$FIX_B" lib-b1.sh
mk_lib "$FIX_B" lib-b2.sh
mk_hook "$FIX_B" hook-b1.sh 'source "$(dirname "${BASH_SOURCE[0]}")/lib-b1.sh"'
mk_hook "$FIX_B" hook-b2.sh '. "$(dirname "${BASH_SOURCE[0]}")/lib-b2.sh"'
mk_suite "$FIX_B/hooks/test/test-fixture-b1.sh"
mk_suite "$FIX_B/hooks/test/test-fixture-b2.sh"

set_manifest "$FIX_B" "test-fixture-b1.sh,test-fixture-b2.sh" '{}' \
  '{"test-fixture-b1.sh": "hooks/multi-manager/hook-b1.sh", "test-fixture-b2.sh": "hooks/multi-manager/hook-b2.sh"}' \
  '{}'

OUT_B1=$(bash "$FIX_B/.github/ci/impact-manifest.sh" --check 2>&1); RC_B1=$?
if [ "$RC_B1" -ne 0 ] \
   && printf '%s' "$OUT_B1" | grep -q 'lib-b1.sh has no LIB_FANOUT entry' \
   && printf '%s' "$OUT_B1" | grep -q 'lib-b2.sh has no LIB_FANOUT entry'; then
  pass "(b) both dot-include forms ('source x.sh' and '. x.sh') are detected as sourcing"
else
  fail "(b) both dot-include forms ('source x.sh' and '. x.sh') are detected as sourcing" "rc=$RC_B1 out=$OUT_B1"
fi

set_manifest "$FIX_B" "test-fixture-b1.sh,test-fixture-b2.sh" '{}' \
  '{"test-fixture-b1.sh": "hooks/multi-manager/hook-b1.sh", "test-fixture-b2.sh": "hooks/multi-manager/hook-b2.sh"}' \
  '{"hooks/multi-manager/lib-b1.sh": "test-fixture-b1.sh", "hooks/multi-manager/lib-b2.sh": "test-fixture-b2.sh"}'

RC_B2=0
bash "$FIX_B/.github/ci/impact-manifest.sh" --check >/dev/null 2>&1 || RC_B2=$?
if [ "$RC_B2" -eq 0 ]; then
  pass "(b) after fanning out both libs, --check exits 0"
else
  fail "(b) after fanning out both libs, --check exits 0" "rc=$RC_B2"
fi

# ---------------------------------------------------------------------------
# Case (c): transitive closure — lib-c-a.sh sources lib-c-b.sh; the covered
# hook sources ONLY lib-c-a.sh directly. LIB_FANOUT for lib-c-a.sh is
# complete but lib-c-b.sh's entry is missing this suite => --check must still
# catch it (proving the closure, not just direct sourcing, is checked).
# ---------------------------------------------------------------------------
FIX_C="$(new_fixture case-c)"
mk_lib "$FIX_C" lib-c-b.sh
mk_lib "$FIX_C" lib-c-a.sh '. "$(dirname "${BASH_SOURCE[0]}")/lib-c-b.sh"'
mk_hook "$FIX_C" hook-c.sh '. "$(dirname "${BASH_SOURCE[0]}")/lib-c-a.sh"'
mk_suite "$FIX_C/hooks/test/test-fixture-c.sh"

set_manifest "$FIX_C" "test-fixture-c.sh" '{}' \
  '{"test-fixture-c.sh": "hooks/multi-manager/hook-c.sh"}' \
  '{"hooks/multi-manager/lib-c-a.sh": "test-fixture-c.sh"}'

OUT_C1=$(bash "$FIX_C/.github/ci/impact-manifest.sh" --check 2>&1); RC_C1=$?
if [ "$RC_C1" -ne 0 ] && printf '%s' "$OUT_C1" | grep -q 'lib-c-b.sh has no LIB_FANOUT entry'; then
  pass "(c) transitive closure: lib-c-a sources lib-c-b, suite only sources lib-c-a directly, lib-c-b's fan-out is still required and missing -> --check errors"
else
  fail "(c) transitive closure: lib-c-a sources lib-c-b, suite only sources lib-c-a directly, lib-c-b's fan-out is still required and missing -> --check errors" "rc=$RC_C1 out=$OUT_C1"
fi

set_manifest "$FIX_C" "test-fixture-c.sh" '{}' \
  '{"test-fixture-c.sh": "hooks/multi-manager/hook-c.sh"}' \
  '{"hooks/multi-manager/lib-c-a.sh": "test-fixture-c.sh", "hooks/multi-manager/lib-c-b.sh": "test-fixture-c.sh"}'

RC_C2=0
bash "$FIX_C/.github/ci/impact-manifest.sh" --check >/dev/null 2>&1 || RC_C2=$?
if [ "$RC_C2" -eq 0 ]; then
  pass "(c) after fanning out the transitively-sourced lib too, --check exits 0"
else
  fail "(c) after fanning out the transitively-sourced lib too, --check exits 0" "rc=$RC_C2"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
