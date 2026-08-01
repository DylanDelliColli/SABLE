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

# SABLE-y4nom.7.1: blank the production classification tables — their keys
# name real-repo paths that do not exist in these minimal fixture universes,
# and the table-key hygiene guard (rows must point at present paths) would
# otherwise fail every happy-path --check here.
s = replace_block(s, "declare -A ZERO_IMPACT=(", [])
s = replace_block(s, "declare -A DECLARED_BROAD=(", [])
s = replace_block(s, "PY_OWNED=(", [])

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

# ---------------------------------------------------------------------------
# Case (d): a production Python module with its convention-mandated pytest
# companion is owned by the Python selector. With no explicit COVERS entry it
# selects zero shell suites rather than the full ALLOW set. Removing the
# companion makes the module an EXISTING unclassified path, which is a loud
# UNKNOWN error naming its remediation (SABLE-y4nom.7.1 — the old
# conservative full-shell fallback is retired).
# ---------------------------------------------------------------------------
FIX_D="$(new_fixture case-d)"
mkdir -p "$FIX_D/bin"
mk_suite "$FIX_D/hooks/test/test-fixture-d.sh"
printf 'VALUE = 1\n' > "$FIX_D/bin/widget.py"
printf 'def test_widget(): assert True\n' > "$FIX_D/bin/test_widget.py"
set_manifest "$FIX_D" "test-fixture-d.sh" '{}' '{}' '{}'

OUT_D1=$(bash "$FIX_D/.github/ci/impact-manifest.sh" --select bin/widget.py 2>"$FIX_D/stderr"); RC_D1=$?
ERR_D1=$(cat "$FIX_D/stderr")
if [ "$RC_D1" -eq 0 ] && [ -z "$OUT_D1" ] && printf '%s' "$ERR_D1" | grep -q 'SCOPED'; then
  pass "(d) bin/X.py with bin/test_X.py and no shell COVERS -> scoped zero shell suites"
else
  fail "(d) bin/X.py with bin/test_X.py and no shell COVERS -> scoped zero shell suites" "rc=$RC_D1 out=$OUT_D1 err=$ERR_D1"
fi

# REVISED under SABLE-y4nom.7.1: the claim — losing the companion is LOUD,
# never silently scoped — is preserved and strengthened. An existing path
# with no classification is now an UNKNOWN error naming itself and the
# remediation menu (nonzero, empty stdout), not a full-set escalation.
rm -f "$FIX_D/bin/test_widget.py"
OUT_D2=$(bash "$FIX_D/.github/ci/impact-manifest.sh" --select bin/widget.py 2>"$FIX_D/stderr"); RC_D2=$?
ERR_D2=$(cat "$FIX_D/stderr")
if [ "$RC_D2" -ne 0 ] && [ -z "$OUT_D2" ] \
   && printf '%s' "$ERR_D2" | grep -q 'UNKNOWN -- unclassified path: bin/widget.py'; then
  pass "(d, y4nom.7.1-revised) removing the Python companion is a loud UNKNOWN error, never silently scoped"
else
  fail "(d, y4nom.7.1-revised) removing the Python companion is a loud UNKNOWN error, never silently scoped" "rc=$RC_D2 out=$OUT_D2 err=$ERR_D2"
fi

printf '#!/usr/bin/env python3\nVALUE = 1\n' > "$FIX_D/bin/sable-widget"
printf 'def test_widget(): assert True\n' > "$FIX_D/bin/test_sable_widget_lib.py"
OUT_D3=$(bash "$FIX_D/.github/ci/impact-manifest.sh" --select bin/sable-widget 2>"$FIX_D/stderr"); RC_D3=$?
ERR_D3=$(cat "$FIX_D/stderr")
if [ "$RC_D3" -eq 0 ] && [ -z "$OUT_D3" ] && printf '%s' "$ERR_D3" | grep -q 'SCOPED'; then
  pass "(d) extensionless Python CLI with matching pytest module -> scoped zero shell suites"
else
  fail "(d) extensionless Python CLI with matching pytest module -> scoped zero shell suites" "rc=$RC_D3 out=$OUT_D3 err=$ERR_D3"
fi

