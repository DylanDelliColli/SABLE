#!/usr/bin/env python3
"""test_sable_vacuous_guard_scan — unit + integration tests for
bin/sable-vacuous-guard-scan (SABLE-5lli.4, S1 STRUCTURE bash surface).

UNIT: flags the real i8kv create-then-assert-exists shape (reproduced
verbatim from commit 94d2557), does NOT flag a shared-helper guard that
routes the check through a named predicate function.

BITE-PROOF: neutering the detector's assertion-matcher (hardcode no-match)
must turn the i8kv-flag case red — proving the flag actually depends on the
matcher, not a fixture that would pass regardless.

INTEGRATION (E2E): runs the detector over the vendored I8KV_BLOCK fixture —
a verbatim, working-tree-only copy of
`git show 94d2557:hooks/test/test-orchestration-install.sh` lines 92-113
(pre-fix, must flag) — vs the same file at current HEAD (post SABLE-f00o
fix, home_has_timer_unit shared predicate, must NOT flag). A separate drift
check confirms the vendored fixture still matches the real 94d2557 artifact
when that commit is reachable, and SKIPS LOUDLY (never silently, never as a
false pass/fail) when it isn't — e.g. in a ci-verify clean-room checkout
that hasn't fetched that history.

SLICE-THEN-ASSERT (SABLE-p9n7k): a second detector shape, added to the same
scanner rather than a new tool. Flags a collection built by bash array
slicing (`TARGET=("${SRC[@]:OFF:CNT}")`) or a filtered `mapfile` read, then
consumed by a `for ITEM in "${TARGET[@]}"; do ... done` loop that asserts on
each item with no `${#TARGET[@]}`-style non-emptiness guard anywhere in
between. This is the bash transliteration of the real SABLE-pk15w defect: a
`lead = samples[:n]` slice asserted over with no length guard, which passed
44/44 vacuously on a fast CI runner (the slice landed empty) and produced a
manager SIGN-OFF rather than a caught bug. An E2E integration case runs the
real scanner binary as a subprocess against a vendored fixture file
reproducing that exact empty-lead-slice shape, asserting the correct
file:line.

Run with:

  python3 bin/test_sable_vacuous_guard_scan.py

Exits 0 if all pass, 1 if any fail. No pytest dependency.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TOOL_PATH = SCRIPT_DIR / "sable-vacuous-guard-scan"
_LOADER = SourceFileLoader("sable_vacuous_guard_scan", str(TOOL_PATH))
_SPEC = importlib.util.spec_from_loader("sable_vacuous_guard_scan", _LOADER)
vg = importlib.util.module_from_spec(_SPEC)
sys.modules["sable_vacuous_guard_scan"] = vg
_LOADER.exec_module(vg)


PASS = 0
FAIL = 0
FAILED_NAMES: list[str] = []


def assert_eq(name: str, got, expected):
    global PASS, FAIL
    if got == expected:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"FAIL: {name}")
        print(f"  expected: {expected!r}")
        print(f"  got:      {got!r}")


def assert_true(name: str, cond, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS: {name}")
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print(f"FAIL: {name}{(' — ' + detail) if detail else ''}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Verbatim from `git show 94d2557:hooks/test/test-orchestration-install.sh`
# (lines 92-113). Line 18 of this block (`if [ -e "$HS/..." ]; then` right
# after the `touch` on line 17) is the proven SABLE-i8kv vacuous guard: it
# only re-observes state the block itself just wrote.
I8KV_BLOCK = """\
# the install itself must never actually touch a live systemd/cron surface.
# SABLE-i8kv: run this against a SANDBOXED HOME, not the developer's real one —
# on a host where the real D5 reconcile-timer is legitimately installed (e.g.
# o9ru), checking the unsandboxed $HOME conflates "install wrote here" with
# "this developer happens to run the timer" and fails for the right answer.
HS="$(mktemp -d)"; mkdir -p "$HS/.config/systemd/user"
HP="$(mktemp -d)"
HOME="$HS" SABLE_PROJECT_DIR="$HP" bash "$INSTALLER" --project >/dev/null 2>&1
if [ -e "$HS/.config/systemd/user/sable-reconcile-timer.timer" ]; then
  fail "project: install does not copy the unit into the real ~/.config/systemd/user" "found $HS/.config/systemd/user/sable-reconcile-timer.timer"
else
  pass "project: install does not copy the unit into the real ~/.config/systemd/user"
fi

# guard: plant a unit inside the SANDBOXED HOME and confirm the assertion still
# bites — proving the sandbox did not neuter the check into a vacuous pass.
touch "$HS/.config/systemd/user/sable-reconcile-timer.timer"
if [ -e "$HS/.config/systemd/user/sable-reconcile-timer.timer" ]; then
  pass "project: assertion still bites when a unit IS present under sandboxed HOME (guard)"
