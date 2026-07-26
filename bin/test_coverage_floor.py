#!/usr/bin/env python3
"""Coverage floor on pruning passes (SABLE-cmar4.5, story S3).

PURE unit coverage for sable_coverage_floor_lib: pruning detection from a
unified diff (removed test function, newly-added skip marker, deleted test
file), named-override parsing (mirrors hooks/multi-manager/pre-dispatch-
model-check.sh's "Model override: <reason>" line), and the allow/deny
decision table. No subprocess, no git, no filesystem — exactly the "PURE,
so it is checkable by enumeration" style sable_gate_promote_lib.
decide_promotion already uses for the adjacent base-moved decision.

Real diff-cover, a real checked-out worktree, and the merge gate's actual
deny path (bin/sable_gate_promote_lib.assert_coverage_floor) are exercised
against a REAL temp repo in hooks/test/test-coverage-floor-gate.sh — this
file only covers the pure logic feeding that path.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import sable_coverage_floor_lib as cf  # noqa: E402


# --------------------------------------------------------------------------
# detect_pruning — removed test function
# --------------------------------------------------------------------------

def test_removed_test_function_is_detected():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
index 1111111..2222222 100644
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -10,7 +10,3 @@ def test_helper():
     pass


-def test_edge_case_that_covers_the_branch():
-    assert foo(-1) == "negative"
-
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.removed_test_functions == ["test_edge_case_that_covers_the_branch"]
    assert signal.newly_skipped_markers == 0
    assert signal.deleted_test_files == []


def test_a_moved_test_function_nets_to_zero():
    """Removed in one hunk, re-added elsewhere in the SAME diff (a rename/move)
    is not pruning — only a NET reduction in the function-COUNT counts
    (SABLE-owix4). removed_test_functions/added_test_functions stay raw
    (informational — they still name what moved), the netting happens via
    net_test_function_delta, not via name subtraction."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,3 +1,0 @@
-def test_moved(): pass
diff --git a/bin/test_bar.py b/bin/test_bar.py
--- a/bin/test_bar.py
+++ b/bin/test_bar.py
@@ -0,0 +1,1 @@
+def test_moved(): pass
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    assert signal.net_test_function_delta == 0
    assert signal.removed_test_functions == ["test_moved"]
    assert signal.added_test_functions == ["test_moved"]


def test_renamed_and_strengthened_test_is_not_pruning():
    """The concrete SABLE-owix4 / SABLE-be4lo.7 instance: a test is renamed
    to reflect a STRONGER invariant (two unordered writers pinned ->
    three writers pinned in source order). One def name vanishes and a
    differently-named, stronger successor appears elsewhere in the same
    file. THE PROPERTY THE FLOOR WANTS is "did coverage decrease" — it did
    not, it went up — so this must be allowed silently, with no coverage
    -delta check required at all, exactly like a plain move/rename."""
    diff = """\
diff --git a/bin/test_sable_gate_promote_lib.py b/bin/test_sable_gate_promote_lib.py
--- a/bin/test_sable_gate_promote_lib.py
+++ b/bin/test_sable_gate_promote_lib.py
@@ -50,10 +50,12 @@
-def test_the_module_has_exactly_two_writers_to_the_integration_branch():
-    writers = {"optimus", "tarzan"}
-    assert set(get_writers()) == writers
+def test_the_module_has_exactly_three_writers_to_the_integration_branch():
+    writers = ["optimus", "tarzan", "chuck"]
+    assert get_writers_in_source_order() == writers
+    assert "lincoln" not in get_writers_in_source_order()
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert decision.action == cf.ACTION_ALLOW
    assert "not a pruning diff" in decision.reason


def test_genuine_deletion_with_no_replacement_is_pruning_and_denies():
    """NEGATIVE CONTROL (SABLE-owix4): a test removed with nothing added to
    replace it anywhere in the diff is a real net reduction in the
    function-count and must still be flagged pruning and denied absent a
    passing coverage check or an override — proving the fix does not
    regress into "never deny anything"."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -10,6 +10,0 @@
