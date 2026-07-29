#!/usr/bin/env python3
"""sable_coverage_floor_lib — coverage floor on pruning passes (SABLE-cmar4.5).

Story S3 (locked: diff-cover patch-coverage semantics — strict patch gate,
lenient project floor). A PRUNING diff — one that removes a test function,
adds a skip marker, or deletes a test file — is not inherently wrong, but it
is exactly the shape of change that can silently erase coverage without any
other signal noticing. This module gives the merge gate a mechanical answer
to "did this pruning diff carry a coverage-delta check": detect pruning from
a unified diff (PURE, no I/O — unit-tested directly), and decide whether a
pruning diff may promote given the real result of that check (also PURE,
enumerable like sable_gate_promote_lib.decide_promotion).

Actually RUNNING the check (pytest + coverage + diff-cover against a checked
-out tree) is I/O and lives in sable_gate_promote_lib.run_coverage_floor_check,
which shells out to .github/ci/diff-cover-gate.sh — the invocation itself is
kept in .github/ci/ so it can be run standalone (by hand, or by CI) the same
way the merge gate runs it.

KNOWN RESIDUAL (accepted at the TEST-STRATEGY gate, cmar4 S3.7): this floor
has teeth only on the pytest/bin/ half. A weakened hooks/test/*.sh suite BODY
is invisible to diff-cover (it is not a coverage.py/pytest tool); only a
removed MAPPING is caught, by the separate manifest-completeness check
(SABLE-cmar4.2). Do not extend this module to fake shell coverage — that is
the accepted gap, not a bug in this file.

FIXED DEFECT (SABLE-owix4): detect_pruning's function-removal leg used to
ask "did a specific def NAME disappear from the diff", which made a test
RENAME invisible to it BY CONSTRUCTION — the same is true of any refactor
that moves a test between files, splits one test into two, or parametrizes
several into one, all of which INCREASE coverage. It now asks the property
the floor actually wants — "did the SIZE of the test-function set go down"
(PruningSignal.net_test_function_delta) — so a rename, move, or split net to
zero-or-positive and pass silently; only a genuine reduction in the number
of test functions is reported as pruning. See detect_pruning's own
docstring for the full reasoning, including why a parametrize-merge is
deliberately left pruning-shaped rather than special-cased.

FIXED DEFECT (SABLE-a4i8h): the skip-marker leg — a SEPARATE leg owix4 left
untouched — used to be a three-way alternation of literal spellings
(`@pytest\\.mark\\.skip\\b|@unittest\\.skip\\b|pytest\\.skip\\(`) and matched
3 of 10 real pruning forms, executed and reconciled by three agents. The
word boundary after `skip` is UNSATISFIABLE against `skipif`/`skipIf`
(`i`/`I` are word chars), so the most common conditional-skip form in this
repo was invisible, as were every `xfail` form and `unittest.expected
Failure`. That is a FAIL-OPEN on the gate that decides what lands. The leg
now classifies markers by a PREDICATE over the marker's own name rather
than by an alternation of spellings — see the block comment above
`suppresses_gating` for the property it stands in for, what enumeration
survives, why deriving from pytest's marker vocabulary at runtime was
rejected, and the residuals no text scan can reach.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Pruning detection — PURE, operates on unified diff text only
# --------------------------------------------------------------------------

_DIFF_HEADER_RE = re.compile(r'^diff --git a/(\S+) b/(\S+)')
_DELETED_MODE_RE = re.compile(r'^deleted file mode')
_HUNK_HEADER_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')
_REMOVED_TEST_FN_RE = re.compile(r'^-\s*(?:async\s+)?def\s+(test_\w+)\s*\(')
_ADDED_TEST_FN_RE = re.compile(r'^\+\s*(?:async\s+)?def\s+(test_\w+)\s*\(')
# Naming conventions this repo actually uses (see CLAUDE.md: bin/test_*.py,
# hooks/test/test-*.sh) plus the common test_foo.py / foo_test.py shapes.
_TEST_FILE_RE = re.compile(
    r'(^|/)(test_[\w]+\.py|[\w]+_test\.py|test-[\w-]+\.sh)$')

# --------------------------------------------------------------------------
# GATING-SUPPRESSING MARKERS (SABLE-a4i8h)
#
# *** THE PROPERTY THIS LEG STANDS IN FOR: A MARKER THAT PREVENTS A TEST FROM
# CONTRIBUTING TO THE GATE. *** Everything below is an approximation of that
# property by TEXT, and the accuracy of the approximation is measurable — the
# predecessor alternation of literal spellings measured 3 of 10 real forms.
#
# WHY NOT AN ALTERNATION OF SPELLINGS. `skip|unittest.skip|pytest.skip(` is an
# ENUMERATION, and an enumeration offers as many places to make a mistake as
# it has members: the same unsatisfiable `\b` was written twice in two
# alternatives by the same hand, which is the signature of the form, not of
# carelessness. Worse, every form nobody happened to list is invisible BY
# CONSTRUCTION, so the next unlisted spelling silently reopens the defect.
#
# WHAT IS ENUMERATED NOW, STATED PLAINLY AS THE STANDING CONTRACT REQUIRES
# (any gate that enumerates must name the property its list stands in for and
# why the list form was chosen):
#   1. THE ROOTS a marker can hang off in a collected file — `pytest`,
#      `unittest`, `self`. This is syntax, not vocabulary: it is fixed by the
#      two test frameworks' public API surface, not by which marks exist.
#   2. A PREDICATE over the marker's OWN last attribute (`suppresses_gating`),
#      not a list of full spellings: the stems `skip*` and `xfail*`, plus the
#      single irregular member `expectedFailure`, which shares no stem with
#      them. Any present or future `skip`/`skipif`/`skipIf`/`skipUnless`/
#      `skipTest`/`SkipTest`/`xfail`/`xfail_*` spelling is covered by the
#      stems rather than by having been listed — which is what dissolves the
#      generator of holes rather than patching two of its members.
#
# WHY NOT DERIVE THE VOCABULARY FROM pytest/unittest AT RUNTIME (asked
# explicitly by this bead, answered rather than skipped): `pytest.mark.<X>` is
# an OPEN namespace — every attribute access returns a valid MarkDecorator, so
# there is no finite marker vocabulary to enumerate FROM. unittest's
# suppressors are attributes of the module, but selecting WHICH of them
# suppress gating still needs exactly the predicate below, so importing buys
# nothing and costs a great deal: this module is PURE (no I/O, no third-party
# import) so the merge gate can answer from a diff alone, and a verdict that
# depended on the installed pytest version would mean the gate SEES DIFFERENT
# THINGS IN DIFFERENT ENVIRONMENTS — the exact silent-instrument class this
# bead is about.
#
# xfail IS RULED IN, DELIBERATELY (this bead required a decision, not a
# guess). An xfail'd test still EXECUTES, so it survives any check that merely
# confirms the test still runs, but its result no longer gates; and a
# non-strict xfail that starts passing announces nothing at all. That is a
# stronger prune than a skip, not a lesser one.
#
# KNOWN RESIDUALS — named here rather than left silent, because a bounded
# search that does not publish its bounds reports "did not look" as "not
# there". A text scan over a diff CANNOT see:
#   * a marker alias defined in a NON-collected helper module (`requires_bd =
#     pytest.mark.skipif(...)` in bin/foo.py, applied as `@requires_bd` in a
#     test file) — the application site carries no framework root;
#   * a bare `raise SkipTest(...)` imported name-first;
#   * pruning with NO textual marker at all: a fixture that errors into a
#     skip, a conditional that stops collecting, a parametrize narrowed so
#     cases vanish, a test file dropped from selection.
# And the mirror image, which this fix made ACUTE rather than introduced —
# text ABOUT a marker is indistinguishable from a marker to a text scan, so a
# test file full of marker fixtures (this module's own suite, necessarily)
# counts every one. MEASURED on the branch that landed this fix: 25 markers
# reported, net +20 test functions, ZERO tests newly skipped. Filed as
# SABLE-k2gbq with the object evidence; do NOT "fix" it by narrowing the
# detector, which would re-open the fail-open above.
# The residual group is why the standing design direction is a RUNTIME signal
# (pytest's own collected-but-not-reported outcome set, diffed against the
# spine's) — strictly more accurate, and unreachable by any regex. It is not
# taken here: it requires running the suite (this leg answers from a diff
# alone, before the queue wait and before the impact tier) and a stored spine
# baseline, and it must distinguish newly-non-contributing from
# always-non-contributing or it reports this repo's own bd/dolt clean-room
# self-skips as a defect every run and gets bypassed by habit. This text leg
# is the cheap pre-filter half of that hybrid, deliberately.
# --------------------------------------------------------------------------

# Files pytest/unittest actually COLLECT. A gating-suppressing marker only
# suppresses anything if it lands somewhere that gets collected, so this is
# the property, not a convenience filter — and it is also what keeps the word
# `skipif` in a comment, docstring or string literal in an ordinary source
# file from being counted. A gate that fires on everything trains the seat to
# reach for --coverage-override reflexively (SABLE-r5pfw), which is strictly
# worse than the under-match this bead fixes.
_COLLECTED_PY_FILE_RE = re.compile(
    r'(^|/)(test_[\w]+\.py|[\w]+_test\.py|conftest\.py)$')

# The two syntactic shapes a gating-suppressing marker takes. Both capture the
# marker's FULL dotted name (group 1) and its LAST attribute (group 2) — the
# last attribute is what `suppresses_gating` classifies, so `pytest.mark.skipif`
# and `unittest.mock.patch` are told apart by what they ARE rather than by
# which of them was remembered when the pattern was written.
_DECORATOR_MARKER_RE = re.compile(r'^@((?:pytest|unittest)(?:\.\w+)*\.(\w+))')
_CALL_MARKER_RE = re.compile(
    r'(?:^|[^\w.])((?:pytest|unittest|self)(?:\.\w+)*\.(\w+))\s*\(')

_SUPPRESSING_STEMS = ("skip", "xfail")
_SUPPRESSING_IRREGULAR = frozenset({"expectedfailure"})


def suppresses_gating(marker_name: str) -> bool:
    """Does a marker with this (last-attribute) name stop a test from
    contributing to the gate? Stem-based on purpose: `skip` and `xfail` are
    PREFIXES of every spelling both frameworks use for the property
    (skip/skipif/skipIf/skipUnless/skipTest/SkipTest, xfail/xfail_*), so a
    spelling nobody has thought of yet is still classified correctly, and
    there is no word boundary to get wrong. `expectedFailure` is the one
    member that shares no stem with the others — it is listed, and the fact
    that it must be listed is exactly why the list is a set of stems plus one
    named exception rather than a set of full spellings."""
    lowered = marker_name.lower()
    return lowered.startswith(_SUPPRESSING_STEMS) or lowered in _SUPPRESSING_IRREGULAR


def find_marker_in_added_line(body: str):
    """Return the dotted name of the gating-suppressing marker introduced by
    one added source line (the line WITHOUT its leading diff `+`), or None.

    A commented-out line is not code and prunes nothing, so a line whose
    first non-space character is `#` never counts — that is the commented-out
    -decorator case, which would otherwise make a diff that DISABLES a skip
    read as one that adds it."""
    stripped = body.strip()
    if not stripped or stripped.startswith("#"):
        return None
    m = _DECORATOR_MARKER_RE.match(stripped)
    if m and suppresses_gating(m.group(2)):
        return m.group(1)
    for m in _CALL_MARKER_RE.finditer(stripped):
        if suppresses_gating(m.group(2)):
            return m.group(1)
    return None


@dataclass(frozen=True)
class SkipMarker:
    """ONE newly-added gating-suppressing marker, NAMED. The count alone was
    not settleable: this bead exists because a gate reported "1 newly-added
    skip marker" where a seat counted 4 by eye, and settling that
    disagreement cost a hand-diff. A count with no operands cannot be checked
    against a human's own reading in one pass; a file:line and the source
    line itself can."""
    path: str
    line: int
    marker: str
    text: str

    def describe(self) -> str:
        shown = self.text if len(self.text) <= 100 else self.text[:97] + "..."
        return f"{self.path}:{self.line} {shown}"


@dataclass
class PruningSignal:
    removed_test_functions: list = field(default_factory=list)
    added_test_functions: list = field(default_factory=list)
    skip_markers: list = field(default_factory=list)
    deleted_test_files: list = field(default_factory=list)

    @property
    def newly_skipped_markers(self) -> int:
        """How many gating-suppressing markers this diff ADDS. Derived from
        skip_markers rather than counted alongside it, so the number and the
        list it summarises cannot drift apart (the 1-vs-4 incident)."""
        return len(self.skip_markers)

    @property
    def net_test_function_delta(self) -> int:
        """len(added) - len(removed), i.e. did the SIZE of the test-function
        set grow or shrink (SABLE-owix4). This is the property the floor
        actually cares about — not which individual names moved. A rename
        removes one name and adds a different one: delta 0. A move to
        another file: delta 0. A split into two: delta +1. All three land
        at zero-or-positive here and are NOT pruning, regardless of the
        fact that a specific name vanished from the diff. Only a genuine
        reduction in the number of test functions goes negative."""
        return len(self.added_test_functions) - len(self.removed_test_functions)

    @property
    def is_pruning(self) -> bool:
        return bool(self.net_test_function_delta < 0 or self.newly_skipped_markers
                    or self.deleted_test_files)

    @property
    def reasons(self) -> list:
        out = []
        if self.net_test_function_delta < 0:
            removed_str = ", ".join(self.removed_test_functions) or "none"
            added_str = ", ".join(self.added_test_functions) or "none"
            out.append(
                f"net decrease of {-self.net_test_function_delta} test "
                f"function(s) (removed: {removed_str}; added: {added_str})")
        if self.skip_markers:
            named = "; ".join(m.describe() for m in self.skip_markers)
            out.append(f"{self.newly_skipped_markers} newly-added skip/xfail "
                       f"marker(s): {named}")
        if self.deleted_test_files:
            out.append("deleted test file(s): " + ", ".join(self.deleted_test_files))
        return out


def detect_pruning(diff_text: str) -> PruningSignal:
    """Scan a unified diff (as produced by `git diff <base>...<branch>`) for
    signals correlated with a coverage regression.

    THE PROPERTY detect_pruning's function-count leg ANSWERS (SABLE-owix4):
    did the NUMBER of test functions go down between base and tip — not did
    a specific def NAME disappear. A unified diff only ever shows lines that
    CHANGED, so every `def test_*` line present in base but absent in tip
    appears as a `-` line somewhere in the diff, and every one present in
    tip but absent in base appears as a `+` line, regardless of which file
    it ended up in or what it was renamed to. Comparing the SIZE of those
    two sets (PruningSignal.net_test_function_delta), instead of asking
    which individual names are common to both, is what makes a rename, a
    move to another file, or a split into more functions net to
    zero-or-positive and pass silently — only a genuine NET reduction in
    the number of test functions is reported as pruning.

    (Several tests merged into one @pytest.mark.parametrize function IS
    still a net reduction in def count by this measure — a static diff scan
    cannot see that the parametrize cases replay every input, so that shape
    is deliberately left flagged as pruning-shaped, which routes it to the
    real coverage-delta check rather than allowing or denying it outright;
    see evaluate_coverage_floor.)

    Skip markers and whole-file deletions are kept as direct property
    checks rather than set comparisons: a newly-added skip marker doesn't
    change any def count but does stop a test from running, and this
    module's KNOWN RESIDUAL (see module docstring) never parses shell
    test-file bodies at all, so for a deleted `.sh` suite file-existence is
    the only signal available — there is no name or count to compare.

    THE PROPERTY the skip-marker leg answers (SABLE-a4i8h): did this diff
    ADD a marker that prevents a test from contributing to the gate — in a
    file that is actually collected. Each such marker is reported as a
    SkipMarker naming its file, its line in the post-image, and the source
    line itself, so a seat can settle the count against its own reading in
    one pass instead of by hand-diffing. See the block comment above
    `suppresses_gating` for how markers are classified and what no text
    scan of a diff can see."""
    removed_fns: set = set()
    added_fns: set = set()
    skip_markers: list = []
    deleted_files: list = []
    current_file = None
    file_deleted = False
    # Line number this added line will occupy in the POST-image, tracked
    # from each hunk header's `+start`. Context and added lines advance it;
    # removed lines do not exist in the post-image and do not.
    new_lineno = 0

    def _flush():
        if current_file is not None and file_deleted and _TEST_FILE_RE.search(current_file):
            deleted_files.append(current_file)

    for line in diff_text.splitlines():
        m = _DIFF_HEADER_RE.match(line)
        if m:
            _flush()
            current_file = m.group(2)
            file_deleted = False
            new_lineno = 0
            continue
        if _DELETED_MODE_RE.match(line):
            file_deleted = True
            continue
        m = _HUNK_HEADER_RE.match(line)
        if m:
            new_lineno = int(m.group(1))
            continue
        if line.startswith("\\"):
            # `\ No newline at end of file` — annotation, not a line of either
            # image, so it advances nothing.
            continue
        if line.startswith("-"):
            m = _REMOVED_TEST_FN_RE.match(line)
            if m:
                removed_fns.add(m.group(1))
            continue
        if line.startswith("+"):
            body = line[1:]
            m = _ADDED_TEST_FN_RE.match(line)
            if m:
                added_fns.add(m.group(1))
            elif current_file and _COLLECTED_PY_FILE_RE.search(current_file):
                marker = find_marker_in_added_line(body)
                if marker:
                    skip_markers.append(SkipMarker(path=current_file,
                                                   line=new_lineno,
                                                   marker=marker,
                                                   text=body.strip()))
            new_lineno += 1
            continue
        # Context line (or the file-header/no-newline noise between hunks,
        # which only ever appears where new_lineno is not yet meaningful).
        new_lineno += 1
    _flush()

    return PruningSignal(removed_test_functions=sorted(removed_fns),
                         added_test_functions=sorted(added_fns),
                         skip_markers=skip_markers,
                         deleted_test_files=deleted_files)


# --------------------------------------------------------------------------
# Named-reason override — mirrors hooks/multi-manager/pre-dispatch-model-
# check.sh's "Model override: <reason>" line exactly (same shape, same
# non-empty-reason requirement), just a different tag.
# --------------------------------------------------------------------------

def parse_named_override(text: str, tag: str = "Coverage override"):
    """Find a `<tag>: <reason>` line and return the trimmed reason. Two forms
    are accepted, both requiring a non-empty reason:

      1. A `<tag>: <reason>` LINE embedded anywhere in `text` (case-
         insensitive, multiline search) — mirrors pre-dispatch-model-check.sh's
         `^Model override:[[:space:]]+\\S` regex exactly, for text blobs
         (bead notes, dispatch prompts) that carry the tag inline.
      2. `text` itself IS the reason, tag-free — the shape
         `sable-merge-gate promote --coverage-override "<reason>"` produces,
         where the CLI flag's value already is the reason and does not repeat
         the tag.

    Returns None if `text` is empty/whitespace-only in both forms, i.e. an
    override is never inferred from an absent or blank reason."""
    if not text or not text.strip():
        return None
    pattern = re.compile(rf'(?im)^{re.escape(tag)}:[ \t]+(\S.*)$')
    m = pattern.search(text)
    if m:
        return m.group(1).strip()
    # No valid "tag: reason" line. If the tag appears at all — just with a
    # missing or blank reason — this is a BOGUS override attempt, not a bare
    # reason, and must not fall through to the bare-text branch below (which
    # would otherwise launder the tag text itself into a truthy "reason").
    if re.search(rf'(?im)^{re.escape(tag)}:', text):
        return None
    return text.strip()


# --------------------------------------------------------------------------
# The decision table — PURE and total, same style as
# sable_gate_promote_lib.decide_promotion so it is checkable by enumeration.
# --------------------------------------------------------------------------

ACTION_ALLOW = "allow"
ACTION_DENY = "deny"


@dataclass
class CoverageFloorDecision:
    action: str
    reason: str


def evaluate_coverage_floor(signal: PruningSignal, coverage_check_passed,
                            override_reason) -> CoverageFloorDecision:
    """coverage_check_passed is tri-valued: True (diff-cover ran and the patch
    met --fail-under), False (diff-cover ran and failed it), or None (the
    branch does not carry the check at all, or it could not be run) — None and
    False are handled IDENTICALLY here, both deny, because "we could not prove
    it's covered" and "we proved it's not" both mean the same thing to a
    promotion: don't. The tri-state exists only so callers can say which
    happened, exactly as sable_gate_promote_lib.decide_promotion's `disjoint`
    parameter does for the base-moved decision."""
    if not signal.is_pruning:
        return CoverageFloorDecision(ACTION_ALLOW,
            "not a pruning diff — no coverage-delta check required")

    if override_reason:
        return CoverageFloorDecision(ACTION_ALLOW,
            f"pruning diff ({'; '.join(signal.reasons)}), named override: {override_reason}")

    if coverage_check_passed is True:
        return CoverageFloorDecision(ACTION_ALLOW,
            f"pruning diff ({'; '.join(signal.reasons)}), diff-cover patch-coverage "
            f"check passed")

    if coverage_check_passed is False:
        return CoverageFloorDecision(ACTION_DENY,
            f"pruning diff ({'; '.join(signal.reasons)}) and the diff-cover "
            f"patch-coverage check FAILED — coverage regressed on the "
            f"removed/skipped test's lines")

    return CoverageFloorDecision(ACTION_DENY,
        f"pruning diff ({'; '.join(signal.reasons)}) but no coverage-delta check "
        f"(.github/ci/diff-cover-gate.sh, diff-cover --fail-under) was carried "
        f"on this branch — DENIED. Add the check or a "
        f"'Coverage override: <reason>' line.")