else
  fail "project: assertion still bites when a unit IS present under sandboxed HOME (guard)"
fi
"""
I8KV_GUARD_LINE = 18  # 1-indexed line within I8KV_BLOCK

# Synthetic shared-helper guard, mirroring the repo's real SABLE-f00o fix
# (home_has_timer_unit): the guard re-invokes the SAME predicate function the
# real assertion calls, instead of re-reading the raw filesystem state.
SHARED_HELPER_BLOCK = """\
file_present(){ [ -e "$1/marker.timer" ]; }

X="$(mktemp -d)"
if file_present "$X"; then
  fail "marker present before install"
else
  pass "marker absent before install"
fi

# guard: plant the marker and re-invoke the SAME predicate.
touch "$X/marker.timer"
if file_present "$X"; then
  pass "assertion still bites when marker IS present (guard)"
else
  fail "assertion still bites when marker IS present (guard)"
fi
"""

# A real (non-vacuous) assertion: a write, then a call into the code under
# test, then the check — nothing is immediately adjacent, so it must not fire.
NON_ADJACENT_BLOCK = """\
touch "$RA/agents/my-custom-agent.md"
touch "$RA/agents/optimus.md"
RA_OUT="$(SABLE_PROJECT_DIR="$RA" bash "$INSTALLER" --project 2>&1)"
if [ ! -e "$RA/agents/optimus.md" ]; then pass "retired agent removed"; else fail "retired agent removed"; fi
if [ -e "$RA/agents/my-custom-agent.md" ]; then pass "custom agent survives"; else fail "custom agent survives"; fi
"""

# The bash transliteration of the real SABLE-pk15w defect: a `lead` window is
# sliced off a sampled array, then asserted over item-by-item with NO check
# that the slice actually contains anything. On a host where the slice
# happens to land empty, the `for` loop runs zero times and every assertion
# inside it is skipped — same green as a real pass, per the summary-line
# invisibility this bead is filed against.
SLICE_THEN_ASSERT_BLOCK = """\
mapfile -t SAMPLES < <(printf '%s\\n' clear clear busy busy clear)
LEAD=("${SAMPLES[@]:0:3}")
for s in "${LEAD[@]}"; do
  if [ "$s" = "busy" ]; then
    fail "no busy sample in lead window"
  else
    pass "lead window sample is clear"
  fi
done
"""
SLICE_ASSERT_LOOP_LINE = 3  # 1-indexed line within SLICE_THEN_ASSERT_BLOCK

# Identical shape, but with an explicit non-emptiness guard before the loop
# (the repo's own escape, mirroring the real `[ "${#HOME_MARKERS[@]}" -eq 2 ]`
# guard at hooks/test/test-impact-tier-serialization.sh:335) — must NOT flag.
SLICE_WITH_GUARD_BLOCK = """\
mapfile -t SAMPLES < <(printf '%s\\n' clear clear busy busy clear)
LEAD=("${SAMPLES[@]:0:3}")
if [ "${#LEAD[@]}" -eq 0 ]; then
  fail "lead window unexpectedly empty"
fi
for s in "${LEAD[@]}"; do
  if [ "$s" = "busy" ]; then
    fail "no busy sample in lead window"
  else
    pass "lead window sample is clear"
  fi
