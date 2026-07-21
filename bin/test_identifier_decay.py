#!/usr/bin/env python3
"""Unit tests for the identifier-decay detector (SABLE-x9vby).

The detector's whole value is a two-directional claim, so every test here is
paired: an INSTRUCTIONAL referral to a retiring identifier must flag, and a
PROVENANCE mention of the same identifier must not. A one-directional suite
would pass happily on a detector that flags every relate-link, which is the
banner-blindness failure mode this hook has to avoid.

The known-positive rehearsal (test_known_positive_jejx3_names_gz3v2) pins the
mandatory validation: the sweep must independently surface the real case chuck
found by hand — SABLE-gz3v2 <- SABLE-jejx3 — using that bead's VERBATIM note
line. An instrument that reports a comfortable number without detecting the
case it was built for is a dead grep.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sable_identifier_decay_lib import (  # noqa: E402
    KNOWN_LIMIT, format_flags, format_unassessed, identifier_variants,
    is_instructional, is_provenance, sweep, sweep_bead,
)

CLI = Path(__file__).resolve().parent / "sable-identifier-decay"


def bead(bead_id, *, title="a bead", description="", notes=""):
    return {"id": bead_id, "title": title, "description": description, "notes": notes}


# --------------------------------------------------------------------------
# The core two-directional claim
# --------------------------------------------------------------------------

def test_flags_instructional_referrer_not_provenance():
    """Both directions in one corpus: the instruction flags, the relate-link
    does not, and the detector distinguishes them within the SAME sweep."""
    corpus = [
        bead("SABLE-instr", notes="verify SABLE-X's suppression still holds after your change"),
        bead("SABLE-prov", notes="RELATES: SABLE-X"),
    ]
    flagged = {f.referrer_id for f in sweep(["SABLE-X"], corpus)}
    assert "SABLE-instr" in flagged, "instructional referrer must flag"
    assert "SABLE-prov" not in flagged, "a bare RELATES link must NOT flag"


def test_provenance_line_is_not_flagged_even_carrying_instructional_words():
    """The provenance exclusion is decided by the LINE SHAPE, not by the absence
    of instructional vocabulary — otherwise 'RELATES: SABLE-X (must-fix sibling)'
    would flag and every richly-annotated link becomes noise."""
    corpus = [bead("SABLE-prov2", notes="RELATES: SABLE-X (the must-fix sibling)")]
    assert sweep(["SABLE-X"], corpus) == []


def test_non_instructional_mention_is_not_flagged():
    """A bead that merely NARRATES the identifier is not carrying an instruction
    about it; flagging it is pure noise with no decay behind it."""
    corpus = [bead("SABLE-narr", notes="SABLE-X landed on the integration branch yesterday.")]
    assert sweep(["SABLE-X"], corpus) == []


@pytest.mark.parametrize("line", [
    "you must verify SABLE-X before landing",
    "HARD REQUIREMENT: migrate the hold recorded on SABLE-X",
    "DO NOT CLOSE until SABLE-X is merged",
    "assert SABLE-X's suppression still holds",
    "this hold needs SABLE-X to land first",
])
def test_instructional_vocabulary_is_tuned_loose(line):
    """Tuned LOOSE on purpose: a false flag costs one read, a miss costs an agent
    honouring a dead instruction indefinitely. These five shapes all flag."""
    assert sweep(["SABLE-X"], [bead("SABLE-r", notes=line)]), line


# --------------------------------------------------------------------------
# Mandatory validation: the case this instrument was built for
# --------------------------------------------------------------------------

# VERBATIM from SABLE-jejx3's notes (line 5 of that field) — the requirement
# that survived lincoln's entirely correct close of SABLE-gz3v2 and then pointed
# at a closed bead with nothing to migrate. Note it names the identifier BARE
# ("gz3v2"), which is why bare-suffix matching is not optional.
JEJX3_NOTE_LINE = (
    "SO THE FIX FOR THIS BEAD CAN DESTROY ITS OWN MITIGATION. Any change to "
    "predicate 3's semantics may silently drop gz3v2's suppression while "
    "wk-tree-claim-target is still held, at which point the floor resumes "
    "auto-filing the INVERTED 'merge me' handoff. HARD REQUIREMENT: verify "
    "gz3v2's suppression still holds after your change, or migrate the hold to "
    "your new first-class mechanism IN THE SAME CHANGE."
)


def test_known_positive_jejx3_names_gz3v2():
    """The sweep must independently surface the hand-found instance."""
    corpus = [bead("SABLE-jejx3", title="HELD as a first-class third outcome",
                   notes=JEJX3_NOTE_LINE)]
    flags = sweep(["SABLE-gz3v2"], corpus)
    assert flags, "the sweep MUST detect the case it was built for (gz3v2 <- jejx3)"
    assert flags[0].referrer_id == "SABLE-jejx3"
    assert "HARD REQUIREMENT" in flags[0].line


def test_known_positive_would_be_missed_without_bare_suffix_matching():
    """Negative control on the instrument itself: full-id-only matching misses
    the motivating case, which is why identifier_variants() exists."""
    corpus = [bead("SABLE-jejx3", notes=JEJX3_NOTE_LINE)]
    assert sweep(["SABLE-gz3v2"], corpus, bare_suffix=False) == []


# --------------------------------------------------------------------------
# Matching boundaries
# --------------------------------------------------------------------------

def test_identifier_variants_expands_bead_ids_but_not_branch_names():
    assert identifier_variants("SABLE-gz3v2") == ["SABLE-gz3v2", "gz3v2"]
    assert identifier_variants("wk-dep-merge-guard", bare_suffix=False) == ["wk-dep-merge-guard"]


def test_short_suffixes_do_not_expand():
    """A 3-char suffix collides with ordinary words; expanding it would drown
    the flag in noise even under a loose tuning."""
    assert identifier_variants("SABLE-abc") == ["SABLE-abc"]


def test_mention_must_be_a_delimited_token():
    """Substring matching would flag SABLE-x9vby2 for SABLE-x9vby."""
    corpus = [bead("SABLE-r", notes="you must verify SABLE-x9vby2 first")]
    assert sweep(["SABLE-x9vby"], corpus) == []


def test_closing_bead_does_not_flag_itself():
    """A bead's instructions about itself are not decay — it is going away."""
    corpus = [bead("SABLE-X", notes="you must verify SABLE-X before closing")]
    assert sweep(["SABLE-X"], corpus) == []