-def test_edge_case_that_covers_the_branch():
-    assert foo(-1) == "negative"
-
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.net_test_function_delta == -1
    denied_no_check = cf.evaluate_coverage_floor(signal, None, None)
    assert denied_no_check.action == cf.ACTION_DENY
    denied_failed_check = cf.evaluate_coverage_floor(signal, False, None)
    assert denied_failed_check.action == cf.ACTION_DENY


def test_split_test_into_two_is_not_pruning():
    """Recommended additional shape (SABLE-owix4): one test split into two
    is a net INCREASE in function count and must pass, same net-zero-or
    -positive logic as a plain rename."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,4 +1,8 @@
-def test_positive_and_negative():
-    assert foo(1) == 1
-    assert foo(-1) == -1
+def test_positive():
+    assert foo(1) == 1
+
+def test_negative():
+    assert foo(-1) == -1
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    assert signal.net_test_function_delta == 1
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert decision.action == cf.ACTION_ALLOW


def test_parametrized_merge_is_pruning_shaped_but_passes_with_coverage_check():
    """Recommended additional shape (SABLE-owix4): several tests merged into
    one @pytest.mark.parametrize function IS a net DECREASE in def count
    (3 -> 1) by this measure — a static diff scan cannot see that the
    parametrize cases replay every input, so this shape is deliberately
    left pruning-shaped rather than special-cased (that would be exactly
    the "another spelling" trap this bead exists to avoid). It must not be
    allowed outright, and it must not be permanently denied either: routed
    to the real coverage-delta check, a passing result (patch coverage
    held, as it does when the cases are preserved) allows it."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,9 +1,5 @@
-def test_foo_zero():
-    assert foo(0) == 0
-def test_foo_one():
-    assert foo(1) == 1
-def test_foo_negative():
-    assert foo(-1) == -1
+@pytest.mark.parametrize("x,expected", [(0, 0), (1, 1), (-1, -1)])
+def test_foo(x, expected):
+    assert foo(x) == expected
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.net_test_function_delta == -2
    denied = cf.evaluate_coverage_floor(signal, None, None)
    assert denied.action == cf.ACTION_DENY
    allowed = cf.evaluate_coverage_floor(signal, True, None)
    assert allowed.action == cf.ACTION_ALLOW


# --------------------------------------------------------------------------
# detect_pruning — newly-added skip marker
# --------------------------------------------------------------------------

def _one_file_diff(path, added_lines, start=5):
    """A minimal, well-formed one-hunk diff adding `added_lines` to `path`,
    preceded by one context line so the post-image line numbers this module
    reports are actually exercised rather than trivially equal to the hunk
    start."""
    body = "".join("+" + line + "\n" for line in added_lines)
    return (f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -{start},1 +{start},{len(added_lines) + 1} @@\n"
            f" import pytest\n"
            f"{body}")


def test_newly_added_pytest_mark_skip_is_detected():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -5,6 +5,7 @@
 import pytest

+@pytest.mark.skip(reason="flaky")
 def test_edge_case():
     assert foo(-1) == "negative"
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.newly_skipped_markers == 1
    assert signal.removed_test_functions == []


def test_skipif_marker_is_detected_as_pruning():
    """*** THE EXACT REGRESSION (SABLE-a4i8h). *** The predecessor pattern
    ended its first alternative with `@pytest\\.mark\\.skip\\b`, and a word
    boundary requires a word char beside a NON-word char — in `skipif` the
    next char is `i`, a word char, so the boundary is UNSATISFIABLE and the
    single most common conditional-skip form in this repo could never match.
    That is a fail-open on the gate that decides what lands: a pruning diff
    whose markers were all `skipif` scored NOT-pruning and promoted with no
    coverage check at all. This test fails against the pre-fix code."""
    diff = _one_file_diff("bin/test_foo.py", [
        '@pytest.mark.skipif(shutil.which("bd") is None, reason="needs bd")',
        'def test_needs_bd():',
        '    assert bd_version()',
    ])
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.newly_skipped_markers == 1
    assert signal.skip_markers[0].marker == "pytest.mark.skipif"