done
"""


# ---------------------------------------------------------------------------
# UNIT
# ---------------------------------------------------------------------------

def test_flags_real_i8kv_shape():
    violations = vg.scan_bash_text(I8KV_BLOCK, "f.sh")
    assert_eq("flags exactly one violation in the i8kv block", len(violations), 1)
    if violations:
        assert_eq("flags the correct line", violations[0].line, I8KV_GUARD_LINE)


def test_does_not_flag_shared_helper_guard():
    violations = vg.scan_bash_text(SHARED_HELPER_BLOCK, "f.sh")
    assert_eq("shared-helper guard produces no violations", violations, [])


def test_does_not_flag_non_adjacent_real_assertion():
    violations = vg.scan_bash_text(NON_ADJACENT_BLOCK, "f.sh")
    assert_eq("non-adjacent real assertion produces no violations", violations, [])


def test_touch_immediately_followed_by_check_on_different_target_not_flagged():
    text = 'touch "$A"\nif [ -e "$B" ]; then pass x; else fail x; fi\n'
    assert_eq("different target not flagged", vg.scan_bash_text(text, "f.sh"), [])


def test_echo_redirect_creation_flagged():
    text = 'echo "data" > "$LOG"\nif [ -e "$LOG" ]; then pass x; else fail x; fi\n'
    violations = vg.scan_bash_text(text, "f.sh")
    assert_eq("echo redirect + raw check flagged", len(violations), 1)


def test_test_dash_e_form_flagged():
    text = 'touch "$X"\ntest -e "$X" && pass ok || fail no\n'
    violations = vg.scan_bash_text(text, "f.sh")
    assert_eq("`test -e` form flagged", len(violations), 1)


def test_blank_and_comment_lines_do_not_break_adjacency():
    text = 'touch "$X"\n\n# still the same guard\nif [ -e "$X" ]; then pass ok; else fail no; fi\n'
    violations = vg.scan_bash_text(text, "f.sh")
    assert_eq("blank/comment-tolerant adjacency still flags", len(violations), 1)


def test_flags_slice_then_assert_without_nonempty_guard():
    violations = vg.scan_bash_text(SLICE_THEN_ASSERT_BLOCK, "f.sh")
    assert_eq("flags exactly one slice-then-assert violation", len(violations), 1)
    if violations:
        assert_eq("flags the for-loop line", violations[0].line, SLICE_ASSERT_LOOP_LINE)
        assert_true(
            "finding names the non-empty guard form",
            "non-empty" in violations[0].fix,
            detail=f"fix={violations[0].fix!r}",
        )


def test_does_not_flag_slice_with_nonempty_guard():
    # Negative control (SABLE-rhsuj false-positive law): the identical slice
    # shape, plus an explicit `${#LEAD[@]}` guard, must NOT be flagged — a
    # matcher that flags every slice regardless of guarding is noise, not a
    # detector.
    violations = vg.scan_bash_text(SLICE_WITH_GUARD_BLOCK, "f.sh")
    assert_eq("guarded slice produces no violations", violations, [])


# ---------------------------------------------------------------------------
# BITE-PROOF
# ---------------------------------------------------------------------------

def test_bite_proof_neutered_matcher_goes_red():
    original = vg._match_assertion
    vg._match_assertion = lambda masked, orig: None
    try:
        neutered = vg.scan_bash_text(I8KV_BLOCK, "f.sh")
    finally:
        vg._match_assertion = original
    # With the assertion matcher neutered, the proven i8kv case must NOT be
    # flagged — i.e. the real (unneutered) matcher above is load-bearing for
    # test_flags_real_i8kv_shape, not incidental.
    assert_eq("neutered matcher misses the i8kv guard (bite-proof)", neutered, [])
    real = vg.scan_bash_text(I8KV_BLOCK, "f.sh")
    assert_true("unneutered matcher still flags it", len(real) == 1)


def test_bite_proof_neutered_empty_slice_matcher_goes_red():
    # Mirrors test_bite_proof_neutered_matcher_goes_red above, for form 2:
    # neuter the assert-call detector the slice-then-assert pass depends on
    # and confirm the proven SLICE_THEN_ASSERT_BLOCK case stops being
    # flagged — proving the flag depends on the matcher, not a fixture that
    # would pass regardless.
    original = vg._has_assert_call
    vg._has_assert_call = lambda code: False
    try:
        neutered = vg.scan_bash_text(SLICE_THEN_ASSERT_BLOCK, "f.sh")
    finally:
        vg._has_assert_call = original
    assert_eq(
        "neutered assert-call matcher misses the slice-then-assert case (bite-proof)",
        neutered, [],
    )
    real = vg.scan_bash_text(SLICE_THEN_ASSERT_BLOCK, "f.sh")
    assert_true("unneutered matcher still flags it", len(real) == 1)


# ---------------------------------------------------------------------------
# INTEGRATION (E2E) — real revision history
# ---------------------------------------------------------------------------

def test_integration_flags_i8kv_at_94d2557():
    # Depends ONLY on the working tree (the vendored I8KV_BLOCK fixture),
    # not on `git show 94d2557` — that history is not fetched in the
    # ci-verify clean-room checkout, so shelling out here was an
    # environment-dependent assertion masquerading as a content one
    # (chuck, ci-verify run 29872346417). Drift between the vendored
    # fixture and the real historical artifact is covered separately by
    # test_vendored_i8kv_fixture_matches_94d2557_when_reachable below.
    violations = vg.scan_bash_text(I8KV_BLOCK, "test-orchestration-install.sh@94d2557(vendored)")
    assert_true(
        "flags the i8kv block (vendored fixture of commit 94d2557)",
        any("sable-reconcile-timer.timer" in v.snippet for v in violations),
        detail=f"violations={violations!r}",
    )


def test_vendored_i8kv_fixture_matches_94d2557_when_reachable():
    # Drift check, not a content assertion: confirms the vendored
    # I8KV_BLOCK fixture used above is still byte-identical to the real
    # historical artifact, so vendoring didn't silently detach the fixture
    # from the thing it claims to represent. This is allowed to depend on
    # commit reachability (unlike the test above) because it SKIPS LOUDLY
    # rather than failing when the environment can't supply the answer —
    # it never reports a false pass/fail on an environment property.
    repo_root = vg.repo_root_of(SCRIPT_DIR)
    reachable = subprocess.run(
        ["git", "cat-file", "-e", "94d2557"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if reachable.returncode != 0:
        print(
            "SKIP: test_vendored_i8kv_fixture_matches_94d2557_when_reachable "
            "— 94d2557 not fetched in this checkout; vendored I8KV_BLOCK "
            "fixture drift vs the real historical artifact is unverified"
        )
        return
    proc = subprocess.run(
        ["git", "show", "94d2557:hooks/test/test-orchestration-install.sh"],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )
    lines = proc.stdout.splitlines(keepends=True)
    actual = "".join(lines[91:113])  # 1-indexed lines 92-113, inclusive
    assert_eq(
        "vendored I8KV_BLOCK fixture is byte-identical to git show 94d2557 lines 92-113",
        actual, I8KV_BLOCK,
    )


def test_integration_does_not_flag_head_shared_helper():
    repo_root = vg.repo_root_of(SCRIPT_DIR)
    target = repo_root / "hooks" / "test" / "test-orchestration-install.sh"
    assert_true("current HEAD file exists", target.is_file())
    text = target.read_text()
    assert_true(
        "current HEAD still carries the home_has_timer_unit shared predicate",
        "home_has_timer_unit" in text,
    )
    violations = vg.scan_bash_text(text, "test-orchestration-install.sh@HEAD")
    reconcile_hits = [v for v in violations if "sable-reconcile-timer.timer" in v.snippet]
    assert_eq(
        "does not flag the current HEAD sandboxed-HOME guard",
        reconcile_hits, [],
    )


def test_integration_real_binary_flags_vendored_pk15w_fixture():
    # Runs the REAL scanner binary (subprocess, not a direct function call)
    # against a vendored fixture file reproducing the pk15w empty-lead-slice
    # shape, and checks the reported file:line — the strongest form of this
    # bead's test spec, since it exercises the actual CLI a suite author
    # would invoke rather than only the library function.
    with tempfile.TemporaryDirectory() as td:
        fixture_path = Path(td) / "test-pk15w-empty-lead-slice.sh"
        fixture_path.write_text(SLICE_THEN_ASSERT_BLOCK)
        proc = subprocess.run(
            [sys.executable, str(TOOL_PATH), str(fixture_path)],
            capture_output=True, text=True,
        )
        assert_eq(
            "real binary exits 1 (violation found) on the vendored pk15w fixture",
            proc.returncode, 1,
        )
        expected_marker = f"{fixture_path}:{SLICE_ASSERT_LOOP_LINE}"
        assert_true(
            "real binary reports the correct file:line for the vendored pk15w fixture",
            expected_marker in proc.stdout,
            detail=f"stdout={proc.stdout!r}",
        )


# ---------------------------------------------------------------------------
# REGRESSION — CLI wiring + real default-corpus sanity
# ---------------------------------------------------------------------------

def test_main_clean_on_real_repo_corpus():
    assert_eq("main() is clean over the repo's real bash test corpus", vg.main([]), 0)


TESTS = [
    test_flags_real_i8kv_shape,
    test_does_not_flag_shared_helper_guard,
    test_does_not_flag_non_adjacent_real_assertion,
    test_touch_immediately_followed_by_check_on_different_target_not_flagged,
    test_echo_redirect_creation_flagged,
    test_test_dash_e_form_flagged,
    test_blank_and_comment_lines_do_not_break_adjacency,
    test_flags_slice_then_assert_without_nonempty_guard,
    test_does_not_flag_slice_with_nonempty_guard,
    test_bite_proof_neutered_matcher_goes_red,
    test_bite_proof_neutered_empty_slice_matcher_goes_red,
    test_integration_flags_i8kv_at_94d2557,
    test_vendored_i8kv_fixture_matches_94d2557_when_reachable,
    test_integration_does_not_flag_head_shared_helper,
    test_integration_real_binary_flags_vendored_pk15w_fixture,
    test_main_clean_on_real_repo_corpus,
]


def main() -> int:
    for t in TESTS:
        try:
            t()
        except Exception as e:
            global FAIL
            FAIL += 1
            FAILED_NAMES.append(f"{t.__name__} (raised {type(e).__name__})")
            print(f"FAIL: {t.__name__} — raised {type(e).__name__}: {e}")
    print()
    print("==========================================")
    print(f"Tests: {PASS + FAIL} | Passed: {PASS} | Failed: {FAIL}")
    print("==========================================")
    if FAIL:
        print("Failed:")
        for n in FAILED_NAMES:
            print(f"  {n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