def test_branch_name_seam_flags_a_hold_instruction():
    """Promote-time seam: deleting a merged branch retires the branch NAME, and
    a hold keyed to that name decays the instant it stops resolving."""
    corpus = [bead("SABLE-3nymz",
                   notes="DO NOT MERGE wk-dep-merge-guard while this hold stands")]
    flags = sweep(["wk-dep-merge-guard"], corpus, bare_suffix=False)
    assert [f.referrer_id for f in flags] == ["SABLE-3nymz"]


def test_all_scanned_fields_are_swept():
    for field in ("title", "description", "notes"):
        corpus = [bead("SABLE-r", **{field: "must verify SABLE-X"} if field != "title"
                       else {"title": "must verify SABLE-X"})]
        flags = sweep(["SABLE-X"], corpus)
        assert [f.field for f in flags] == [field], field


def test_flag_records_field_and_line_number():
    corpus = [bead("SABLE-r", notes="intro line\nanother line\nmust verify SABLE-X here")]
    (flag,) = sweep(["SABLE-X"], corpus)
    assert (flag.field, flag.line_no) == ("notes", 3)
    assert flag.line == "must verify SABLE-X here"


def test_multiple_retiring_identifiers_are_all_swept():
    corpus = [bead("SABLE-r", notes="must verify SABLE-A\nand migrate SABLE-B")]
    flags = sweep(["SABLE-A", "SABLE-B"], corpus)
    assert {f.identifier for f in flags} == {"SABLE-A", "SABLE-B"}