def test_newly_added_unittest_skip_is_detected():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -5,6 +5,7 @@
 class T(unittest.TestCase):
+    @unittest.skip("wip")
     def test_edge_case(self):
         self.assertEqual(foo(-1), "negative")
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.newly_skipped_markers == 1


def test_two_added_skip_markers_are_both_counted():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,4 +1,6 @@
+@pytest.mark.skip
 def test_a(): pass
+@pytest.mark.skip
 def test_b(): pass
"""
    signal = cf.detect_pruning(diff)
    assert signal.newly_skipped_markers == 2


# --------------------------------------------------------------------------
# detect_pruning — the gating-suppressing marker CLASS
#
# WHY THERE IS A HAND-WRITTEN LIST HERE, AND WHY IT IS NOT THE SAME MISTAKE
# (this bead's own warning: "a table-driven test that enumerates the same
# strings the regex enumerates proves only that two enumerations agree").
# The list below is INPUT DATA — real spellings pytest and unittest actually
# ship, which is why they must be written out somewhere. The IMPLEMENTATION
# holds no matching list: it classifies a marker by a stem predicate
# (`suppresses_gating`), so there is no second enumeration for this one to
# agree with. The load-bearing proof that the fix is class-shaped rather than
# list-shaped is test_unlisted_skip_and_xfail_spellings_are_detected_by_class
# below, which feeds spellings that appear NOWHERE in the module — if someone
# ever replaces the predicate with an alternation of spellings, THAT test
# fails while this one still passes. That is the pair that would rot if
# separated; do not delete one and keep the other.
# --------------------------------------------------------------------------

SUPPRESSING_FORMS = [
    ('@pytest.mark.skip', 'pytest.mark.skip'),
    ('@pytest.mark.skip(reason="flaky")', 'pytest.mark.skip'),
    ('@pytest.mark.skipif(sys.platform == "win32", reason="posix only")',
     'pytest.mark.skipif'),
    ('@pytest.mark.xfail', 'pytest.mark.xfail'),
    ('@pytest.mark.xfail(strict=False)', 'pytest.mark.xfail'),
    ('    pytest.skip("no bd on this host")', 'pytest.skip'),
    ('    pytest.xfail("known broken")', 'pytest.xfail'),
    ('@unittest.skip("wip")', 'unittest.skip'),
    ('@unittest.skipIf(x, "y")', 'unittest.skipIf'),
    ('@unittest.skipUnless(x, "y")', 'unittest.skipUnless'),
    ('@unittest.expectedFailure', 'unittest.expectedFailure'),
    ('        self.skipTest("needs a real db")', 'self.skipTest'),
    ('        raise unittest.SkipTest("needs a real db")', 'unittest.SkipTest'),
]


@pytest.mark.parametrize("line,expected_marker", SUPPRESSING_FORMS)
def test_every_real_pruning_form_is_detected(line, expected_marker):
    """Every form measured MISSING by three agents against the pre-fix
    pattern (3 of 10 matched), plus the two the fleet's sweep did not reach.
    Each is asserted to be detected AND to be NAMED correctly — a detector
    that fires but cannot say what it saw is what made the original 1-vs-4
    disagreement cost a hand-diff."""
    signal = cf.detect_pruning(_one_file_diff("bin/test_foo.py", [line]))
    assert signal.is_pruning, f"{line!r} not detected as pruning"
    assert signal.newly_skipped_markers == 1
    assert signal.skip_markers[0].marker == expected_marker


def test_the_count_equals_the_number_of_markers_planted():
    """*** THE 4-VS-1 BUG ITSELF. *** A test asserting merely "is_pruning is
    True" passes while the count is wrong — the incident that produced this
    bead was a gate reporting 1 where a seat counted 4, on a diff that was
    denied anyway, which is why the defect stayed invisible. So the count,
    not the boolean, is what this asserts, over every form at once."""
    lines = [line for line, _ in SUPPRESSING_FORMS]
    signal = cf.detect_pruning(_one_file_diff("bin/test_foo.py", lines))
    assert signal.newly_skipped_markers == len(SUPPRESSING_FORMS), (
        f"planted {len(SUPPRESSING_FORMS)} markers, detector reported "
        f"{signal.newly_skipped_markers}: "
        f"{[m.marker for m in signal.skip_markers]}")


UNLISTED_SPELLINGS = [
    # None of these strings appear anywhere in sable_coverage_floor_lib.py.
    # They stand in for "the next spelling nobody thought to list", which is
    # how this defect would silently reopen after a spelling-by-spelling fix.
    ('@pytest.mark.skipwhen(SLOW, reason="hypothetical future mark")',
     'pytest.mark.skipwhen'),
    ('@pytest.mark.xfail_on_windows', 'pytest.mark.xfail_on_windows'),
    ('    pytest.skip_module("hypothetical")', 'pytest.skip_module'),
    ('@unittest.skipForNow', 'unittest.skipForNow'),
]


@pytest.mark.parametrize("line,expected_marker", UNLISTED_SPELLINGS)
def test_unlisted_skip_and_xfail_spellings_are_detected_by_class(line, expected_marker):
    """*** THE LOAD-BEARING STRUCTURAL TEST. *** These spellings do not exist
    in pytest or unittest and appear nowhere in the module under test. They
    are detected because the implementation classifies a marker by the stems
    `skip*`/`xfail*` rather than by matching a list of remembered spellings —
    i.e. the fix dissolves the generator of holes instead of patching two of
    its members. Replace `suppresses_gating` with an alternation of real
    spellings and this test goes red while every other test in this file
    still passes."""
    signal = cf.detect_pruning(_one_file_diff("bin/test_foo.py", [line]))
    assert signal.is_pruning
    assert signal.skip_markers[0].marker == expected_marker


NON_SUPPRESSING_FORMS = [
    '@pytest.mark.parametrize("x", [1, 2, 3])',
    '@pytest.mark.usefixtures("tmp_repo")',
    '@pytest.mark.slow',
    '@pytest.fixture',
    '@unittest.mock.patch("bin.foo.bar")',
    '    self.assertEqual(foo(-1), "negative")',
    '    pytest.raises(ValueError)',
    '    pytest.fail("boom")',
]


@pytest.mark.parametrize("line", NON_SUPPRESSING_FORMS)
def test_markers_that_do_not_suppress_gating_are_not_counted(line):
    """KNOWN-NEGATIVE, and it is load-bearing in the other direction. A stem
    predicate is only safe if it is narrow: `parametrize`, `usefixtures` and
    `mock.patch` sit in the same syntactic position as a skip and must not
    count, or the floor fires on ordinary test edits and the seat learns to
    reach for --coverage-override by reflex (SABLE-r5pfw) — strictly worse
    than the under-match this bead fixes. `pytest.fail` is the sharp one: it
    is adjacent vocabulary that does the OPPOSITE of suppressing a result."""
    signal = cf.detect_pruning(_one_file_diff("bin/test_foo.py", [line]))
    assert signal.skip_markers == []
    assert not signal.is_pruning


def test_the_word_skipif_in_a_non_test_file_is_not_counted():
    """KNOWN-NEGATIVE (this bead's third bullet, the easy one to skip). A
    marker only suppresses a test if it lands somewhere pytest COLLECTS, so
    the word in a comment, a docstring or a string literal in an ordinary
    source file — including this floor's own source, which discusses these
    markers at length — prunes nothing and must not be counted."""
    diff = """\
diff --git a/bin/sable_coverage_floor_lib.py b/bin/sable_coverage_floor_lib.py
--- a/bin/sable_coverage_floor_lib.py
+++ b/bin/sable_coverage_floor_lib.py
@@ -40,2 +40,6 @@
 import re
+# @pytest.mark.skipif is the most common conditional-skip form in this repo.
+HINT = "add @pytest.mark.skipif rather than deleting the test"
+DOC = '''pytest.skip("...") inside a test body also prunes.'''
+SKIPIF_HELP = "@unittest.skipUnless(cond, reason)"
"""
    signal = cf.detect_pruning(diff)
    assert signal.skip_markers == []
    assert not signal.is_pruning


def test_a_commented_out_marker_in_a_test_file_is_not_counted():
    """A commented-out decorator is not code and suppresses nothing. Without
    this, a diff that DISABLES a skip (the un-pruning direction) would read
    as one that adds it."""
    diff = _one_file_diff("bin/test_foo.py", [
        '# @pytest.mark.skipif(shutil.which("bd") is None, reason="needs bd")',
        '    # pytest.skip("temporarily disabled while we debug")',
    ])
    signal = cf.detect_pruning(diff)
    assert signal.skip_markers == []
    assert not signal.is_pruning


def test_a_removed_skip_marker_is_not_counted_as_pruning():
    """KNOWN-NEGATIVE, opposite polarity: deleting a skip RE-ENABLES a test.
    Only ADDED markers prune."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -5,7 +5,6 @@
 import pytest
-@pytest.mark.skipif(True, reason="was broken")
-@pytest.mark.xfail
 def test_edge_case():
     assert foo(-1) == "negative"
"""
    signal = cf.detect_pruning(diff)
    assert signal.skip_markers == []
    assert not signal.is_pruning


def test_a_marker_added_to_conftest_is_counted():
    """conftest.py is collected by pytest and its markers apply to whole
    directories of tests, so it is inside the property even though its name
    matches no test_*.py convention."""
    signal = cf.detect_pruning(_one_file_diff(
        "bin/conftest.py",
        ['collect_ignore = ["test_slow.py"]',
         '@pytest.mark.skipif(True, reason="module-wide")']))
    assert signal.is_pruning
    assert signal.skip_markers[0].path == "bin/conftest.py"


def test_each_matched_marker_is_named_with_its_file_and_line():
    """*** ACCEPTANCE CRITERION (SABLE-a4i8h), not polish. *** The incident
    behind this bead cost a seat a hand-diff to settle a 4-vs-1 disagreement
    against a count with no operands. Every match must carry its file, its
    line in the POST-image, and the source line itself, and those must reach
    the deny reason the seat actually reads."""
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -10,4 +10,7 @@ class T:
 import pytest

+@pytest.mark.skipif(shutil.which("bd") is None, reason="needs bd")
 def test_needs_bd():
     assert bd_version()
+
+@pytest.mark.xfail(strict=False)
 def test_flaky():
"""
    signal = cf.detect_pruning(diff)
    assert [(m.path, m.line, m.marker) for m in signal.skip_markers] == [
        ("bin/test_foo.py", 12, "pytest.mark.skipif"),
        ("bin/test_foo.py", 16, "pytest.mark.xfail"),
    ]
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert "bin/test_foo.py:12" in decision.reason
    assert "bin/test_foo.py:16" in decision.reason
    assert "needs bd" in decision.reason


def test_line_numbers_are_reported_per_file_across_a_multi_file_diff():
    """Post-image line numbers reset at each file's hunk header — a counter
    that leaked across files would report plausible-looking but wrong lines,
    which is worse than no line at all because it cannot be spotted by eye."""
    diff = """\
diff --git a/bin/test_a.py b/bin/test_a.py
--- a/bin/test_a.py
+++ b/bin/test_a.py
@@ -100,2 +100,3 @@
 import pytest
+@pytest.mark.skip
 def test_a(): pass
diff --git a/bin/test_b.py b/bin/test_b.py
--- a/bin/test_b.py
+++ b/bin/test_b.py
@@ -3,2 +3,3 @@
 import pytest
+@pytest.mark.skipif(True, reason="x")
 def test_b(): pass
"""
    signal = cf.detect_pruning(diff)
    assert [(m.path, m.line) for m in signal.skip_markers] == [
        ("bin/test_a.py", 101),
        ("bin/test_b.py", 4),
    ]


def test_suppresses_gating_classifies_by_stem_not_by_spelling():
    """The predicate directly, so its shape is pinned independently of the
    diff plumbing: every skip*/xfail* spelling in, the one irregular member
    (`expectedFailure`) in, adjacent test vocabulary out."""
    for name in ("skip", "skipif", "skipIf", "skipUnless", "skipTest",
                 "SkipTest", "xfail", "xfail_slow", "expectedFailure",
                 "skip_this_hypothetical_future_mark"):
        assert cf.suppresses_gating(name), f"{name} should suppress gating"
    for name in ("parametrize", "usefixtures", "fixture", "patch", "fail",
                 "raises", "slow", "expected", "failure"):
        assert not cf.suppresses_gating(name), f"{name} should not suppress gating"


# --------------------------------------------------------------------------
# detect_pruning — deleted test file
# --------------------------------------------------------------------------

def test_deleted_python_test_file_is_detected():
    diff = """\
diff --git a/bin/test_foo.py b/bin/test_foo.py
deleted file mode 100644
index 1111111..0000000
--- a/bin/test_foo.py
+++ /dev/null
@@ -1,5 +0,0 @@
-def test_edge_case():
-    assert foo(-1) == "negative"
"""
    signal = cf.detect_pruning(diff)
    assert signal.is_pruning
    assert signal.deleted_test_files == ["bin/test_foo.py"]


def test_deleted_shell_test_file_is_detected():
    diff = """\
diff --git a/hooks/test/test-something.sh b/hooks/test/test-something.sh
deleted file mode 100755
index 1111111..0000000
--- a/hooks/test/test-something.sh
+++ /dev/null
@@ -1,3 +0,0 @@
-#!/usr/bin/env bash
-echo hi
"""
    signal = cf.detect_pruning(diff)
    assert signal.deleted_test_files == ["hooks/test/test-something.sh"]


def test_deleted_non_test_file_is_not_pruning():
    diff = """\
diff --git a/bin/foo.py b/bin/foo.py
deleted file mode 100644
index 1111111..0000000
--- a/bin/foo.py
+++ /dev/null
@@ -1,3 +0,0 @@
-def foo(x):
-    return x
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    assert signal.deleted_test_files == []


# --------------------------------------------------------------------------
# detect_pruning — negative case
# --------------------------------------------------------------------------

def test_non_pruning_diff_is_not_flagged():
    diff = """\
diff --git a/bin/foo.py b/bin/foo.py
--- a/bin/foo.py
+++ b/bin/foo.py
@@ -1,3 +1,4 @@
 def foo(x):
+    x = abs(x)
     return x
diff --git a/bin/test_foo.py b/bin/test_foo.py
--- a/bin/test_foo.py
+++ b/bin/test_foo.py
@@ -1,3 +1,6 @@
 def test_foo():
     assert foo(1) == 1
+
+def test_foo_negative():
+    assert foo(-1) == 1
"""
    signal = cf.detect_pruning(diff)
    assert not signal.is_pruning
    assert signal.reasons == []


def test_empty_diff_is_not_pruning():
    signal = cf.detect_pruning("")
    assert not signal.is_pruning


# --------------------------------------------------------------------------
# parse_named_override — requires a non-empty reason
# --------------------------------------------------------------------------

def test_override_line_embedded_in_a_text_blob_is_parsed():
    text = (
        "Some bead notes here.\n"
        "Coverage override: removed a flaky test, replacement covers the same lines\n"
        "More notes.\n"
    )
    assert cf.parse_named_override(text) == (
        "removed a flaky test, replacement covers the same lines")


def test_override_is_case_insensitive():
    text = "coverage OVERRIDE: reason goes here"
    assert cf.parse_named_override(text) == "reason goes here"


def test_bare_reason_with_no_tag_is_accepted():
    """sable-merge-gate promote --coverage-override "<reason>" hands the CLI
    value straight through — it does not repeat the tag."""
    assert cf.parse_named_override("dropped test, coverage moved to test_bar.py") == (
        "dropped test, coverage moved to test_bar.py")


def test_empty_string_is_not_an_override():
    assert cf.parse_named_override("") is None


def test_whitespace_only_is_not_an_override():
    assert cf.parse_named_override("   \n\t  ") is None


def test_none_is_not_an_override():
    assert cf.parse_named_override(None) is None


def test_tag_with_no_reason_falls_back_to_bare_text_and_is_not_none():
    """'Coverage override:' with nothing after it never matches the tag regex
    (which requires \\S after the colon) — but the WHOLE blob is still
    non-empty text, so the bare-text fallback would return it verbatim. This
    pins that a blob whose ONLY content is a bare, reason-less tag line is
    exactly the shape that must be rejected — regressed by requiring the tag
    regex to demand a reason, and separately confirming the fallback path
    does not silently launder an empty reason back into a truthy override."""
    assert cf.parse_named_override("Coverage override:") is None
    assert cf.parse_named_override("Coverage override:   ") is None


# --------------------------------------------------------------------------
# evaluate_coverage_floor — the decision table
# --------------------------------------------------------------------------

def _signal(**kw):
    return cf.PruningSignal(**kw)


def _marker(path="bin/test_foo.py", line=12, marker="pytest.mark.skipif",
            text='@pytest.mark.skipif(True, reason="x")'):
    return cf.SkipMarker(path=path, line=line, marker=marker, text=text)


def test_non_pruning_diff_always_allows_regardless_of_check_result():
    clean = _signal()
    for passed in (True, False, None):
        decision = cf.evaluate_coverage_floor(clean, passed, None)
        assert decision.action == cf.ACTION_ALLOW


def test_pruning_with_passing_check_allows():
    signal = _signal(removed_test_functions=["test_x"])
    decision = cf.evaluate_coverage_floor(signal, True, None)
    assert decision.action == cf.ACTION_ALLOW
    assert "passed" in decision.reason


def test_pruning_with_failing_check_denies():
    signal = _signal(removed_test_functions=["test_x"])
    decision = cf.evaluate_coverage_floor(signal, False, None)
    assert decision.action == cf.ACTION_DENY
    assert "FAILED" in decision.reason


def test_pruning_with_no_carried_check_denies():
    """None (not carried / could not run) is NOT treated as 'benefit of the
    doubt' — it denies exactly like a proven failure. Fail-closed, mirroring
    assert_not_frozen's unreadable-freeze-file contract."""
    signal = _signal(deleted_test_files=["bin/test_x.py"])
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert decision.action == cf.ACTION_DENY
    assert "no coverage-delta check" in decision.reason


def test_pruning_with_named_override_allows_even_when_check_failed():
    """The override is a human bypass, checked ahead of the coverage result —
    same contract as promote()'s own --override: 'consults no run at all'."""
    signal = _signal(skip_markers=[_marker()])
    decision = cf.evaluate_coverage_floor(signal, False, "flaky on CI, tracked in SABLE-xyz")
    assert decision.action == cf.ACTION_ALLOW
    assert "flaky on CI, tracked in SABLE-xyz" in decision.reason


def test_pruning_with_named_override_allows_when_check_never_ran():
    signal = _signal(skip_markers=[_marker()])
    decision = cf.evaluate_coverage_floor(signal, None, "reason")
    assert decision.action == cf.ACTION_ALLOW


def test_decision_reason_names_every_pruning_signal():
    signal = _signal(removed_test_functions=["test_a"],
                     skip_markers=[_marker(line=12),
                                   _marker(path="bin/test_c.py", line=40)],
                     deleted_test_files=["bin/test_b.py"])
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert "test_a" in decision.reason
    assert "2 newly-added skip/xfail marker" in decision.reason
    assert "bin/test_foo.py:12" in decision.reason
    assert "bin/test_c.py:40" in decision.reason
    assert "bin/test_b.py" in decision.reason


def test_a_very_long_marker_line_is_truncated_in_the_report():
    """The report NAMES every match, and a pruning diff can legitimately
    carry many — a deny reason that pastes a 400-char skipif verbatim per
    marker becomes unreadable, which defeats the point of naming them."""
    long_reason = "x" * 400
    signal = _signal(skip_markers=[
        _marker(text=f'@pytest.mark.skipif(True, reason="{long_reason}")')])
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert "bin/test_foo.py:12" in decision.reason
    assert long_reason not in decision.reason
    assert "..." in decision.reason