# ---------------------------------------------------------------------------
# Case (e): the golden-manifest pin rule (SABLE-slip0.7). A changed path that
# hooks/test/fixtures/install-golden-manifest.txt pins a hash FOR also selects
# test-install-golden-manifest.sh — the suite that compares each pin against
# sha256 of its repo source. That is what stops "edit an installed artifact,
# forget its pin" from passing a proportional run, which is how three golden
# entries had already gone stale unnoticed.
#
# Exercised here against a SYNTHETIC golden, so these assertions test the RULE
# (does the selector read the manifest it is given?) rather than today's real
# artifact list — test-impact-selection.sh case 9 covers the real declarations.
# ---------------------------------------------------------------------------
FIX_E="$(new_fixture case-e)"
mkdir -p "$FIX_E/hooks/test/fixtures"
cp "$REPO/hooks/test/lib-golden-manifest.sh" "$FIX_E/hooks/test/lib-golden-manifest.sh"
mk_lib "$FIX_E" lib-e.sh
mk_hook "$FIX_E" hook-e.sh '. "$(dirname "${BASH_SOURCE[0]}")/lib-e.sh"'
mk_suite "$FIX_E/hooks/test/test-fixture-e.sh"
mk_suite "$FIX_E/hooks/test/test-install-golden-manifest.sh"

E_GOLDEN="$FIX_E/hooks/test/fixtures/install-golden-manifest.txt"
E_HASH="1111111111111111111111111111111111111111111111111111111111111111"
# A pinned verbatim artifact, plus a GOLDEN_DERIVED entry
# (./.sable-install-provenance is written per-install, not copied) that must
# NOT become a pinned source. y4nom.7.1 note: the derived probe deliberately
# avoids CLAUDE.md — the production ZERO_IMPACT table (inherited by this
# fixture copy) classifies CLAUDE.md, which would mask the derived-entry
# outcome under the new class semantics.
{
  printf '%s  ./hooks/multi-manager/hook-e.sh\n' "$E_HASH"
  printf '%s  ./.sable-install-provenance\n' "$E_HASH"
} > "$E_GOLDEN"

set_manifest "$FIX_E" "test-fixture-e.sh,test-install-golden-manifest.sh" '{}' \
  '{"test-fixture-e.sh": "hooks/multi-manager/hook-e.sh"}' \
  '{"hooks/multi-manager/lib-e.sh": "test-fixture-e.sh"}'

E_SEL=$(bash "$FIX_E/.github/ci/impact-manifest.sh" --select hooks/multi-manager/hook-e.sh 2>/dev/null | sort)
E_EXPECTED=$(printf 'test-fixture-e.sh\ntest-install-golden-manifest.sh' | sort)
if [ "$E_SEL" = "$E_EXPECTED" ]; then
  pass "(e) a golden-pinned artifact selects BOTH its own covering suite and the golden suite"
else
  fail "(e) a golden-pinned artifact selects BOTH its own covering suite and the golden suite" "got: $E_SEL"
fi

# A GOLDEN_DERIVED entry is excused from the hash check, so it must not be
# published as a pinned source either — otherwise the two halves of the lib
# would disagree about what the manifest covers.
# REVISED under SABLE-y4nom.7.1: "not silently golden-covered" now lands in
# the UNKNOWN-error branch — an existing path with no classification (and
# deliberately NO golden pin) demands its class loudly, which is a stronger
# form of the same claim than the old full-set escalation. The path is
# created so the case exercises the existing-unknown branch, not the
# deletion class.
echo derived-fixture > "$FIX_E/.sable-install-provenance"
E_DERIVED_OUT=$(bash "$FIX_E/.github/ci/impact-manifest.sh" --select .sable-install-provenance 2>"$FIX_E/stderr"); E_DERIVED_RC=$?
E_DERIVED_NOTICE=$(cat "$FIX_E/stderr")
if [ "$E_DERIVED_RC" -ne 0 ] && [ -z "$E_DERIVED_OUT" ] \
   && printf '%s' "$E_DERIVED_NOTICE" | grep -q 'UNKNOWN -- unclassified path: .sable-install-provenance'; then
  pass "(e, y4nom.7.1-revised) a GOLDEN_DERIVED golden entry is not a pinned source — it errors as unclassified, never silently golden-covered"
else
  fail "(e, y4nom.7.1-revised) a GOLDEN_DERIVED golden entry is not a pinned source — it errors as unclassified, never silently golden-covered" \
       "rc=$E_DERIVED_RC out=[$E_DERIVED_OUT] notice: $E_DERIVED_NOTICE"
fi

# NEGATIVE CONTROL: drop the artifact's line from the golden. The same path must
# stop selecting the golden suite — proving the selection is READ FROM the
# manifest, not hardcoded against a path list that would rot beside it.
printf '%s  ./CLAUDE.md\n' "$E_HASH" > "$E_GOLDEN"
E_SEL2=$(bash "$FIX_E/.github/ci/impact-manifest.sh" --select hooks/multi-manager/hook-e.sh 2>/dev/null | sort)
if [ "$E_SEL2" = "test-fixture-e.sh" ]; then
  pass "(e) NEGATIVE CONTROL: un-pinning the artifact stops selecting the golden suite (the rule reads the manifest)"
else
  fail "(e) NEGATIVE CONTROL: un-pinning the artifact stops selecting the golden suite (the rule reads the manifest)" "got: $E_SEL2"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