def test_predicates_are_independently_correct():
    assert is_instructional("you must do the thing")
    assert not is_instructional("this landed yesterday")
    assert is_provenance("RELATES: SABLE-X")
    assert is_provenance("  - blocked by: SABLE-X")
    assert not is_provenance("the reference implementation must verify SABLE-X")


def test_sweep_bead_is_the_single_bead_unit():
    flags = sweep_bead("SABLE-X", bead("SABLE-r", notes="must verify SABLE-X"))
    assert len(flags) == 1 and flags[0].referrer_id == "SABLE-r"


# --------------------------------------------------------------------------
# Reporting: silence means clean, and could-not-assess never looks clean
# --------------------------------------------------------------------------

def test_no_flags_renders_nothing():
    """Rare enough to be read is a property of the SILENT case too — a clean
    sweep must add zero lines to the closer's screen."""
    assert format_flags([], ["SABLE-X"]) == ""


def test_report_names_the_referrer_the_line_and_the_known_limit():
    flags = sweep(["SABLE-X"], [bead("SABLE-r", title="the referrer",
                                     notes="must verify SABLE-X")])
    report = format_flags(flags, ["SABLE-X"])
    assert "SABLE-r" in report
    assert "must verify SABLE-X" in report
    assert "notes:1" in report
    assert KNOWN_LIMIT in report, "a detector whose limits are undocumented gets trusted past them"


def test_long_reports_truncate_but_never_silently():
    """A hub bead can leave twenty-plus referrals; twenty-plus is not readable.
    The cap must state the TRUE total and how to see the rest — a silent cap
    reads as 'everything is covered' when it isn't."""
    corpus = [bead(f"SABLE-r{i}", notes="must verify SABLE-X") for i in range(25)]
    flags = sweep(["SABLE-X"], corpus)
    assert len(flags) == 25
    report = format_flags(flags, ["SABLE-X"])
    assert "leaves 25 open instructions" in report, "the head states the TRUE total"
    assert "15 more not shown" in report
    assert "sable-identifier-decay SABLE-X" in report, "names how to see the rest"
    assert "SABLE-r0" in report and "SABLE-r24" not in report


def test_known_limit_names_the_undetectable_case():
    assert "SABLE-3nymz" in KNOWN_LIMIT
    assert "code path" in KNOWN_LIMIT.lower()


def test_unassessed_report_is_loud_and_never_reads_as_clean():
    msg = format_unassessed(["SABLE-X"], "bd is not on PATH")
    assert "COULD NOT ASSESS" in msg
    assert "bd is not on PATH" in msg
    assert "NOT a clean result" in msg
    assert "fail-open" in msg.lower()


# --------------------------------------------------------------------------
# CLI surface (no bd required: these paths never reach a query)
# --------------------------------------------------------------------------

def _cli(*args, env=None):
    return subprocess.run([sys.executable, str(CLI), *args], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)


def test_cli_with_no_identifiers_is_silent_and_clean():
    cp = _cli()
    assert cp.returncode == 0 and cp.stdout.strip() == ""


def test_cli_help_states_the_known_limit():
    cp = _cli("--help")
    assert cp.returncode == 0
    assert "code path" in cp.stdout.lower()


def test_cli_reports_could_not_assess_when_bd_is_absent(tmp_path):
    """Discipline 7, the failure direction: no bd on PATH must produce a LOUD
    could-not-assess and exit 3 — never the silence of a clean sweep."""
    empty_path = tmp_path / "emptybin"
    empty_path.mkdir()
    import os
    env = dict(os.environ)
    env["PATH"] = str(empty_path)
    cp = subprocess.run([sys.executable, str(CLI), "SABLE-X"], text=True, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert cp.returncode == 3, cp.stderr
    assert "COULD NOT ASSESS" in cp.stdout
    assert "NOT a clean result" in cp.stdout
